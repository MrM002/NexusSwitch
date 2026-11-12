"""Self-hosted Telegram bot: plain async HTTPS calls to the Bot API
(getUpdates long-polling + sendMessage). No framework, no server, no webhook.

Only the linked ``chat_id`` can talk to the bot - everyone else is ignored.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Any, Awaitable, Callable, Protocol

import httpx

import donations

log = logging.getLogger("nexus.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
POLL_TIMEOUT = 25

COMMANDS = [
    ("status", "Current coin, uptime and profit/day"),
    ("top", "Top 5 most profitable coins right now"),
    ("switch", "Force a coin: /switch XMR"),
    ("auto", "Resume automatic switching"),
    ("donate", "Support the developer"),
    ("help", "Show all commands"),
]


class TelegramError(Exception):
    def __init__(self, message: str, code: int = 0, retry_after: float = 0.0):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def valid_token(token: str) -> bool:
    return bool(TOKEN_RE.match(token.strip()))


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _redact(text: str, token: str) -> str:
    """The bot token is part of every API URL - never let it reach a log."""
    return text.replace(token, "<token>") if token else text


def _parse(resp: httpx.Response, token: str) -> Any:
    try:
        data = resp.json()
    except ValueError:
        raise TelegramError(f"HTTP {resp.status_code} (non-JSON reply)", resp.status_code) from None
    if not data.get("ok"):
        params = data.get("parameters") or {}
        raise TelegramError(
            _redact(str(data.get("description", "unknown error")), token),
            int(data.get("error_code", resp.status_code)),
            float(params.get("retry_after", 0) or 0),
        )
    return data.get("result")


def call_sync(token: str, method: str, timeout: float = 15.0, **params: Any) -> Any:
    """Blocking Bot API call (used by the setup wizard)."""
    try:
        resp = httpx.post(API.format(token=token, method=method), json=params, timeout=timeout)
    except httpx.HTTPError as e:
        raise TelegramError(_redact(f"network error: {type(e).__name__}: {e}", token)) from None
    return _parse(resp, token)


# ------------------------------------------------------------ message builders
def welcome_text() -> str:
    cmds = "\n".join(f"/{c} — {esc(d)}" for c, d in COMMANDS)
    return (
        f"🚀 <b>{donations.PROJECT_NAME}</b> is online!\n"
        "Your self-hosted crypto auto profit-switcher.\n\n"
        f"{cmds}\n\n"
        "💖 Free &amp; open-source. If it helps you earn, tap /donate to support the dev."
    )


def switch_text(old: str | None, new: str, gain_pct: float, profit: float, manual: bool = False) -> str:
    if manual:
        return f"🕹 Manual switch to <b>{esc(new)}</b> (${profit:.2f}/day)"
    if not old:
        return f"⛏ Started mining <b>{esc(new)}</b> (${profit:.2f}/day)"
    return f"🔄 Switched from <b>{esc(old)}</b> to <b>{esc(new)}</b> (+{gain_pct:.1f}% profit, ${profit:.2f}/day)"


def top_text(ranked: list, n: int, title: str) -> str:
    """``ranked`` = list of profit_engine.CoinProfit, best first."""
    if not ranked:
        return f"📊 <b>{esc(title)}</b>\nNo market data available yet."
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📊 <b>{esc(title)}</b>"]
    for i, p in enumerate(ranked[:n]):
        badge = medals[i] if i < 3 else f"{i + 1}."
        flag = " ⚠️" if p.quote.spike else ""
        lines.append(f"{badge} <b>{esc(p.coin)}</b> ({esc(p.algo_name)}) — ${p.profit_usd:.2f}/day{flag}")
    if any(p.quote.spike for p in ranked[:n]):
        lines.append("\n⚠️ = suspicious spike / low volume (never auto-mined)")
    return "\n".join(lines)


def crash_text(coin: str, miner: str, code: int | None, restarted: bool) -> str:
    tail = "Auto-restarted ✅" if restarted else "Restart failed ❌ — will retry"
    return f"⚠️ <b>{esc(miner)}</b> crashed while mining <b>{esc(coin)}</b> (exit code {code}). {tail}"


# ------------------------------------------------------------------ interface
class BotHooks(Protocol):
    """What the bot needs from the main app."""
    def status_text(self) -> str: ...
    async def live_top_text(self, n: int) -> str: ...
    async def request_switch(self, coin: str) -> str: ...
    async def resume_auto(self) -> str: ...


class TelegramBot:
    def __init__(self, token: str, chat_id: int, hooks: BotHooks,
                 on_activity: Callable[[str], None] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.token = token.strip()
        self.chat_id = int(chat_id)
        self.hooks = hooks
        self.on_activity = on_activity or (lambda _msg: None)
        self.connected = False
        self.username = ""
        self.last_error = ""
        self.sent = 0
        self._offset = 0
        self._ignored: set[int] = set()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(POLL_TIMEOUT + 15, connect=10),
                                         transport=transport)
        self._handlers: dict[str, Callable[[str], Awaitable[str]]] = {
            "/start": self._cmd_start, "/help": self._cmd_start,
            "/status": self._cmd_status, "/top": self._cmd_top,
            "/switch": self._cmd_switch, "/auto": self._cmd_auto,
            "/donate": self._cmd_donate,
        }

    async def close(self) -> None:
        await self._client.aclose()

    # -------------------------------------------------------------- transport
    async def _call(self, method: str, **params: Any) -> Any:
        try:
            resp = await self._client.post(API.format(token=self.token, method=method), json=params)
        except httpx.HTTPError as e:
            raise TelegramError(_redact(f"network error: {type(e).__name__}: {e}", self.token)) from None
        return _parse(resp, self.token)

    async def send(self, text: str, chat_id: int | None = None) -> bool:
        """Send an HTML message. Never raises - returns False on failure."""
        for attempt in range(3):
            try:
                await self._call("sendMessage", chat_id=chat_id or self.chat_id, text=text,
                                 parse_mode="HTML", disable_web_page_preview=True)
                self.sent += 1
                return True
            except TelegramError as e:
                self.last_error = str(e)
                if e.code == 429:  # flood control
                    await asyncio.sleep(min(e.retry_after or 3, 30))
                elif e.code in (400, 401, 403):
                    log.warning("telegram send rejected: %s", e)
                    return False
                else:
                    await asyncio.sleep(2 * (attempt + 1))
        log.warning("telegram send failed: %s", self.last_error)
        return False

    # ---------------------------------------------------------------- polling
    async def run(self) -> None:
        """Background task: connect, then long-poll forever with backoff."""
        backoff = 3.0
        announced = False
        while True:
            try:
                if not self.connected:
                    me = await self._call("getMe")
                    self.username = me.get("username", "")
                    await self._call("setMyCommands",
                                     commands=[{"command": c, "description": d} for c, d in COMMANDS])
                    # drop commands queued while we were offline (e.g. an old /switch)
                    stale = await self._call("getUpdates", offset=-1, timeout=0)
                    if stale:
                        self._offset = stale[-1]["update_id"] + 1
                    self.connected, self.last_error, backoff = True, "", 3.0
                    if not announced:
                        announced = True
                        self.on_activity(f"Telegram bot @{self.username} connected")
                        await self.send(welcome_text())
                updates = await self._call("getUpdates", offset=self._offset, timeout=POLL_TIMEOUT,
                                           allowed_updates=["message"])
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    await self._handle(upd)
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                self.connected, self.last_error = False, str(e)
                if e.code == 401:
                    self.on_activity("Telegram: bot token rejected - re-run with --setup")
                    log.error("telegram token invalid, bot disabled")
                    return
                if e.code == 409:
                    self.last_error = "another instance is polling this bot"
                wait = e.retry_after or backoff
                backoff = min(backoff * 2, 60.0)
                log.warning("telegram poll error: %s (retry in %.0fs)", self.last_error, wait)
                await asyncio.sleep(wait)
            except Exception:  # never let the bot task die silently
                self.connected = False
                log.exception("unexpected telegram error")
                await asyncio.sleep(backoff)

    async def _handle(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        if not text.startswith("/") or chat is None:
            return
        if chat != self.chat_id:  # not the owner: stay silent
            if chat not in self._ignored:
                self._ignored.add(chat)
                log.warning("ignored command from unlinked chat %s", chat)
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        handler = self._handlers.get(cmd)
        if handler is None:
            await self.send("Unknown command. Send /help for the list.")
            return
        try:
            reply = await handler(arg.strip())
        except Exception as e:
            log.exception("command %s failed", cmd)
            reply = f"❌ {esc(cmd)} failed: {esc(type(e).__name__)}"
        self.on_activity(f"Telegram command {cmd} answered")
        await self.send(reply)

    # --------------------------------------------------------------- commands
    async def _cmd_start(self, _arg: str) -> str:
        return welcome_text()

    async def _cmd_status(self, _arg: str) -> str:
        return self.hooks.status_text()

    async def _cmd_top(self, _arg: str) -> str:
        return await self.hooks.live_top_text(5)

    async def _cmd_switch(self, arg: str) -> str:
        coin = arg.split()[0].upper() if arg else ""
        if not re.fullmatch(r"[A-Z0-9]{2,10}", coin):
            return "Usage: <code>/switch XMR</code>"
        return await self.hooks.request_switch(coin)

    async def _cmd_auto(self, _arg: str) -> str:
        return await self.hooks.resume_auto()

    async def _cmd_donate(self, _arg: str) -> str:
        return donations.donation_text_html()


def wait_for_start(token: str, timeout: float = 300.0,
                   tick: Callable[[], None] | None = None) -> dict | None:
    """Wizard helper: block until someone sends /start to the bot.
    Returns the Telegram ``chat`` object, or None on timeout."""
    deadline = time.time() + timeout
    offset = 0
    try:  # a webhook (if any was ever set) would block getUpdates
        call_sync(token, "deleteWebhook")
    except TelegramError:
        pass
    while time.time() < deadline:
        if tick:
            tick()
        try:
            updates = call_sync(token, "getUpdates", offset=offset)  # short poll
        except TelegramError as e:
            if e.code in (401, 404):
                raise
            time.sleep(2)
            continue
        for upd in updates or []:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if (msg.get("text") or "").strip().lower().startswith("/start") and msg.get("chat"):
                call_sync(token, "getUpdates", offset=offset)  # acknowledge
                return msg["chat"]
        time.sleep(1.5)
    return None
