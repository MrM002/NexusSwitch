"""Miner process manager: builds the command line, launches the miner, kills
the whole process tree on switch/exit (no zombies) and reports crashes."""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from config import LOG_DIR

log = logging.getLogger("nexus.miner")

PID_FILE = LOG_DIR / "miner.pid"
MINER_LOG = LOG_DIR / "miner.log"
GRACEFUL_TIMEOUT = 8.0

# Argument templates, checked against each miner's official documentation.
# Placeholders: {algo} {pool} {host} {port} {url} {wallet} {worker}
#   {pool} = host:port            {url} = stratum+tcp://host:port (stratum+ssl:// for TLS pools)
# Each whitespace-separated token is formatted on its own, so values can never
# break out into extra arguments. Override per coin with "args" in config.json.
#   tls          : appended when the pool is given as "ssl://host:port"
#   algo_extra   : appended for one algorithm
#   coin_extra   : appended for one coin
#   skip_coins   : coins that share the algorithm family on the market feed but
#                  need a different miner algorithm / login (set "miner" + "args")
MINERS: dict[str, dict[str, Any]] = {
    "xmrig": {  # https://xmrig.com/docs/miner/command-line-options
        "label": "XMRig",
        "exe": ["xmrig.exe", "xmrig"],
        "args": "-o {pool} -u {wallet} -p {worker} --rig-id {worker} -a {algo} -k --no-color",
        "tls": "--tls",
        "algos": {"randomx": "rx/0"},
        "skip_coins": ["EPIC", "XDAG", "ETI"],
    },
    "lolminer": {  # https://github.com/Lolliedieb/lolMiner-releases
        "label": "lolMiner",
        "exe": ["lolMiner.exe", "lolMiner"],
        "args": "--algo {algo} --pool {pool} --user {wallet}.{worker}",
        "tls": "--tls on",
        "algos": {
            "beamhash3": "BEAM-III", "zhash": "EQUI144_5", "zelhash": "FLUX",
            "etchash": "ETCHASH", "autolykos2": "AUTOLYKOS2",
        },
        # Equihash 144/5 needs a personalization string; "auto" asks the pool.
        "algo_extra": {"zhash": "--pers auto"},
        "coin_extra": {"BTG": "--pers BgoldPoW", "BTCZ": "--pers BitcoinZ"},
    },
    "rigel": {  # https://github.com/rigelminer/rigel
        "label": "Rigel",
        "exe": ["rigel.exe", "rigel"],
        "args": "-a {algo} -o {url} -u {wallet} -w {worker}",
        "algos": {"kawpow": "kawpow", "autolykos2": "autolykos2", "etchash": "etchash"},
        # documented --coin tickers: avoid DAG rebuilds around the dev-fee switch
        "coin_extra": {"RVN": "--coin rvn", "QUAI": "--coin quai", "XNA": "--coin xna",
                       "NEOX": "--coin neox", "ERG": "--coin erg"},
    },
    "srbminer": {  # https://github.com/doktor83/SRBMiner-Multi (Parameters)
        "label": "SRBMiner-Multi",
        "exe": ["SRBMiner-MULTI.exe", "SRBMiner-MULTI"],
        "args": "--algorithm {algo} --pool {pool} --wallet {wallet}.{worker}",
        "tls": "--tls true",
        "algos": {"randomx": "randomx", "autolykos2": "autolykos2", "kawpow": "kawpow", "etchash": "etchash"},
        "algo_extra": {"randomx": "--disable-gpu", "autolykos2": "--disable-cpu",
                       "kawpow": "--disable-cpu", "etchash": "--disable-cpu"},
        "skip_coins": ["EPIC", "XDAG", "ETI"],
    },
    "trex": {  # https://github.com/trexminer/T-Rex (NVIDIA only)
        "label": "T-Rex",
        "exe": ["t-rex.exe", "t-rex"],
        "args": "-a {algo} -o {url} -u {wallet} -p x -w {worker}",
        "algos": {"kawpow": "kawpow", "etchash": "etchash", "autolykos2": "autolykos2"},
    },
}

# Preferred miner order per algorithm (first one the user has installed wins).
ALGO_MINERS: dict[str, list[str]] = {
    "randomx": ["xmrig", "srbminer"],
    "kawpow": ["rigel", "trex", "srbminer"],
    "autolykos2": ["rigel", "lolminer", "trex", "srbminer"],
    "etchash": ["rigel", "lolminer", "trex", "srbminer"],
    "zhash": ["lolminer"],
    "beamhash3": ["lolminer"],
    "zelhash": ["lolminer"],
    # No bundled template: current SRBMiner-Multi releases dropped these, so
    # they need a per-coin "miner" + "args" override in config.json.
    "dynexsolve": [],
    "astrobwt": [],
    "equihash": [],  # ASIC territory
}

_TLS_SCHEMES = ("ssl://", "tls://", "stratum+ssl://", "stratum+tls://")
_TCP_SCHEMES = ("tcp://", "stratum+tcp://", "stratum://")


def parse_pool(value: str) -> tuple[str, str, bool]:
    """``[ssl://]host:port`` -> (host, port, tls). Raises MinerError."""
    pool = _safe(value, "pool")
    low, tls = pool.lower(), False
    for scheme in _TLS_SCHEMES + _TCP_SCHEMES:
        if low.startswith(scheme):
            pool, tls = pool[len(scheme):], scheme in _TLS_SCHEMES
            break
    host, _, port = pool.rpartition(":")
    if not host or not port.isdigit() or "/" in host or host.startswith("-"):
        raise MinerError(f"pool must be host:port or ssl://host:port (got {value!r})")
    return host, port, tls


class MinerError(Exception):
    pass


@dataclass
class MinerPlan:
    miner: str
    label: str
    exe: str
    args: list[str]


def _safe(value: str, what: str) -> str:
    """Reject values that could be mistaken for extra CLI options."""
    v = str(value).strip()
    if not v or v.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in v):
        raise MinerError(f"invalid {what}: {value!r}")
    return v


def kill_process_tree(pid: int, timeout: float = GRACEFUL_TIMEOUT) -> None:
    """Terminate ``pid`` and all of its children: polite first, then forced."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(alive, timeout=3)


class MinerManager:
    def __init__(self, cfg: dict[str, Any], demo: bool = False):
        self.cfg = cfg
        self.demo = demo
        self.proc: subprocess.Popen | None = None
        self.coin: str | None = None
        self.algo: str | None = None
        self.miner_label = "-"
        self.started_at = 0.0
        self.donating = False
        self._log_fh = None
        self._lock = asyncio.Lock()
        atexit.register(self._atexit)

    # --------------------------------------------------------------- planning
    def plan(self, coin: str, algo: str, wallet: str | None = None) -> MinerPlan:
        """Work out which binary + arguments mine ``coin``. Raises MinerError
        with a human-readable reason when the coin cannot be mined."""
        coin = coin.upper()
        if self.demo:
            return MinerPlan("demo", "Simulated miner", sys.executable, _dummy_args())
        ccfg = self.cfg["coins"].get(coin)
        if not ccfg or not str(ccfg.get("wallet", "")).strip():
            raise MinerError("no wallet")
        host, port, tls = parse_pool(ccfg.get("pool", ""))
        pool = f"{host}:{port}"
        values = {
            "pool": pool, "host": host, "port": port,
            "url": f"stratum+{'ssl' if tls else 'tcp'}://{pool}",
            "wallet": _safe(wallet or ccfg["wallet"], "wallet"),
            "worker": _safe(self.cfg.get("worker_name") or "nexus", "worker name"),
        }
        candidates = [ccfg["miner"].lower()] if ccfg.get("miner") else ALGO_MINERS.get(algo, [])
        for name in candidates:
            spec = MINERS.get(name)
            exe = str(self.cfg["miners"].get(name, "")).strip().strip('"')
            if not spec or not exe or not Path(exe).is_file():
                continue
            custom = ccfg.get("args")
            if not custom and coin in spec.get("skip_coins", ()):
                continue  # same algorithm family, different miner setup
            malgo = spec["algos"].get(algo)
            template = custom or spec["args"]
            if not malgo and "{algo}" in template:
                continue
            parts = [template]
            if not custom:  # a custom template is used exactly as written
                coin_extra = spec.get("coin_extra", {}).get(coin, "")
                algo_extra = spec.get("algo_extra", {}).get(algo, "")
                if coin_extra.startswith("--pers"):
                    algo_extra = ""  # a coin-specific personalization replaces "auto"
                parts += [spec.get("tls", "") if tls else "", algo_extra, coin_extra]
            try:
                args = [tok.format(algo=malgo or "", **values) for tok in " ".join(parts).split()]
            except (KeyError, IndexError, ValueError) as e:
                raise MinerError(f"bad args template for {coin}: {e}") from e
            return MinerPlan(name, spec["label"], exe, args)
        raise MinerError("no miner" if not candidates else "miner not installed")

    def can_mine(self, coin: str, algo: str) -> tuple[bool, str]:
        try:
            self.plan(coin, algo)
            return True, ""
        except MinerError as e:
            return False, str(e)

    # ----------------------------------------------------------------- state
    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at if self.running else 0.0

    def exit_code(self) -> int | None:
        """Exit code if the miner died on its own, else None."""
        return self.proc.poll() if self.proc is not None else None

    # --------------------------------------------------------------- control
    async def start(self, coin: str, algo: str, wallet: str | None = None, donating: bool = False) -> MinerPlan:
        """Stop whatever is running, then launch the miner for ``coin``."""
        plan = self.plan(coin, algo, wallet)
        async with self._lock:
            await self._stop_locked()
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(MINER_LOG, "ab", buffering=0)
            self._log_fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {plan.label} -> {coin} =====\n".encode())
            kwargs: dict[str, Any] = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            try:
                self.proc = subprocess.Popen(
                    [plan.exe, *plan.args],
                    cwd=str(Path(plan.exe).resolve().parent),
                    stdin=subprocess.DEVNULL, stdout=self._log_fh, stderr=subprocess.STDOUT,
                    **kwargs,
                )
            except OSError as e:
                self._close_log()
                self.proc = None
                raise MinerError(f"could not launch {plan.label}: {e}") from e
            self.coin, self.algo = coin.upper(), algo
            self.miner_label, self.started_at, self.donating = plan.label, time.time(), donating
            self._write_pidfile()
            log.info("started %s (pid %s) for %s", plan.label, self.proc.pid, coin)
        return plan

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            await asyncio.to_thread(kill_process_tree, proc.pid)
            try:
                proc.wait(timeout=1)  # reap
            except subprocess.TimeoutExpired:
                pass
            log.info("stopped miner pid %s", proc.pid)
        self._close_log()
        self.coin = self.algo = None
        self.miner_label, self.donating = "-", False
        PID_FILE.unlink(missing_ok=True)

    def _close_log(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    # ------------------------------------------------------- orphan handling
    def _write_pidfile(self) -> None:
        try:
            p = psutil.Process(self.proc.pid)
            PID_FILE.write_text(json.dumps({"pid": p.pid, "created": p.create_time(), "name": p.name()}))
        except (psutil.Error, OSError):
            pass

    @staticmethod
    def cleanup_orphan() -> bool:
        """Kill a miner left behind by a previous hard crash of NexusSwitch.
        PID + creation time + name must all match, so an unrelated process that
        happens to reuse the PID is never touched."""
        try:
            info = json.loads(PID_FILE.read_text())
            p = psutil.Process(int(info["pid"]))
            if abs(p.create_time() - float(info["created"])) < 1.0 and p.name() == info["name"]:
                kill_process_tree(p.pid)
                return True
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            pass
        finally:
            PID_FILE.unlink(missing_ok=True)
        return False

    def _atexit(self) -> None:
        """Last line of defence: never leave a miner running after we exit."""
        if self.proc is not None and self.proc.poll() is None:
            kill_process_tree(self.proc.pid, timeout=4)
            PID_FILE.unlink(missing_ok=True)
        self._close_log()


def _dummy_args() -> list[str]:
    """Arguments that make our own executable behave as a harmless fake miner."""
    if getattr(sys, "frozen", False):
        return ["--dummy-miner"]
    return [str(Path(__file__).resolve().parent / "main.py"), "--dummy-miner"]


def detect_miner(name: str, search_dirs: list[Path]) -> str:
    """Best-effort search for a miner executable in likely folders."""
    for base in search_dirs:
        if not base.is_dir():
            continue
        for exe in MINERS[name]["exe"]:
            try:
                for hit in base.glob(f"**/{exe}"):
                    if hit.is_file():
                        return str(hit)
            except OSError:
                continue
    return ""
