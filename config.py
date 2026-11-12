"""Configuration: defaults, load/save, validation. Everything lives in a local
``config.json`` next to the app - nothing is ever sent anywhere else."""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

APP_VERSION = "1.0.0"


def app_dir() -> Path:
    """Folder that holds config.json / logs (next to the .exe when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = app_dir() / "config.json"
LOG_DIR = app_dir() / "logs"

# Canonical algorithm keys -> display name
ALGORITHMS: dict[str, str] = {
    "randomx": "RandomX",
    "equihash": "Equihash",
    "zhash": "Zhash",
    "beamhash3": "BeamHash III",
    "zelhash": "ZelHash",
    "kawpow": "KawPow",
    "autolykos2": "Autolykos2",
    "etchash": "Etchash",
    "astrobwt": "AstroBWT",
    "dynexsolve": "DynexSolve",
}

# Well-known public pools, used as editable defaults in the wizard.
DEFAULT_POOLS: dict[str, str] = {
    "XMR": "pool.supportxmr.com:3333",
    "ZEPH": "de.zephyr.herominers.com:1123",
    "RVN": "rvn.2miners.com:6060",
    "XNA": "xna.2miners.com:6060",
    "NEOX": "neox.2miners.com:4040",
    "ERG": "erg.2miners.com:8888",
    "ETC": "etc.2miners.com:1010",
    "BEAM": "beam.2miners.com:5252",
    "BTG": "btg.2miners.com:4040",
    "ZEC": "zec.2miners.com:1010",
    "DNX": "de.dynex.herominers.com:1120",
    "DERO": "de.dero.herominers.com:1117",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "worker_name": "nexus",
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": 0,
        "report_interval_hours": 4,
    },
    "hardware": {
        "cpu": {"model": "", "threads": 0},
        "gpu": {"model": "", "count": 0},
        # hashrate per algorithm in H/s (Sol/s for Equihash family)
        "hashrates": {},
        # power draw per algorithm in watts (for electricity cost)
        "power_watts": {},
    },
    "electricity_cost_kwh": 0.0,
    "miners": {"xmrig": "", "lolminer": "", "rigel": "", "srbminer": "", "trex": ""},
    # coin -> {"wallet": "...", "pool": "host:port", optional "miner", "args"}
    "coins": {},
    "switching": {
        "threshold_percent": 7.0,
        "confirmations": 2,
        "check_interval_seconds": 180,
        "min_runtime_minutes": 10,
        "manual_hold_minutes": 60,
        "min_volume_usd": 10000,
        "spike_ratio": 1.6,
        "mine_when_unprofitable": False,
    },
    # Transparent & optional: mine to the developer wallet for `percent` of the
    # time (only for coins the developer publishes a wallet for). Set
    # "enabled": false to turn it off completely - no hidden fees, ever.
    "donation_cycle": {"enabled": True, "percent": 1.0, "cycle_minutes": 600},
    # Minerstat's API now requires a (free) key. Leave empty to use the
    # key-less WhatToMine + CoinGecko source.
    "data_source": {"minerstat_api_key": ""},
}


class ConfigError(Exception):
    pass


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def default_config() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def config_exists(path: Path = CONFIG_PATH) -> bool:
    return path.is_file()


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load config.json merged over defaults (so new keys appear on upgrade)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"{path.name} not found - run with --setup") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} must contain a JSON object")
    cfg = _merge(DEFAULT_CONFIG, raw)
    cfg["coins"] = {str(k).upper(): v for k, v in cfg.get("coins", {}).items() if isinstance(v, dict)}
    validate(cfg)
    return cfg


def save_config(cfg: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    """Atomic write so a crash mid-save can never corrupt the config."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    try:  # config holds the bot token: keep it owner-only where the OS supports it
        os.chmod(path, 0o600)
    except OSError:
        pass


def validate(cfg: dict[str, Any]) -> None:
    """Clamp numeric settings to sane ranges; raise on unusable structure."""
    sw = cfg["switching"]
    try:
        sw["threshold_percent"] = max(0.0, float(sw["threshold_percent"]))
        sw["confirmations"] = max(1, int(sw["confirmations"]))
        sw["check_interval_seconds"] = max(60, int(sw["check_interval_seconds"]))
        sw["min_runtime_minutes"] = max(0.0, float(sw["min_runtime_minutes"]))
        sw["manual_hold_minutes"] = max(0.0, float(sw["manual_hold_minutes"]))
        sw["min_volume_usd"] = max(0.0, float(sw["min_volume_usd"]))
        sw["spike_ratio"] = max(1.05, float(sw["spike_ratio"]))
        cfg["electricity_cost_kwh"] = max(0.0, float(cfg["electricity_cost_kwh"]))
        dc = cfg["donation_cycle"]
        dc["percent"] = min(5.0, max(0.0, float(dc["percent"])))
        dc["cycle_minutes"] = max(60, int(dc["cycle_minutes"]))
        tg = cfg["telegram"]
        tg["report_interval_hours"] = max(0.25, float(tg["report_interval_hours"]))
        hw = cfg["hardware"]
        hw["hashrates"] = {k: max(0.0, float(v)) for k, v in hw.get("hashrates", {}).items()}
        hw["power_watts"] = {k: max(0.0, float(v)) for k, v in hw.get("power_watts", {}).items()}
    except (TypeError, ValueError, KeyError) as e:
        raise ConfigError(f"Invalid value in config.json: {e}") from e


DEMO_GPU_COUNT = 16


def demo_rig_label(cfg: dict[str, Any]) -> str:
    hw = cfg["hardware"]
    return (f"simulated rig: {hw['gpu']['count']}× {hw['gpu']['model']} + {hw['cpu']['model']}"
            f" · electricity ${cfg['electricity_cost_kwh']:.2f}/kWh · live market data")


def demo_config() -> dict[str, Any]:
    """Built-in sample rig used by ``--demo`` (no config.json required)."""
    cfg = default_config()
    cfg["worker_name"] = "demo-rig"
    gpus = DEMO_GPU_COUNT  # per-card figures match the RTX 3070 preset in the wizard
    cfg["hardware"] = {
        "cpu": {"model": "Ryzen 9 5950X", "threads": 32},
        "gpu": {"model": "RTX 3070", "count": gpus},
        "hashrates": {
            "randomx": 17500, "kawpow": 30e6 * gpus, "autolykos2": 175e6 * gpus,
            "etchash": 61e6 * gpus, "zhash": 95 * gpus, "beamhash3": 40 * gpus,
        },
        "power_watts": {
            "randomx": 140, "kawpow": 130 * gpus, "autolykos2": 130 * gpus,
            "etchash": 130 * gpus, "zhash": 130 * gpus, "beamhash3": 130 * gpus,
        },
    }
    # Power cost is left out of the demo (and the dashboard says so): after
    # electricity, results depend entirely on each user's own tariff.
    cfg["electricity_cost_kwh"] = 0.0
    cfg["switching"]["check_interval_seconds"] = 120
    cfg["switching"]["min_runtime_minutes"] = 0
    cfg["donation_cycle"]["enabled"] = False
    return cfg
