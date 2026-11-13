"""First-run interactive setup wizard (Rich + Questionary)."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import questionary
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
import ui
from miner_manager import ALGO_MINERS, MINERS, detect_miner
from telegram_bot import TelegramError, call_sync, valid_token, wait_for_start

STYLE = questionary.Style([
    ("qmark", "fg:#ff00ff bold"), ("question", "bold"), ("answer", "fg:#00ffff bold"),
    ("pointer", "fg:#ff00ff bold"), ("highlighted", "fg:#00ffff bold"),
    ("selected", "fg:#00ff9f"), ("instruction", "fg:#808080"),
])

CUSTOM = "Custom / not listed (enter hashrates manually)"
NONE = "None / skip"

# Approximate stock-ish hashrates. They only seed the config - fine-tune them
# to your real numbers for accurate switching.   model -> (RandomX H/s, watts)
CPU_PRESETS: dict[str, tuple[float, float]] = {
    "AMD Ryzen 5 5600X": (7000, 75), "AMD Ryzen 7 5800X": (9500, 105),
    "AMD Ryzen 9 5900X": (13500, 125), "AMD Ryzen 9 5950X": (17500, 140),
    "AMD Ryzen 9 7950X": (22000, 160), "AMD Ryzen 9 9950X": (24000, 165),
    "Intel Core i5-12400": (5500, 70), "Intel Core i7-12700K": (9500, 125),
    "Intel Core i9-13900K": (14500, 190),
}
# model -> ({algo: hashrate in H/s or Sol/s}, watts) for ONE card
GPU_PRESETS: dict[str, tuple[dict[str, float], float]] = {
    "NVIDIA RTX 3060":      ({"kawpow": 23e6, "autolykos2": 115e6, "etchash": 49e6, "zhash": 62, "beamhash3": 30, "zelhash": 45}, 115),
    "NVIDIA RTX 3060 Ti":   ({"kawpow": 30e6, "autolykos2": 170e6, "etchash": 60e6, "zhash": 90, "beamhash3": 37, "zelhash": 60}, 130),
    "NVIDIA RTX 3070":      ({"kawpow": 30e6, "autolykos2": 175e6, "etchash": 61e6, "zhash": 95, "beamhash3": 40, "zelhash": 65}, 130),
    "NVIDIA RTX 3080":      ({"kawpow": 48e6, "autolykos2": 270e6, "etchash": 97e6, "zhash": 135, "beamhash3": 55, "zelhash": 90}, 230),
    "NVIDIA RTX 3090":      ({"kawpow": 58e6, "autolykos2": 290e6, "etchash": 120e6, "zhash": 155, "beamhash3": 70, "zelhash": 100}, 290),
    "NVIDIA RTX 4070":      ({"kawpow": 32e6, "autolykos2": 185e6, "etchash": 60e6, "zhash": 100, "beamhash3": 45, "zelhash": 72}, 130),
    "NVIDIA RTX 4070 Ti":   ({"kawpow": 40e6, "autolykos2": 230e6, "etchash": 66e6, "zhash": 125, "beamhash3": 55, "zelhash": 90}, 180),
    "NVIDIA RTX 4080":      ({"kawpow": 55e6, "autolykos2": 300e6, "etchash": 95e6, "zhash": 160, "beamhash3": 70, "zelhash": 115}, 230),
    "NVIDIA RTX 4090":      ({"kawpow": 75e6, "autolykos2": 390e6, "etchash": 130e6, "zhash": 215, "beamhash3": 95, "zelhash": 150}, 320),
    "AMD RX 6600 XT":       ({"kawpow": 15e6, "autolykos2": 70e6, "etchash": 32e6, "zhash": 45, "beamhash3": 20, "zelhash": 30}, 80),
    "AMD RX 6700 XT":       ({"kawpow": 24e6, "autolykos2": 110e6, "etchash": 47e6, "zhash": 60, "beamhash3": 28, "zelhash": 43}, 120),
    "AMD RX 6800 XT":       ({"kawpow": 33e6, "autolykos2": 170e6, "etchash": 63e6, "zhash": 90, "beamhash3": 42, "zelhash": 62}, 150),
    "AMD RX 7900 XTX":      ({"kawpow": 50e6, "autolykos2": 260e6, "etchash": 95e6, "zhash": 130, "beamhash3": 60, "zelhash": 95}, 250),
}

# (ticker, name, algorithm) offered in the coin picker
POPULAR_COINS: list[tuple[str, str, str]] = [
    ("XMR", "Monero", "randomx"), ("ZEPH", "Zephyr", "randomx"),
    ("RVN", "Ravencoin", "kawpow"), ("QUAI", "Quai", "kawpow"), ("XNA", "Neurai", "kawpow"),
    ("ERG", "Ergo", "autolykos2"), ("ETC", "Ethereum Classic", "etchash"),
    ("BEAM", "Beam", "beamhash3"), ("ZEC", "Zcash", "equihash"),
]


class WizardCancelled(Exception):
    pass


def _ask(question: Any) -> Any:
    answer = question.ask()
    if answer is None:  # Ctrl+C
        raise WizardCancelled()
    return answer


def _number(prompt: str, default: float = 0.0, minimum: float = 0.0) -> float:
    def check(text: str) -> bool | str:
        try:
            return float(text) >= minimum or f"Must be ≥ {minimum:g}"
        except ValueError:
            return "Please enter a number"
    return float(_ask(questionary.text(prompt, default=f"{default:g}", validate=check, style=STYLE)))


def _step(console: Console, n: int, total: int, title: str) -> None:
    console.print()
    console.rule(Text(f" Step {n}/{total} · {title} ", style=f"bold {ui.C_ACCENT}"), style=ui.C_PRIMARY)


# ------------------------------------------------------------------ Telegram
def link_telegram(console: Console, cfg: dict[str, Any]) -> bool:
    """Guide the user through BotFather, capture chat_id, send a test message."""
    steps = Table.grid(padding=(0, 1))
    steps.add_column(style=f"bold {ui.C_PRIMARY}")
    steps.add_column()
    steps.add_row("1.", "Open [bold cyan]@BotFather[/] on Telegram")
    steps.add_row("2.", "Send [bold cyan]/newbot[/] and follow the prompts (pick any name)")
    steps.add_row("3.", "Paste your [bold cyan]Bot Token[/] here (looks like 123456789:AA…)")
    console.print(Panel(steps, title="🤖 Create your own free Telegram bot", border_style=ui.C_ACCENT,
                        subtitle="The token stays on this PC in config.json", subtitle_align="right"))
    while True:
        token = _ask(questionary.password(
            "Bot Token:", style=STYLE,
            validate=lambda t: valid_token(t) or "That does not look like a bot token (123456789:AA…)",
        )).strip()
        try:
            with console.status("Checking token with Telegram…", spinner="dots"):
                me = call_sync(token, "getMe")
            break
        except TelegramError as e:
            console.print(f"[{ui.C_BAD}]✖ Telegram rejected the token: {e}[/]")
            if not _ask(questionary.confirm("Try again?", default=True, style=STYLE)):
                return False
    username = me.get("username", "your_bot")
    console.print(f"[{ui.C_GOOD}]✔ Token OK — bot is [bold]@{username}[/][/]")
    console.print(Panel(
        f"Now send [bold cyan]/start[/] to your new bot on Telegram to link your account!\n"
        f"👉  [link=https://t.me/{username}]https://t.me/{username}[/link]",
        border_style=ui.C_PRIMARY))
    while True:
        try:
            with console.status("Waiting for your /start message… (Ctrl+C to cancel)", spinner="earth"):
                chat = wait_for_start(token, timeout=300)
        except KeyboardInterrupt:
            raise WizardCancelled() from None
        except TelegramError as e:
            console.print(f"[{ui.C_BAD}]✖ {e}[/]")
            return False
        if chat:
            break
        if not _ask(questionary.confirm("No /start received in 5 minutes. Keep waiting?", default=True, style=STYLE)):
            return False
    chat_id = int(chat["id"])
    who = chat.get("first_name") or chat.get("username") or str(chat_id)
    try:
        call_sync(token, "sendMessage", chat_id=chat_id, parse_mode="HTML",
                  text="🚀 <b>NexusSwitch Linked Successfully!</b>\n"
                       "You will get switch alerts, crash alerts and market reports here.\n"
                       "Commands: /status /top /switch /donate")
    except TelegramError as e:
        console.print(f"[{ui.C_WARN}]⚠ Linked, but the test message failed: {e}[/]")
    cfg["telegram"].update({"enabled": True, "bot_token": token, "chat_id": chat_id})
    console.print(f"[{ui.C_GOOD}]✔ Linked to {who} (chat_id {chat_id}) — check Telegram for the confirmation![/]")
    return True


# ------------------------------------------------------------------ hardware
def _hardware(console: Console, cfg: dict[str, Any]) -> None:
    rates: dict[str, float] = {}
    watts: dict[str, float] = {}
    cpu = _ask(questionary.select("CPU for RandomX mining:", choices=[*CPU_PRESETS, CUSTOM, NONE], style=STYLE))
    threads = os.cpu_count() or 4
    if cpu != NONE:
        threads = int(_number("CPU threads to mine with:", default=threads, minimum=1))
        if cpu == CUSTOM:
            rates["randomx"] = _number("RandomX hashrate (H/s):", default=threads * 450)
            watts["randomx"] = _number("CPU power draw while mining (watts):", default=100)
        else:
            rates["randomx"], watts["randomx"] = CPU_PRESETS[cpu]
    cfg["hardware"]["cpu"] = {"model": "" if cpu in (CUSTOM, NONE) else cpu, "threads": threads if cpu != NONE else 0}

    gpu = _ask(questionary.select("GPU model:", choices=[*GPU_PRESETS, CUSTOM, NONE], style=STYLE))
    count = 0
    if gpu != NONE:
        count = int(_number("How many of these GPUs?", default=1, minimum=1))
        if gpu in GPU_PRESETS:
            per_card, card_watts = GPU_PRESETS[gpu]
            for algo, hr in per_card.items():
                rates[algo], watts[algo] = hr * count, card_watts * count
    cfg["hardware"]["gpu"] = {"model": "" if gpu in (CUSTOM, NONE) else gpu, "count": count}

    def show() -> None:
        t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {ui.C_ACCENT}")
        t.add_column("Algorithm"); t.add_column("Hashrate", justify="right"); t.add_column("Power", justify="right")
        for key, name in config.ALGORITHMS.items():
            hr = rates.get(key, 0)
            t.add_row(name, ui.fmt_hashrate(hr).replace("H/s", "H/s (Sol/s)") if key in ("zhash", "beamhash3", "zelhash", "equihash") and hr
                      else ui.fmt_hashrate(hr) if hr else "[grey58]not set[/]",
                      f"{watts.get(key, 0):.0f} W" if hr else "")
        console.print(t)

    show()
    console.print(f"[{ui.C_DIM}]Presets are estimates. Algorithms that are 'not set' are simply skipped.[/]")
    default_tune = gpu == CUSTOM
    if _ask(questionary.confirm("Fine-tune hashrates / power manually?", default=default_tune, style=STYLE)):
        picked = _ask(questionary.checkbox(
            "Which algorithms do you want to edit?", style=STYLE,
            choices=[questionary.Choice(name, value=key) for key, name in config.ALGORITHMS.items()]))
        for key in picked:
            name = config.ALGORITHMS[key]
            rates[key] = _number(f"{name} hashrate in H/s (e.g. 30 MH/s = 30000000):", default=rates.get(key, 0))
            watts[key] = _number(f"{name} power draw (watts):", default=watts.get(key, 0))
        show()
    cfg["hardware"]["hashrates"] = {k: v for k, v in rates.items() if v > 0}
    cfg["hardware"]["power_watts"] = {k: v for k, v in watts.items() if rates.get(k, 0) > 0}
    cfg["electricity_cost_kwh"] = _number("Electricity cost in USD per kWh (0 to ignore):", default=0.0)


# -------------------------------------------------------------------- miners
def _miners(console: Console, cfg: dict[str, Any]) -> None:
    console.print(f"[{ui.C_DIM}]Paste the full path to each miner you have (drag & drop the .exe works). "
                  "Leave blank to skip.[/]")
    search = [config.app_dir() / "miners", config.app_dir()]
    needed = {m for algo in cfg["hardware"]["hashrates"] for m in ALGO_MINERS.get(algo, [])}
    for name, spec in MINERS.items():
        if needed and name not in needed:
            continue
        found = cfg["miners"].get(name) or detect_miner(name, search)

        def check(text: str) -> bool | str:
            p = text.strip().strip('"')
            return not p or Path(p).is_file() or "File not found (leave blank to skip)"
        algos = ", ".join(config.ALGORITHMS[a] for a in spec["algos"])
        path = _ask(questionary.text(f"{spec['label']} path [{algos}]:", default=found, validate=check, style=STYLE))
        cfg["miners"][name] = path.strip().strip('"')


# --------------------------------------------------------------------- coins
def _coins(console: Console, cfg: dict[str, Any]) -> None:
    rates = cfg["hardware"]["hashrates"]
    choices = [
        questionary.Choice(f"{tag:5} {name} ({config.ALGORITHMS[algo]})", value=tag,
                           checked=tag in cfg["coins"])
        for tag, name, algo in POPULAR_COINS if rates.get(algo, 0) > 0
    ]
    if not choices:
        console.print(f"[{ui.C_WARN}]No hashrates configured, so there is nothing to mine yet. "
                      "Edit config.json or re-run --setup.[/]")
        return
    picked = _ask(questionary.checkbox("Coins you want to mine (space to toggle):", choices=choices, style=STYLE))
    coins: dict[str, dict] = {}
    for tag in picked:
        old = cfg["coins"].get(tag, {})
        wallet = _ask(questionary.text(
            f"{tag} wallet address:", default=old.get("wallet", ""), style=STYLE,
            validate=lambda t: bool(re.fullmatch(r"[^\s-]\S{9,}", t.strip())) or "Enter a valid wallet address")).strip()
        pool = _ask(questionary.text(
            f"{tag} pool (host:port, or ssl://host:port for TLS):", default=old.get("pool") or config.DEFAULT_POOLS.get(tag, ""), style=STYLE,
            validate=lambda t: bool(re.fullmatch(
                r"((ssl|tls|tcp|stratum\+(ssl|tls|tcp))://)?[A-Za-z0-9.-]+:\d{2,5}", t.strip()))
            or "Format: host:port (or ssl://host:port)")).strip()
        coins[tag] = {**old, "wallet": wallet, "pool": pool}
    cfg["coins"] = coins
    console.print(f"[{ui.C_DIM}]More coins can be added by hand in config.json → \"coins\".[/]")


# ------------------------------------------------------------------ donation
def _donation(console: Console, cfg: dict[str, Any]) -> None:
    dc = cfg["donation_cycle"]
    console.print(Panel(
        f"NexusSwitch is free and has [bold]no hidden fees[/].\n"
        f"If you opt in, it mines to the developer's wallet for [bold]{dc['percent']:g}%[/] of the time "
        f"(≈{dc['cycle_minutes'] * dc['percent'] / 100:.0f} min every {dc['cycle_minutes'] / 60:g} h), only for coins the "
        "developer publishes a wallet for.\nIt is always shown on the dashboard and can be switched off any time in "
        'config.json → "donation_cycle".',
        title="💖 Optional donation cycle", border_style=ui.C_PRIMARY))
    dc["enabled"] = _ask(questionary.confirm(
        f"Support development with a {dc['percent']:g}% donation cycle?", default=True, style=STYLE))


# ---------------------------------------------------------------------- main
def run_wizard(console: Console, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the full wizard and save config.json. Raises WizardCancelled."""
    cfg = existing or config.default_config()
    console.clear()
    console.print(ui.banner())
    console.print()
    console.print(ui.donation_box())
    console.print(f"\n[bold]Welcome![/] Let's get your rig switching in about two minutes. "
                  f"[{ui.C_DIM}](Ctrl+C cancels, nothing is saved until the end)[/]")
    total = 5

    _step(console, 1, total, "Telegram notifications")
    cfg["worker_name"] = re.sub(r"[^A-Za-z0-9_-]", "", _ask(questionary.text(
        "Worker / rig name:", default=cfg.get("worker_name") or "nexus", style=STYLE))) or "nexus"
    already = cfg["telegram"].get("enabled") and cfg["telegram"].get("bot_token")
    question = "Re-link Telegram? (currently linked)" if already else "Enable Telegram notifications & remote control?"
    if _ask(questionary.confirm(question, default=not already, style=STYLE)):
        if not link_telegram(console, cfg) and not already:
            cfg["telegram"]["enabled"] = False
            console.print(f"[{ui.C_WARN}]Telegram skipped — link later with: python main.py --link-telegram[/]")

    _step(console, 2, total, "Hardware profile")
    _hardware(console, cfg)
    _step(console, 3, total, "Miner executables")
    _miners(console, cfg)
    _step(console, 4, total, "Coins, wallets & pools")
    _coins(console, cfg)
    _step(console, 5, total, "Support the project")
    _donation(console, cfg)

    config.validate(cfg)
    config.save_config(cfg)
    console.print(Panel(
        f"[{ui.C_GOOD}]✔ Saved to {config.CONFIG_PATH}[/]\n"
        f"Coins: [bold]{', '.join(cfg['coins']) or 'none yet'}[/] · "
        f"Telegram: [bold]{'linked' if cfg['telegram']['enabled'] else 'off'}[/] · "
        f"Switch threshold: [bold]{cfg['switching']['threshold_percent']:g}%[/] × "
        f"{cfg['switching']['confirmations']} checks",
        title="🎉 Setup complete", border_style=ui.C_GOOD))
    return cfg
