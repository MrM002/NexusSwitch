"""NexusSwitch - open-source crypto auto profit-switcher with a self-hosted
Telegram bot.  Entry point + orchestration of all background tasks.

    python main.py                  first run launches the setup wizard
    python main.py --demo           live market data + simulated miner
    python main.py --setup          re-run the wizard
    python main.py --link-telegram  (re)link the Telegram bot only
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Any

from rich.console import Console
from rich.live import Live

import config
import donations
import telegram_bot as tg
import ui
from miner_manager import MinerError, MinerManager
from profit_engine import Boost, CoinProfit, MarketDataError, ProfitEngine, SwitchDecider, pick_anchor

log = logging.getLogger("nexus.app")

CRASH_WINDOW = 600          # seconds
MAX_CRASHES_IN_WINDOW = 3   # then the coin is benched for COOLDOWN
COOLDOWN = 1800
_STATUS_FOR_REASON = {"no wallet": "No wallet", "no miner": "No miner", "miner not installed": "No miner"}


class NexusApp:
    """Owns the engine, the miner, the bot and the dashboard state."""

    def __init__(self, cfg: dict[str, Any], console: Console, demo: bool = False):
        self.cfg, self.console, self.demo = cfg, console, demo
        self.state = ui.DashboardState(demo=demo, telegram_enabled=False)
        self.engine = ProfitEngine(cfg)
        self.miners = MinerManager(cfg, demo=demo)
        sw = cfg["switching"]
        self.decider = SwitchDecider(sw["threshold_percent"], sw["confirmations"])
        self.ranked: list[CoinProfit] = []
        self.manual_until = 0.0
        self.cooldown: dict[str, float] = {}
        self.crashes: deque[float] = deque(maxlen=20)
        self._lock = asyncio.Lock()          # serialises every miner start/stop
        self._wake = asyncio.Event()         # forces an immediate market check
        self._bg: set[asyncio.Task] = set()
        self._idle_logged = False
        # boost vs. static mining (see profit_engine.Boost)
        self.anchor: str | None = None       # baseline coin, fixed for the session
        self.session_gain = 0.0              # USD earned above the baseline so far
        self._gain_ts = 0.0
        self._checks = 0
        self._flagged_sig: tuple | None = None
        self._pending_sig: tuple = ("", 0)
        self._demo_started = self._demo_reported = False
        if demo:
            self.state.demo_note = config.demo_rig_label(cfg)
            self.state.top_title = "Top 5 Coins Right Now — best coin per algorithm"
        self.bot: tg.TelegramBot | None = None
        t = cfg["telegram"]
        if t.get("enabled") and t.get("bot_token") and t.get("chat_id"):
            self.bot = tg.TelegramBot(t["bot_token"], t["chat_id"], self,
                                      on_activity=lambda m: self.state.log(f"📨 {m}", ui.C_ACCENT))
            self.state.telegram_enabled = True

    # ------------------------------------------------------------- utilities
    def notify(self, html_text: str, label: str) -> None:
        """Fire-and-forget Telegram alert (also recorded in the activity log)."""
        if not self.bot:
            return

        async def _send() -> None:
            ok = await self.bot.send(html_text)
            self.state.log(f"📨 Telegram alert {'sent' if ok else 'FAILED'}: {label}", ui.C_ACCENT if ok else ui.C_BAD)

        task = asyncio.create_task(_send())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _current(self) -> CoinProfit | None:
        return next((p for p in self.ranked if p.coin == self.miners.coin), None)

    def _annotate(self, ranked: list[CoinProfit]) -> list[CoinProfit]:
        """Set .status on every coin and return the ones we may auto-mine."""
        now = time.time()
        allow_loss = self.cfg["switching"]["mine_when_unprofitable"] or self.demo
        eligible = []
        for p in ranked:
            ok, reason = self.miners.can_mine(p.coin, p.quote.algo)
            if p.quote.spike:
                p.status = "Spike?"
            elif not ok:
                p.status = _STATUS_FOR_REASON.get(reason, "Config?")
            elif self.cooldown.get(p.coin, 0) > now:
                p.status = "Cooldown"
            else:
                mining = self.miners.running and p.coin == self.miners.coin
                p.status = ("Donating" if self.miners.donating else "Mining") if mining else "Ready"
                if p.profit_usd > 0 or allow_loss:
                    eligible.append(p)
        return eligible

    def boost(self) -> Boost | None:
        """Current coin vs. the static baseline coin (None while idle/donating)."""
        if not self.anchor or not self.miners.running or self.miners.donating:
            return None
        cur = self._current()
        base = next((p for p in self.ranked if p.coin == self.anchor), None)
        return Boost(self.anchor, base.profit_usd, cur.profit_usd) if cur and base else None

    def _after_fetch(self, eligible: list[CoinProfit]) -> None:
        """Book-keeping after every market refresh: session gain, baseline
        coin and the (change-only) market / anti-spike activity lines."""
        now = time.time()
        b = self.boost()
        if b and self._gain_ts:  # integrate (current - static) since the last check
            self.session_gain += b.gain_usd * (now - self._gain_ts) / 86400.0
        self._gain_ts = now
        if self._checks == 0:
            algos = len({p.quote.algo for p in self.ranked})
            self.state.log(f"📡 Market fetch: {len(self.ranked)} coins across {algos} algorithms "
                           f"via {self.engine.source}", ui.C_ACCENT)
        flagged = [p for p in self.ranked if p.quote.spike]
        sig = tuple(sorted(p.coin for p in flagged))
        if sig != self._flagged_sig:
            self._flagged_sig = sig
            if flagged:
                names = ", ".join(p.coin for p in flagged[:4]) + ("…" if len(flagged) > 4 else "")
                self.state.log(f"🛡 Anti-spike filter: ignoring {len(flagged)} coins ({names}) — "
                               f"e.g. {flagged[0].coin}: {flagged[0].quote.spike_reason}", ui.C_WARN)
        if eligible and (self.anchor is None or not any(p.coin == self.anchor for p in self.ranked)):
            self.anchor = pick_anchor(eligible)
            self.state.log(f"📌 Static-mining baseline for this session: {self.anchor}", ui.C_DIM)
        self._checks += 1

    def _top_view(self) -> list[CoinProfit]:
        """Rows for the dashboard table. The demo shows the best trustworthy
        coin of each algorithm first so the multi-algorithm ranking is visible."""
        if not self.demo:
            return self.ranked
        seen: set[str] = set()
        best = []
        for p in self.ranked:
            if not p.quote.spike and p.quote.algo not in seen:
                seen.add(p.quote.algo)
                best.append(p)
        return best + [p for p in self.ranked if p not in best]

    @staticmethod
    def _dedupe(ranked: list[CoinProfit]) -> list[CoinProfit]:
        seen, out = set(), []
        for p in ranked:  # a coin can be listed under two algorithms: keep its best
            if p.coin not in seen:
                seen.add(p.coin)
                out.append(p)
        return out

    # ---------------------------------------------------------------- market
    async def refresh_market(self) -> list[CoinProfit]:
        quotes = await self.engine.fetch_quotes()
        self.ranked = self._dedupe(self.engine.rank(quotes))
        self.state.source, self.state.last_update = self.engine.source, self.engine.last_update
        self.state.market_error = self.engine.last_error
        return self._annotate(self.ranked)

    async def _switch_to(self, target: CoinProfit, gain: float, manual: bool = False) -> None:
        """Caller must hold self._lock."""
        old = self.miners.coin
        plan = await self.miners.start(target.coin, target.quote.algo)
        self.decider.reset()
        self._idle_logged = False
        if manual:
            msg = f"🕹 Manual switch → {target.coin} via {plan.label} (${target.profit_usd:.2f}/day)"
        elif old and old != target.coin:
            msg = f"🔄 Switched {old} → {target.coin} (+{gain:.1f}% profit) via {plan.label}"
        else:
            msg = f"⛏ Started mining {target.coin} via {plan.label} (${target.profit_usd:.2f}/day)"
        self.state.log(msg, ui.C_GOOD)
        log.info(msg)
        self.notify(tg.switch_text(old if old != target.coin else None, target.coin, gain,
                                   target.profit_usd, manual), f"switch to {target.coin}")
        if self.demo and old and old != target.coin and not manual and not self._demo_reported:
            self._demo_reported = True  # demo: show the periodic report right away
            if self.bot:
                self.notify(tg.top_text(self.ranked, 3, "Top 3 Most Profitable Coins right now"), "Top-3 market report")
            else:
                self.state.log("📨 Telegram not linked — `python main.py --link-telegram` sends this alert "
                               "+ a Top-3 report to your phone", ui.C_DIM)

    async def evaluate(self) -> None:
        """One market check: refresh data, then start / switch / idle."""
        try:
            eligible = await self.refresh_market()
        except MarketDataError as e:
            self.state.market_error = str(e)
            self.state.log(f"⚠ Market data unavailable: {str(e)[:90]}", ui.C_WARN)
            log.warning("market data error: %s", e)
            return
        self._after_fetch(eligible)
        async with self._lock:
            if self.miners.donating:
                return  # never interrupt a donation window
            current = self.miners.coin if self.miners.running else None
            sw = self.cfg["switching"]
            if current and time.time() < self.manual_until:
                self.state.pending = ""
                return  # user forced a coin from Telegram
            cur_ok = any(p.coin == current for p in eligible)
            if current and cur_ok and self.miners.uptime < sw["min_runtime_minutes"] * 60:
                self.state.pending = "holding (min runtime)"
                return
            decision = self.decider.decide(current, eligible)
            cand, streak = self.decider.pending
            self.state.pending = f"{cand} +{decision.gain_pct:.1f}% ({streak}/{self.decider.confirmations})" if cand else ""
            if cand and (cand, streak) != self._pending_sig:
                self.state.log(f"⚖ {cand} beats {current} by +{decision.gain_pct:.1f}% (threshold "
                               f"{self.decider.threshold_percent:g}%) — confirming {streak}/{self.decider.confirmations}",
                               ui.C_ACCENT)
            self._pending_sig = (cand, streak)
            if self.demo and current is None and decision.target and not self._demo_started:
                # Demo script: begin on the static baseline coin so a real,
                # data-driven switch can be watched within the first minute.
                self._demo_started = True
                base = next((p for p in eligible if p.coin == self.anchor), None)
                if base and base.coin != decision.target.coin:
                    decision.target = base
                    self.state.log(f"🎬 Demo: starting on the static baseline coin {base.coin} "
                                   "to show a live switch", ui.C_DIM)
            if decision.target is None:
                if current and not cur_ok:  # nothing left worth mining
                    await self.miners.stop()
                    self.state.log("⏸ Nothing profitable/eligible right now — miner stopped", ui.C_WARN)
                elif not current and not self._idle_logged:
                    self._idle_logged = True
                    why = "add wallets + miner paths (python main.py --setup)" if not self.cfg["coins"] \
                        else "no configured coin is profitable/eligible right now"
                    self.state.log(f"⏸ Idle — {why}", ui.C_WARN)
                return
            try:
                await self._switch_to(decision.target, decision.gain_pct)
            except MinerError as e:
                self.cooldown[decision.target.coin] = time.time() + COOLDOWN
                self.state.log(f"✖ Could not start {decision.target.coin}: {e} (benched 30 min)", ui.C_BAD)
                log.error("start failed for %s: %s", decision.target.coin, e)
        self._annotate(self.ranked)  # refresh the Status column after acting

    async def market_loop(self) -> None:
        while True:
            # the demo runs its first checks quickly so the switch logic is visible
            interval = 20 if self.demo and self._checks < 2 else self.cfg["switching"]["check_interval_seconds"]
            try:
                await self.evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("market loop error")
                self.state.log("✖ Unexpected error in market loop (see logs/nexusswitch.log)", ui.C_BAD)
            self.state.next_check = time.time() + interval
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # -------------------------------------------------------------- watchdog
    async def watchdog_loop(self) -> None:
        """Detect a dead miner and bring it back (with crash-loop protection)."""
        while True:
            await asyncio.sleep(3)
            code = self.miners.exit_code()
            if code is None:
                continue
            async with self._lock:
                code = self.miners.exit_code()
                if code is None:
                    continue
                coin, algo, label = self.miners.coin, self.miners.algo, self.miners.miner_label
                now = time.time()
                self.crashes.append(now)
                recent = sum(1 for t in self.crashes if now - t < CRASH_WINDOW)
                await self.miners.stop()
                if recent >= MAX_CRASHES_IN_WINDOW:
                    self.cooldown[coin] = now + COOLDOWN
                    self.crashes.clear()
                    self.state.log(f"✖ {label} keeps crashing on {coin} — benched 30 min, picking another coin", ui.C_BAD)
                    self.notify(f"🛑 <b>{tg.esc(label)}</b> crashed {recent}× on <b>{tg.esc(coin)}</b>. "
                                "Benched for 30 min — trying the next best coin.", "crash loop")
                    self._wake.set()
                    continue
                await asyncio.sleep(min(5 * recent, 30))
                restarted = True
                try:
                    await self.miners.start(coin, algo)
                except MinerError as e:
                    restarted = False
                    log.error("restart failed: %s", e)
                    self._wake.set()
                self.state.log(f"⚠ {label} crashed on {coin} (exit {code}) — "
                               f"{'auto-restarted' if restarted else 'restart failed'}",
                               ui.C_WARN if restarted else ui.C_BAD)
                log.warning("miner crash: %s on %s exit=%s restarted=%s", label, coin, code, restarted)
                self.notify(tg.crash_text(coin, label, code, restarted), "miner crash")

    # --------------------------------------------------------------- reports
    async def report_loop(self) -> None:
        hours = self.cfg["telegram"]["report_interval_hours"]
        while True:
            await asyncio.sleep(hours * 3600)
            if self.bot and self.ranked:
                self.notify(tg.top_text(self.ranked, 3, "Top 3 Most Profitable Coins right now"),
                            f"{hours:g}h market report")

    # -------------------------------------------------------------- donation
    async def donation_loop(self) -> None:
        """Transparent, opt-in dev donation: mine to the developer wallet for
        `percent` of each cycle. Skipped when no dev wallet exists for the coin."""
        dc = self.cfg["donation_cycle"]
        if self.demo or not dc["enabled"] or dc["percent"] <= 0:
            return
        window = dc["cycle_minutes"] * 60 * dc["percent"] / 100.0
        while True:
            await asyncio.sleep(dc["cycle_minutes"] * 60 - window)
            async with self._lock:
                coin, algo = self.miners.coin, self.miners.algo
                wallet = donations.dev_mining_wallet(coin) if coin and self.miners.running else None
                if not wallet:
                    continue
                try:
                    await self.miners.start(coin, algo, wallet=wallet, donating=True)
                except MinerError as e:
                    log.warning("donation cycle skipped: %s", e)
                    self._wake.set()
                    continue
                self.state.log(f"💖 Donation cycle: mining {coin} for the developer for "
                               f"{window / 60:.0f} min — thank you!", ui.C_PRIMARY)
            try:
                await asyncio.sleep(window)
            finally:
                async with self._lock:
                    if self.miners.donating:
                        try:
                            await self.miners.start(coin, algo)
                            self.state.log(f"⛏ Donation cycle finished — back to your {coin} wallet", ui.C_GOOD)
                        except MinerError:
                            await self.miners.stop()
                            self._wake.set()

    # ------------------------------------------------------- Telegram hooks
    def status_text(self) -> str:
        cur = self._current()
        if not self.miners.running or not self.miners.coin:
            body = "⏸ <b>Idle</b> — no miner running."
        else:
            profit = f"${cur.profit_usd:.2f}/day" if cur else "n/a"
            body = (f"⛏ Mining <b>{tg.esc(self.miners.coin)}</b> with {tg.esc(self.miners.miner_label)}\n"
                    f"⏱ Miner uptime: {ui.fmt_duration(self.miners.uptime)}\n"
                    f"💰 Est. profit: <b>{profit}</b>")
            b = self.boost()
            if b:
                body += f"\n📈 {tg.esc(b.line())}"
                if abs(self.session_gain) >= 0.0001:
                    sign = "+" if self.session_gain >= 0 else "-"
                    body += f"\n🧮 Session gain vs. static: {sign}${abs(self.session_gain):.4f}"
        mode = self.state.mode + (" (demo)" if self.demo else "")
        return (f"📟 <b>NexusSwitch status</b>\n{body}\n"
                f"🕒 App uptime: {ui.fmt_duration(time.time() - self.state.started_at)}\n"
                f"⚙️ Mode: {mode}")

    async def live_top_text(self, n: int) -> str:
        if time.time() - self.engine.last_update > 30:
            try:
                await self.refresh_market()
            except MarketDataError as e:
                if not self.ranked:
                    return f"❌ Market data unavailable: {tg.esc(str(e)[:150])}"
        return tg.top_text(self.ranked, n, f"Top {n} Most Profitable Coins right now")

    async def request_switch(self, coin: str) -> str:
        target = next((p for p in self.ranked if p.coin == coin), None)
        if target is None:
            known = ", ".join(p.coin for p in self.ranked[:12]) or "none yet"
            return f"❌ <b>{tg.esc(coin)}</b> is not in the current market list.\nAvailable: {tg.esc(known)}"
        ok, reason = self.miners.can_mine(coin, target.quote.algo)
        if not ok:
            return f"❌ Cannot mine <b>{tg.esc(coin)}</b>: {tg.esc(reason)}. Add it to config.json first."
        async with self._lock:
            if self.miners.donating:
                return "💖 A short donation cycle is running — try again in a few minutes."
            try:
                await self._switch_to(target, 0.0, manual=True)
            except MinerError as e:
                return f"❌ Failed to start {tg.esc(coin)}: {tg.esc(e)}"
            hold = self.cfg["switching"]["manual_hold_minutes"]
            self.manual_until = time.time() + hold * 60
        self._annotate(self.ranked)
        return (f"✅ Now mining <b>{tg.esc(coin)}</b> (${target.profit_usd:.2f}/day).\n"
                f"Auto-switching is paused for {hold:g} min — send /auto to resume now.")

    async def resume_auto(self) -> str:
        self.manual_until = 0.0
        self._wake.set()
        self.state.log("▶ Auto-switching resumed from Telegram", ui.C_GOOD)
        return "▶️ Auto-switching resumed."

    # ---------------------------------------------------------------- UI/run
    def _sync_state(self) -> None:
        s, m = self.state, self.miners
        s.miner_running, s.miner_label = m.running, m.miner_label
        s.coin = m.coin if m.running else None
        cur = self._current() if m.running else None
        s.algo_name = cur.algo_name if cur else ""
        s.profit_usd = cur.profit_usd if cur else None
        s.top = self._top_view()
        s.boost = self.boost()
        if m.donating:
            s.mode = "DONATING"
        elif not m.running:
            s.mode = "IDLE"
        else:
            s.mode = "MANUAL" if time.time() < self.manual_until else "AUTO"
        if self.bot:
            s.telegram_connected, s.telegram_name, s.telegram_sent = self.bot.connected, self.bot.username, self.bot.sent

    async def ui_loop(self) -> None:
        with Live(console=self.console, screen=True, auto_refresh=False) as live:
            while True:
                self._sync_state()
                live.update(ui.build_dashboard(self.state, self.console.size.height), refresh=True)
                await asyncio.sleep(0.5)

    async def run(self, once: bool = False) -> None:
        if MinerManager.cleanup_orphan():
            self.state.log("🧹 Cleaned up a miner left over from a previous run", ui.C_WARN)
        self.state.log(f"NexusSwitch started{' in DEMO mode (simulated miner)' if self.demo else ''}", ui.C_ACCENT)
        dc = self.cfg["donation_cycle"]
        if dc["enabled"] and not self.demo:
            self.state.log(f"💖 Donation cycle ON ({dc['percent']:g}%) — thank you! Disable any time in config.json", ui.C_PRIMARY)
        else:
            self.state.log("💖 Like the project? See the donation wallets below or send /donate to your bot", ui.C_PRIMARY)
        try:
            if once:
                for _ in range(3 if self.demo else 1):  # demo: play the switch script
                    await self.evaluate()
                self._sync_state()
                ui.render_snapshot(self.state, self.console)
                return
            coros = [self.market_loop(), self.watchdog_loop(), self.donation_loop(), self.ui_loop()]
            if self.bot:
                coros += [self.bot.run(), self.report_loop()]
            tasks = [asyncio.create_task(c) for c in coros]
            try:
                # the UI / market / watchdog loops never return: this only ends
                # on Ctrl+C (cancellation) or if one of them raises
                done, _ = await asyncio.wait(tasks[:2] + tasks[3:4], return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    t.result()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        was_mining = self.miners.running
        await self.miners.stop()
        if self.bot:
            if was_mining and self.bot.connected:
                await self.bot.send("🛑 NexusSwitch stopped — miner shut down cleanly.")
            await self.bot.close()
        await self.engine.close()


# ---------------------------------------------------------------------- CLI
def _setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.LOG_DIR / "nexusswitch.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # httpx logs full request URLs at INFO - those contain the bot token
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _dummy_miner() -> None:
    """`--dummy-miner`: stand-in process used by --demo. Does nothing, forever."""
    print("NexusSwitch simulated miner running (no hashing is performed).", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main() -> int:
    if "--dummy-miner" in sys.argv:
        _dummy_miner()
        return 0
    parser = argparse.ArgumentParser(prog="nexusswitch", description=__doc__.split("\n")[0])
    parser.add_argument("--demo", action="store_true", help="live market data with a simulated miner (no config needed)")
    parser.add_argument("--setup", action="store_true", help="run the interactive setup wizard")
    parser.add_argument("--link-telegram", action="store_true", help="(re)link the Telegram bot only")
    parser.add_argument("--once", action="store_true", help="fetch the market once, print the dashboard and exit")
    parser.add_argument("--version", action="version", version=f"NexusSwitch {config.APP_VERSION}")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):  # box-drawing + emoji on legacy Windows codepages
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    console = Console()
    _setup_logging()

    import setup_wizard  # imported late: needs an interactive terminal only when used

    try:
        if args.link_telegram:
            cfg = config.load_config() if config.config_exists() else config.default_config()
            if setup_wizard.link_telegram(console, cfg):
                config.save_config(cfg)
                console.print(f"[{ui.C_GOOD}]✔ Saved. Start the dashboard with: python main.py[/]")
            return 0

        if args.demo:
            cfg = config.demo_config()
            if config.config_exists():  # reuse a linked bot so alerts can be tried in demo
                try:
                    cfg["telegram"] = config.load_config()["telegram"]
                except config.ConfigError:
                    pass
        else:
            if args.setup or not config.config_exists():
                if not sys.stdin.isatty():
                    console.print(f"[{ui.C_BAD}]No config.json found. Run `python main.py` in an interactive "
                                  "terminal to start the setup wizard, or try `python main.py --demo`.[/]")
                    return 2
                existing = None
                if config.config_exists():
                    try:
                        existing = config.load_config()
                    except config.ConfigError:
                        existing = None
                setup_wizard.run_wizard(console, existing)
                if args.setup:
                    console.print("Start mining with: [bold cyan]python main.py[/]")
                    return 0
                console.input(f"\n[{ui.C_ACCENT}]Press Enter to launch the live dashboard…[/]")
            cfg = config.load_config()
    except setup_wizard.WizardCancelled:
        console.print(f"\n[{ui.C_WARN}]Setup cancelled — nothing was saved.[/]")
        return 1
    except config.ConfigError as e:
        console.print(f"[{ui.C_BAD}]Config error: {e}[/]")
        return 2

    app = NexusApp(cfg, console, demo=args.demo)
    try:
        asyncio.run(app.run(once=args.once))
    except KeyboardInterrupt:
        pass
    console.print(f"[{ui.C_PRIMARY}]👋 NexusSwitch stopped — miner shut down cleanly.[/]")
    console.print(ui.donation_box("💖 Thanks for using NexusSwitch — tips keep it alive"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
