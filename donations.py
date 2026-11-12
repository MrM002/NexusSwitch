"""Developer donation wallets - the single source of truth for the terminal UI,
the Telegram ``/donate`` command and the optional donation mining cycle.

Keep README.md and .github/FUNDING.yml in sync when changing an address
(``python test_core.py`` fails if the README drifts).
"""
from __future__ import annotations

PROJECT_NAME = "NexusSwitch"
PROJECT_URL = "https://github.com/MrM002/NexusSwitch"
DONATE_URL = f"{PROJECT_URL}#support--donate"

_EVM = "0x2a41fff54CD971208CB2b22C283F88Bbdd80dC86"
_TRON = "TAVHibFGZtAviidRntJ865WiTzZHPpyGcK"
_BTC = "bc1qe7ru3cqa2me8yd3fxuyl5l84kuux5s70dj0jys"
_XMR = "84v25MAGWRvQjDWSvc5juS8mWiTxDBN7Q8LXoKr2Mi36UcTTNKXnSNCVViJRtJ4Gwy4NmoELaekoEeftUsXDKvLFUN2XzuM"
_SOL = "563hB2cnjKXM9YwtTSJBJAbYbA7dKjxqHjzGLtFLnWXP"
_LTC = "ltc1q8hezx2rmxryemdxpuqph77m6eataqgunylxdne"
_DOGE = "D8YFHRQwiWvJzJQAcgxBAYMWNWQb5PNFDB"
_BCH = "bitcoincash:qpp3fdywurf0t6gf5cvegzl22jrurck0fyh82az9cg"
_ZEC = "u1w3ph6tg5j9fv3ps5gnpv8wurxaskls40ztg89pmhs6r85jdcxdayyenv7u2yshvjya6ek9722mtxmdaewwkjjfkdls4xj2c9hgcnjfxz"
_DCR = "DsmnbZ38ZHhEmwnBcmnWyzqaoBrrvM57Lra"
_XNO = "nano_33pjgdrf1dbtzzcbpdnkcmzn3s46azof9eopsdpgh6c35wp7ckkshtahmbry"
_TON = "UQAON17b-WB1PPQ9R_J16braRY_ZcskNYoc9YLbnbjqpdv6s"

# Primary wallets: shown in the terminal banner / footer and first in /donate.
PRIMARY_WALLETS: dict[str, str] = {
    "USDT (TRC20) / TRX": _TRON,
    "USDT (ERC20/BEP20) / ETH / BNB": _EVM,
    "BTC": _BTC,
    "XMR": _XMR,
    "SOL": _SOL,
    "LTC": _LTC,
}

# Everything else: shown in /donate and the README.
MORE_WALLETS: dict[str, str] = {
    "DOGE": _DOGE,
    "BCH": _BCH,
    "ZEC (Unified)": _ZEC,
    "DCR": _DCR,
    "XNO (Nano)": _XNO,
    "TON": _TON,
}

DEV_WALLETS: dict[str, str] = {**PRIMARY_WALLETS, **MORE_WALLETS}

# coin ticker -> developer wallet used by the *optional* donation mining cycle.
# Only coins listed here can ever be donation-mined, and only while the user is
# already mining that coin (the cycle never switches coins by itself).
#
# ZEC is deliberately absent: mining pools pay out to transparent (t1...)
# addresses and generally reject Unified (u1...) ones, which would only cost
# the user hashrate. Add a t-address here to enable it.
DEV_MINING_WALLETS: dict[str, str] = {
    "XMR": _XMR,
    "ETC": _EVM,  # Ethereum Classic pays out to the same EVM address
    "BTC": _BTC,
    "LTC": _LTC,
    "DOGE": _DOGE,
    "BCH": _BCH.split(":", 1)[-1],  # pools expect CashAddr without the URI prefix
    "DCR": _DCR,
}


def is_placeholder(address: str | None) -> bool:
    """True when an address is empty or still a ``YOUR_..._ADDRESS`` template."""
    if not address:
        return True
    a = address.strip()
    return not a or (a.upper().startswith("YOUR_") and a.upper().endswith("_ADDRESS"))


def primary_wallets() -> dict[str, str]:
    """Short list for the terminal banner / footer."""
    return {k: v for k, v in PRIMARY_WALLETS.items() if not is_placeholder(v)}


def visible_wallets() -> dict[str, str]:
    """Full list, primary wallets first."""
    return {k: v for k, v in DEV_WALLETS.items() if not is_placeholder(v)}


def dev_mining_wallet(coin: str | None) -> str | None:
    """Developer wallet for donation-mining ``coin`` (None if not available)."""
    addr = DEV_MINING_WALLETS.get((coin or "").upper())
    return None if is_placeholder(addr) else addr


def donation_text_plain() -> str:
    lines = [f"Support {PROJECT_NAME} development:"]
    lines += [f"  {label}: {addr}" for label, addr in visible_wallets().items()]
    return "\n".join(lines)


def donation_text_html() -> str:
    """Telegram HTML - each address in <code> so one tap copies it."""
    def block(wallets: dict[str, str]) -> list[str]:
        return [f"<b>{label}</b>\n<code>{addr}</code>" for label, addr in wallets.items() if not is_placeholder(addr)]

    lines = [
        f"💖 <b>Support {PROJECT_NAME}</b>",
        "This bot is 100% free &amp; self-hosted. If it earns you money, "
        "consider tipping the developer (tap an address to copy it):",
        "",
        *block(PRIMARY_WALLETS),
        "",
        "➕ <b>More coins</b>",
        *block(MORE_WALLETS),
        "",
        f"⭐ Star the project: {PROJECT_URL}",
    ]
    return "\n".join(lines)
