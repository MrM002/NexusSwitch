"""Offline self-test (no network, no real miner):  python test_core.py

Covers the switch threshold logic, the anti-spike filters, miner process-tree
handling + crash recovery, and the Telegram bot against a mocked Bot API.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import psutil
from rich.console import Console

import config
import donations
import main as app_main
import telegram_bot as tg
from miner_manager import MinerError, MinerManager
from profit_engine import Boost, CoinProfit, CoinQuote, ProfitEngine, SwitchDecider, canonical_algo, pick_anchor


def _p(coin: str, profit: float, algo: str = "randomx") -> CoinProfit:
    return CoinProfit(CoinQuote(coin, coin, algo, 1.0, 1e6, 1.0), 1.0, profit, 0.0, profit)


def test_switch_threshold() -> None:
    d = SwitchDecider(threshold_percent=7.0, confirmations=2)
    assert d.decide(None, [_p("XMR", 1.0)]).target.coin == "XMR"            # initial pick
    assert d.decide("XMR", [_p("ZEPH", 1.05), _p("XMR", 1.0)]).target is None  # +5% < 7%
    assert d.decide("XMR", [_p("ZEPH", 1.2), _p("XMR", 1.0)]).target is None   # 1st confirmation
    dec = d.decide("XMR", [_p("ZEPH", 1.2), _p("XMR", 1.0)])                   # 2nd -> switch
    assert dec.target.coin == "ZEPH" and round(dec.gain_pct) == 20
    # streak resets when the challenger drops back under the threshold
    d.decide("XMR", [_p("ZEPH", 1.2), _p("XMR", 1.0)])
    d.decide("XMR", [_p("ZEPH", 1.01), _p("XMR", 1.0)])
    assert d.decide("XMR", [_p("ZEPH", 1.2), _p("XMR", 1.0)]).target is None
    # current coin vanished (spike / no longer eligible) -> move immediately
    assert d.decide("XMR", [_p("ZEPH", 0.5)]).target.coin == "ZEPH"
    assert d.decide("XMR", []).target is None


def test_filters_and_ranking() -> None:
    cfg = config.demo_config()
    cfg["electricity_cost_kwh"] = 0.10
    eng = ProfitEngine(cfg)
    good = CoinQuote("XMR", "Monero", "randomx", 300.0, 5e6, 1e-7)
    thin = CoinQuote("SCAM", "Scam", "randomx", 300.0, 50.0, 1e-5)
    eng._apply_filters([good, thin])
    assert not good.spike and thin.spike and "volume" in thin.spike_reason
    for _ in range(4):  # build a baseline, then a 3x jump must be flagged
        q = CoinQuote("XMR", "Monero", "randomx", 300.0, 5e6, 1e-7)
        eng._apply_filters([q])
    jump = CoinQuote("XMR", "Monero", "randomx", 900.0, 5e6, 1e-7)
    eng._apply_filters([jump])
    assert jump.spike and "median" in jump.spike_reason
    ranked = eng.rank([good, CoinQuote("RVN", "Raven", "kawpow", 0.01, 1e6, 1e-9)])
    xmr = next(p for p in ranked if p.coin == "XMR")
    assert abs(xmr.revenue_usd - 1e-7 * 17500 * 300) < 1e-9
    assert abs(xmr.power_cost_usd - 0.140 * 24 * 0.10) < 1e-9          # electricity deducted
    assert ranked == sorted(ranked, key=lambda p: -p.profit_usd)
    assert canonical_algo("BeamHash III") == "beamhash3" and canonical_algo("Autolykos") == "autolykos2"
    assert canonical_algo("SHA-256") is None
    asyncio.run(eng.close())


def test_miner_plan() -> None:
    cfg = config.default_config()
    cfg["miners"]["xmrig"] = sys.executable  # any existing file
    cfg["coins"]["XMR"] = {"wallet": "4AbcWalletAddress123", "pool": "pool.example.com:3333"}
    m = MinerManager(cfg)
    plan = m.plan("XMR", "randomx")
    assert plan.miner == "xmrig" and plan.args[:4] == ["-o", "pool.example.com:3333", "-u", "4AbcWalletAddress123"]
    assert "rx/0" in plan.args
    assert m.can_mine("RVN", "kawpow") == (False, "no wallet")
    cfg["coins"]["RVN"] = {"wallet": "RWallet1234567", "pool": "rvn.example.com:6060"}
    assert m.can_mine("RVN", "kawpow") == (False, "miner not installed")
    cfg["coins"]["XMR"]["wallet"] = "--config=evil"   # option injection must be refused
    try:
        m.plan("XMR", "randomx")
        raise AssertionError("injection accepted")
    except MinerError:
        pass


async def _miner_lifecycle() -> None:
    console = Console(file=open("nul" if sys.platform == "win32" else "/dev/null", "w"), width=120)
    cfg = config.demo_config()
    app = app_main.NexusApp(cfg, console, demo=True)
    app.ranked = [_p("XMR", 1.0), _p("ZEPH", 0.9)]
    try:
        await app.miners.start("XMR", "randomx")
        pid1 = app.miners.proc.pid
        assert app.miners.running and psutil.pid_exists(pid1)
        await app.miners.start("ZEPH", "randomx")              # switch kills the old tree first
        pid2 = app.miners.proc.pid
        assert not psutil.pid_exists(pid1) or psutil.Process(pid1).status() == psutil.STATUS_ZOMBIE
        psutil.Process(pid2).kill()                              # simulate a miner crash
        dog = asyncio.create_task(app.watchdog_loop())
        for _ in range(60):
            await asyncio.sleep(0.5)
            if app.miners.running and app.miners.proc.pid != pid2:
                break
        dog.cancel()
        assert app.miners.running and app.miners.coin == "ZEPH", "watchdog did not restart the miner"
        assert any("crashed" in msg for _, msg, _ in app.state.logs)
        pid3 = app.miners.proc.pid
    finally:
        await app.shutdown()
    assert not psutil.pid_exists(pid3), "miner survived shutdown"
    assert not MinerManager.cleanup_orphan()


def test_miner_lifecycle() -> None:
    asyncio.run(_miner_lifecycle())


class _Hooks:
    def __init__(self) -> None:
        self.switched: list[str] = []

    def status_text(self) -> str:
        return "STATUS-OK"

    async def live_top_text(self, n: int) -> str:
        return f"TOP-{n}"

    async def request_switch(self, coin: str) -> str:
        self.switched.append(coin)
        return f"SWITCHED-{coin}"

    async def resume_auto(self) -> str:
        return "AUTO"


async def _telegram() -> None:
    owner, stranger = 111, 999
    sent: list[dict] = []
    queue = [
        {"update_id": 5, "message": {"text": "/status", "chat": {"id": owner}}},
        {"update_id": 6, "message": {"text": "/switch zeph", "chat": {"id": owner}}},
        {"update_id": 7, "message": {"text": "/switch XMR", "chat": {"id": stranger}}},   # must be ignored
        {"update_id": 8, "message": {"text": "/top", "chat": {"id": owner}}},
        {"update_id": 9, "message": {"text": "/donate", "chat": {"id": owner}}},
        {"update_id": 10, "message": {"text": "/switch ; rm -rf", "chat": {"id": owner}}},
    ]
    done = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if method == "getMe":
            result = {"username": "nexus_test_bot"}
        elif method == "sendMessage":
            sent.append(body)
            result = {"message_id": len(sent)}
        elif method == "getUpdates":
            if body.get("offset") == -1:
                result = [{"update_id": 4, "message": {"text": "/switch OLD", "chat": {"id": owner}}}]
            elif queue:
                assert body["offset"] == 5, "stale backlog was not skipped"
                result, queue[:] = list(queue), []
            else:
                done.set()
                await asyncio.sleep(0.05)
                result = []
        else:
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    hooks = _Hooks()
    bot = tg.TelegramBot("123456789:" + "A" * 35, owner, hooks, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(bot.run())
    await asyncio.wait_for(done.wait(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await bot.close()
    texts = [m["text"] for m in sent]
    assert all(m["chat_id"] == owner for m in sent), "replied to a stranger"
    assert hooks.switched == ["ZEPH"], hooks.switched       # no OLD, no stranger, no garbage
    assert "STATUS-OK" in texts and "SWITCHED-ZEPH" in texts and "TOP-5" in texts
    donate = next(t for t in texts if "Support NexusSwitch" in t)
    assert all(f"<code>{addr}</code>" in donate for addr in donations.DEV_WALLETS.values())
    assert len(donate) < 4096  # Telegram message limit
    assert any(t.startswith("Usage:") for t in texts)
    assert bot.connected and bot.username == "nexus_test_bot"
    assert tg.valid_token("123456789:" + "A" * 35) and not tg.valid_token("hello")
    assert "<token>" in tg._redact("https://api.telegram.org/bot123:abc/getMe", "123:abc")


def test_telegram() -> None:
    asyncio.run(_telegram())


def test_config_roundtrip() -> None:
    path = config.LOG_DIR / "_test_config.json"
    config.LOG_DIR.mkdir(exist_ok=True)
    cfg = config.default_config()
    cfg["coins"]["xmr"] = {"wallet": "w" * 20, "pool": "a.b:1"}
    cfg["switching"]["confirmations"] = 0
    config.save_config(cfg, path)
    loaded = config.load_config(path)
    path.unlink()
    assert "XMR" in loaded["coins"] and loaded["switching"]["confirmations"] == 1


def test_boost_vs_static() -> None:
    b = Boost("ERG", 2.20, 3.47)
    assert b.positive and round(b.gain_pct, 1) == 57.7 and round(b.gain_usd, 2) == 1.27
    assert b.line() == "vs. Static Mining (ERG): $2.20/d ➔ $3.47/d (🚀 +57.7% Boost | +$1.27/d with NexusSwitch)"
    same = Boost("XMR", 0.60, 0.60)
    assert not same.positive and "🚀" not in same.line() and "Boost" not in same.line()
    worse = Boost("XMR", 1.00, 0.90)                 # never dressed up as a gain
    assert not worse.positive and "-$0.10/d" in worse.line() and "🚀" not in worse.line()
    loss_base = Boost("RVN", -0.50, 0.25)            # % is meaningless on a negative baseline
    assert loss_base.gain_pct is None and "%" not in loss_base.line() and "+$0.75/d" in loss_base.line()
    # baseline coin: best established coin, else the median eligible coin
    assert pick_anchor([_p("QUAI", 3.0), _p("ERG", 2.0), _p("XMR", 1.0)]) == "ERG"
    assert pick_anchor([_p("AAA", 3.0), _p("BBB", 2.0), _p("CCC", 1.0)]) == "BBB"
    assert pick_anchor([]) is None


async def _demo_script() -> None:
    """Demo: start on the baseline coin, then switch on real threshold logic."""
    console = Console(file=open("nul" if sys.platform == "win32" else "/dev/null", "w"), width=120)
    app = app_main.NexusApp(config.demo_config(), console, demo=True)
    quotes = [
        CoinQuote("QUAI", "Quai", "kawpow", 1.0, 1e6, 3.0 / 480e6, diff_trend_pct=-1.0),
        CoinQuote("RVN", "Raven", "kawpow", 1.0, 1e6, 1.0 / 480e6),
        CoinQuote("ERG", "Ergo", "autolykos2", 1.0, 1e6, 2.0 / 2800e6),
        CoinQuote("XMR", "Monero", "randomx", 1.0, 1e6, 0.5 / 17500),
        CoinQuote("FAKE", "Fake", "randomx", 1.0, 5.0, 9.0 / 17500, spike=True, spike_reason="24h volume only $5"),
    ]

    async def fake_fetch() -> list:
        app.engine.source, app.engine.last_update = "test", time.time()
        return quotes

    app.engine.fetch_quotes = fake_fetch
    try:
        await app.evaluate()
        assert app.anchor == "ERG" and app.miners.coin == "ERG"       # baseline first
        assert app.boost() is not None and not app.boost().positive
        await app.evaluate()
        assert app.miners.coin == "ERG"                                # 1/2 confirmations
        await app.evaluate()
        assert app.miners.coin == "QUAI"                               # switched for real
        b = app.boost()
        assert b.anchor == "ERG" and round(b.gain_pct) == 50 and b.line() in app.status_text()
        top = [p.coin for p in app._top_view()[:3]]
        assert top == ["QUAI", "ERG", "XMR"], top                     # best per algorithm, no spikes
        msgs = " | ".join(m for _, m, _ in app.state.logs)
        for needle in ("Market fetch: 5 coins", "Anti-spike filter: ignoring 1", "confirming 1/2", "Switched ERG → QUAI"):
            assert needle in msgs, needle
    finally:
        await app.shutdown()


def test_demo_script() -> None:
    asyncio.run(_demo_script())


def test_donation_wallets() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    sources = "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py") if p.name != "test_core.py")
    assert "YOUR_" + "BTC" not in readme + sources, "placeholder left behind"
    for label, addr in donations.DEV_WALLETS.items():
        assert not donations.is_placeholder(addr) and addr == addr.strip(), label
        assert readme.count(addr) >= 2, f"README top/bottom missing {label}"
    assert set(donations.primary_wallets()) <= set(donations.visible_wallets())
    assert len(donations.primary_wallets()) == 6 and len(donations.visible_wallets()) == 12
    assert donations.dev_mining_wallet("ETC") == donations.PRIMARY_WALLETS["USDT (ERC20/BEP20) / ETH / BNB"]
    for coin in ("XMR", "ETC", "BTC", "LTC", "DOGE", "BCH", "DCR"):
        wallet = donations.dev_mining_wallet(coin)
        assert wallet and ":" not in wallet and wallet in "".join(donations.DEV_WALLETS.values()), coin
    assert donations.dev_mining_wallet("xmr") == donations.dev_mining_wallet("XMR")
    assert donations.dev_mining_wallet("RVN") is None and donations.dev_mining_wallet(None) is None
    funding = (root / ".github" / "FUNDING.yml").read_text(encoding="utf-8")
    assert donations.DONATE_URL in funding and 'id="support--donate"' in readme


if __name__ == "__main__":
    started = time.time()
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:  # noqa: BLE001 - report every failure
            failed += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed in {time.time() - started:.1f}s")
    sys.exit(1 if failed else 0)
