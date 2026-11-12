"""Rich terminal UI: banner, donation box and the live dashboard layout."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import donations
from config import APP_VERSION

# cyberpunk palette
C_PRIMARY = "bright_magenta"
C_ACCENT = "bright_cyan"
C_GOOD = "bright_green"
C_WARN = "yellow"
C_BAD = "bright_red"
C_DIM = "grey58"

BANNER = r"""
 ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗  ███████╗██╗    ██╗██╗████████╗ ██████╗██╗  ██╗
 ████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝  ██╔════╝██║    ██║██║╚══██╔══╝██╔════╝██║  ██║
 ██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗  ███████╗██║ █╗ ██║██║   ██║   ██║     ███████║
 ██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║  ╚════██║██║███╗██║██║   ██║   ██║     ██╔══██║
 ██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║  ███████║╚███╔███╔╝██║   ██║   ╚██████╗██║  ██║
 ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝  ╚══════╝ ╚══╝╚══╝ ╚═╝   ╚═╝    ╚═════╝╚═╝  ╚═╝
"""
TAGLINE = "Open-Source Crypto Auto Profit-Switcher  ·  Self-Hosted Telegram Bot  ·  Zero Server Costs"


@dataclass
class DashboardState:
    """Everything the dashboard renders. The app mutates it, the UI reads it."""
    started_at: float = field(default_factory=time.time)
    demo: bool = False
    miner_label: str = "-"
    miner_running: bool = False
    coin: str | None = None
    algo_name: str = ""
    profit_usd: float | None = None
    boost: Any = None                  # profit_engine.Boost or None
    demo_note: str = ""
    top_title: str = "Top 5 Coins Right Now (for your hardware)"
    revenue_usd: float | None = None
    mode: str = "AUTO"                 # AUTO / MANUAL / DONATING / IDLE
    pending: str = ""                  # e.g. "ZEPH +9.1% (1/2)"
    telegram_enabled: bool = False
    telegram_connected: bool = False
    telegram_name: str = ""
    telegram_sent: int = 0
    source: str = "-"
    last_update: float = 0.0
    next_check: float = 0.0
    market_error: str = ""
    top: list = field(default_factory=list)        # list[CoinProfit] with .status
    logs: deque = field(default_factory=lambda: deque(maxlen=60))

    def log(self, message: str, style: str = "") -> None:
        self.logs.append((time.strftime("%H:%M:%S"), message, style))


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


def fmt_hashrate(h: float) -> str:
    for unit, div in (("GH/s", 1e9), ("MH/s", 1e6), ("kH/s", 1e3)):
        if h >= div:
            return f"{h / div:.2f} {unit}"
    return f"{h:.0f} H/s"


def fmt_usd(v: float | None) -> Text:
    if v is None:
        return Text("—", style=C_DIM)
    return Text(f"${v:,.2f}", style=C_GOOD if v > 0 else C_BAD if v < 0 else C_DIM)


def banner() -> RenderableType:
    art = Text(BANNER.strip("\n"), style=f"bold {C_PRIMARY}", no_wrap=True, overflow="crop")
    return Group(Align.center(art), Align.center(Text(TAGLINE, style=C_ACCENT)),
                 Align.center(Text(f"v{APP_VERSION}", style=C_DIM)))


def donation_box(title: str = "💖 Support the Dev (Donation)") -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=f"bold {C_ACCENT}", justify="right")
    grid.add_column(style="white", overflow="fold")
    for label, addr in donations.primary_wallets().items():
        grid.add_row(label, addr)
    note = Text("NexusSwitch is free forever. A small tip keeps the updates coming!\n"
                f"More coins (DOGE, BCH, ZEC, DCR, XNO, TON): {donations.DONATE_URL}", style=C_DIM)
    return Panel(Group(grid, Text(""), note), title=title, border_style=C_PRIMARY,
                 box=box.DOUBLE, padding=(1, 2))


# ------------------------------------------------------------------ dashboard
def _header(s: DashboardState) -> Panel:
    title = Text.assemble(
        ("⚡ NEXUS", f"bold {C_PRIMARY}"), ("SWITCH", f"bold {C_ACCENT}"), (f"  v{APP_VERSION}", C_DIM),
        ("   DEMO MODE" if s.demo else "", f"bold {C_WARN}"),
    )
    if s.telegram_connected:
        tg = Text(f"● Connected @{s.telegram_name}", style=C_GOOD)
    elif s.telegram_enabled:
        tg = Text("● Offline (retrying)", style=C_BAD)
    else:
        tg = Text("○ Disabled", style=C_DIM)
    mode_style = {"AUTO": C_GOOD, "MANUAL": C_WARN, "DONATING": C_PRIMARY}.get(s.mode, C_DIM)
    coin = Text(s.coin or "—", style=f"bold {C_ACCENT}")
    if s.coin and s.algo_name:
        coin.append(f"  ({s.algo_name})", style=C_DIM)
    miner = Text("● " if s.miner_running else "○ ", style=C_GOOD if s.miner_running else C_DIM)
    miner.append(s.miner_label if s.miner_running else "stopped", style="bold white" if s.miner_running else C_DIM)

    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in range(3):
        grid.add_column(style=C_DIM, no_wrap=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_row("Uptime", Text(fmt_duration(time.time() - s.started_at), style="white"),
                 "Active Miner", miner, "Telegram Bot", tg)
    grid.add_row("Current Coin", coin, "Est. Daily Profit", fmt_usd(s.profit_usd),
                 "Mode", Text(s.mode + (f"  → {s.pending}" if s.pending else ""), style=mode_style))
    if s.boost is None:
        boost = Text("vs. Static Mining: —  (shown once a coin is being mined)", style=C_DIM)
    else:
        boost = Text(s.boost.line(), style=f"bold {C_GOOD}" if s.boost.positive else C_DIM)
    boost.no_wrap, boost.overflow = True, "ellipsis"
    sub = Text(s.demo_note, style=C_WARN) if s.demo and s.demo_note else None
    return Panel(Group(grid, boost), title=title, title_align="left", subtitle=sub, subtitle_align="right",
                 border_style=C_PRIMARY, box=box.HEAVY)


_STATUS_STYLE = {"Mining": f"bold {C_GOOD}", "Donating": f"bold {C_PRIMARY}", "Ready": C_ACCENT, "Spike?": C_WARN}


def _market(s: DashboardState) -> Panel:
    t = Table(expand=True, box=box.SIMPLE_HEAD, header_style=f"bold {C_ACCENT}", pad_edge=False)
    t.add_column("#", width=2, style=C_DIM)
    t.add_column("Coin", style="bold white", no_wrap=True)
    t.add_column("Algorithm", no_wrap=True)
    t.add_column("Reward/24h", justify="right", no_wrap=True)
    t.add_column("Profit/24h", justify="right", no_wrap=True)
    t.add_column("Diff Trend", justify="right", no_wrap=True)
    t.add_column("Status", no_wrap=True)
    for i, p in enumerate(s.top[:5], 1):
        trend = p.quote.diff_trend_pct
        if trend is None:
            trend_t = Text("—", style=C_DIM)
        elif abs(trend) < 0.5:
            trend_t = Text(f"● {trend:+.1f}%", style=C_DIM)
        else:  # falling difficulty is good for miners
            trend_t = Text(f"{'▲' if trend > 0 else '▼'} {trend:+.1f}%", style=C_BAD if trend > 0 else C_GOOD)
        status = p.status or "—"
        t.add_row(str(i), p.coin, p.algo_name, fmt_usd(p.revenue_usd), fmt_usd(p.profit_usd), trend_t,
                  Text(status, style=_STATUS_STYLE.get(status, C_DIM)))
    if not s.top:
        t.add_row("", Text(s.market_error or "Fetching live market data…", style=C_WARN if s.market_error else C_DIM),
                  "", "", "", "", "")
    age = f"updated {fmt_duration(time.time() - s.last_update)} ago" if s.last_update else "waiting for data"
    nxt = f" · next check in {fmt_duration(s.next_check - time.time())}" if s.next_check > time.time() else ""
    sub = Text(f"{s.source} · {age}{nxt}", style=C_DIM)
    if s.market_error and s.top:
        sub.append(f" · ⚠ {s.market_error[:60]}", style=C_WARN)
    return Panel(t, title=Text(f"📈 {s.top_title}", style=f"bold {C_ACCENT}"),
                 title_align="left", subtitle=sub, subtitle_align="right", border_style=C_ACCENT)


def _activity(s: DashboardState, rows: int) -> Panel:
    lines = Text(no_wrap=True, overflow="ellipsis")
    entries = list(s.logs)[-max(1, rows):]
    for i, (ts, msg, style) in enumerate(entries):
        lines.append(f"{ts} ", style=C_DIM)
        lines.append(msg + ("\n" if i < len(entries) - 1 else ""), style=style or "white")
    if not entries:
        lines.append("No activity yet…", style=C_DIM)
    sub = Text(f"Telegram alerts sent: {s.telegram_sent}", style=C_DIM) if s.telegram_enabled else None
    return Panel(lines, title=Text("📜 Recent Activity", style="bold white"), title_align="left",
                 subtitle=sub, subtitle_align="right", border_style=C_DIM)


def _footer() -> Panel:
    wallets = donations.primary_wallets()
    # rotate through the primary wallets so every address gets shown in full
    label, addr = list(wallets.items())[int(time.time() // 8) % len(wallets)]
    line = Text.assemble(("💖 Donate  ", f"bold {C_PRIMARY}"), (f"{label}: ", f"bold {C_ACCENT}"), (addr, "white"))
    line.no_wrap, line.overflow = True, "ellipsis"
    return Panel(Align.center(line), border_style=C_PRIMARY, box=box.ROUNDED,
                 title=Text("Enjoying NexusSwitch? Support the developer", style=f"bold {C_PRIMARY}"),
                 subtitle=Text("more coins: /donate on Telegram · Ctrl+C to quit", style=C_DIM), subtitle_align="right")


def build_dashboard(s: DashboardState, height: int = 30) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(_header(s), name="header", size=5),
        Layout(_market(s), name="market", size=11),
        Layout(name="activity", ratio=1, minimum_size=4),
        Layout(_footer(), name="footer", size=3),
    )
    layout["activity"].update(_activity(s, rows=height - 5 - 11 - 3 - 2))
    return layout


def render_snapshot(s: DashboardState, console: Any) -> None:
    """Print one static frame (used by --once and for tests)."""
    console.print(build_dashboard(s, height=console.size.height))
