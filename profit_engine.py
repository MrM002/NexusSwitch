"""Real-time market data + profitability engine + anti-flap switch logic.

Data sources (all free, the end user needs no API key):
  * WhatToMine public JSON  (network hashrate, block reward, BTC price, volume)
  * CoinGecko / Coinbase     (BTC -> USD)
  * Minerstat ``/v2/coins``  (optional - their API now requires a free key;
                              used first when ``data_source.minerstat_api_key``
                              is set, with automatic fallback)
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import ALGORITHMS, APP_VERSION

log = logging.getLogger("nexus.profit")

MINERSTAT_URL = "https://api.minerstat.com/v2/coins"
WTM_BASE = "https://whattomine.com"
COINGECKO_BTC = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
COINBASE_BTC = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

USER_AGENT = f"NexusSwitch/{APP_VERSION} (open-source profit switcher)"
MAX_EXTRA_COIN_LOOKUPS = 8
STALE_QUOTES_MAX_AGE = 45 * 60  # reuse last good data for up to 45 min of outage

# normalised (lowercase alphanumeric) source name -> canonical algorithm key
_ALGO_ALIASES = {
    "randomx": "randomx",
    "equihash": "equihash",
    "zhash": "zhash", "equihash1445": "zhash",
    "beamhashiii": "beamhash3", "beamhash3": "beamhash3", "beamhash": "beamhash3",
    "zelhash": "zelhash", "equihash1254": "zelhash",
    "kawpow": "kawpow",
    "autolykos2": "autolykos2", "autolykos": "autolykos2",
    "etchash": "etchash",
    "astrobwt": "astrobwt", "astrobwtv3": "astrobwt",
    "dynexsolve": "dynexsolve",
}


def canonical_algo(name: str | None) -> str | None:
    if not name:
        return None
    return _ALGO_ALIASES.get(re.sub(r"[^a-z0-9]", "", name.lower()))


class MarketDataError(Exception):
    """All data sources failed and no fresh-enough cached data exists."""


@dataclass
class CoinQuote:
    coin: str
    name: str
    algo: str                     # canonical key (see config.ALGORITHMS)
    price_usd: float
    volume_usd: float             # 24h exchange volume
    reward_per_hash_day: float    # coins earned per 1 H/s in 24h
    diff_trend_pct: float | None = None   # current difficulty vs 24h average
    spike: bool = False
    spike_reason: str = ""
    source: str = ""


@dataclass
class CoinProfit:
    quote: CoinQuote
    hashrate: float
    revenue_usd: float
    power_cost_usd: float
    profit_usd: float
    status: str = ""              # filled in by the app (Mining / Ready / ...)

    @property
    def coin(self) -> str:
        return self.quote.coin

    @property
    def algo_name(self) -> str:
        return ALGORITHMS.get(self.quote.algo, self.quote.algo)


def _f(value: Any, default: float = 0.0) -> float:
    """Tolerant float conversion for messy API payloads."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out and out not in (float("inf"), float("-inf")) else default


class ProfitEngine:
    def __init__(self, cfg: dict[str, Any], client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        self.source = "-"
        self.last_update = 0.0
        self.last_error = ""
        self._quotes: list[CoinQuote] = []
        self._btc_usd = 0.0
        self._btc_ts = 0.0
        self._wtm_index: dict[str, dict] = {}
        self._wtm_index_ts = 0.0
        self._minerstat_disabled_until = 0.0
        # rolling unit-revenue history per coin for spike detection
        self._history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=20))

    async def close(self) -> None:
        if self._own_client:
            await self.client.aclose()

    # ------------------------------------------------------------------ HTTP
    async def _get_json(self, url: str, headers: dict | None = None, retries: int = 3) -> Any:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = await self.client.get(url, headers=headers)
                if r.status_code == 429:  # rate limited: honour Retry-After
                    wait = min(_f(r.headers.get("Retry-After"), 5.0), 30.0)
                    last = MarketDataError(f"rate limited by {httpx.URL(url).host}")
                    await asyncio.sleep(wait)
                    continue
                if r.status_code in (401, 403):
                    raise PermissionError(f"{httpx.URL(url).host} rejected the request ({r.status_code})")
                r.raise_for_status()
                return r.json()
            except PermissionError:
                raise
            except (httpx.HTTPError, ValueError) as e:
                last = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise MarketDataError(f"{httpx.URL(url).host}: {type(last).__name__}: {last}")

    async def _btc_price(self) -> float:
        if self._btc_usd and time.time() - self._btc_ts < 120:
            return self._btc_usd
        for url, pick in (
            (COINGECKO_BTC, lambda d: d["bitcoin"]["usd"]),
            (COINBASE_BTC, lambda d: d["data"]["amount"]),
        ):
            try:
                price = _f(pick(await self._get_json(url, retries=2)))
                if price > 0:
                    self._btc_usd, self._btc_ts = price, time.time()
                    return price
            except (MarketDataError, PermissionError, KeyError, TypeError) as e:
                log.warning("BTC price source failed: %s", e)
        if self._btc_usd:  # stale but better than nothing
            return self._btc_usd
        raise MarketDataError("could not fetch BTC/USD price")

    # ------------------------------------------------------------- Minerstat
    async def _fetch_minerstat(self) -> list[CoinQuote]:
        key = (self.cfg.get("data_source", {}).get("minerstat_api_key") or "").strip()
        data = await self._get_json(MINERSTAT_URL, headers={"X-API-Key": key})
        if not isinstance(data, list):
            raise MarketDataError("minerstat: unexpected payload")
        now = time.time()
        quotes = []
        for c in data:
            if not isinstance(c, dict) or c.get("type", "coin") != "coin":
                continue
            algo = canonical_algo(c.get("algorithm"))
            reward, price = _f(c.get("reward")), _f(c.get("price"))
            if not algo or reward <= 0 or price <= 0:
                continue  # minerstat reports -1 for unknown values
            updated = _f(c.get("updated"))
            if updated and now - updated > 3 * 3600:
                continue  # stale listing
            quotes.append(CoinQuote(
                coin=str(c.get("coin", "")).upper(), name=str(c.get("name", "")), algo=algo,
                price_usd=price, volume_usd=_f(c.get("volume")),
                reward_per_hash_day=reward * 24.0,  # minerstat: coins per 1 H/s per hour
                source="Minerstat",
            ))
        if not quotes:
            raise MarketDataError("minerstat: no usable coins in response")
        return quotes

    # ------------------------------------------------------------ WhatToMine
    def _wtm_quote(self, name: str, c: dict, btc_usd: float) -> CoinQuote | None:
        tag = str(c.get("tag", "")).upper()
        algo = canonical_algo(c.get("algorithm"))
        if not algo or not tag or tag == "NICEHASH" or c.get("lagging") or c.get("testing"):
            return None
        if c.get("status") not in (None, "Active"):
            return None
        nethash, block_time = _f(c.get("nethash")), _f(c.get("block_time"))
        reward, rate = _f(c.get("block_reward")), _f(c.get("exchange_rate"))
        if min(nethash, block_time, reward, rate) <= 0:
            return None
        curr = str(c.get("exchange_rate_curr", "BTC")).upper()
        fx = btc_usd if curr == "BTC" else 1.0 if curr in ("USD", "USDT", "USDC") else 0.0
        if fx <= 0:
            return None
        blocks_day = 86400.0 / block_time
        q = CoinQuote(
            coin=tag, name=name, algo=algo, price_usd=rate * fx,
            volume_usd=_f(c.get("exchange_rate_vol")) * fx,
            reward_per_hash_day=blocks_day * reward / nethash,
            source="WhatToMine",
        )
        # --- anti-spike: compare "right now" against the 24h average ---------
        diff, diff24 = _f(c.get("difficulty")), _f(c.get("difficulty24"))
        reward24, rate24 = _f(c.get("block_reward24"), reward), _f(c.get("exchange_rate24"), rate)
        if diff > 0 and diff24 > 0:
            q.diff_trend_pct = (diff / diff24 - 1.0) * 100.0
            now_unit = q.reward_per_hash_day * rate
            avg_unit = blocks_day * (reward24 or reward) / (nethash * diff24 / diff) * (rate24 or rate)
            ratio = now_unit / avg_unit if avg_unit > 0 else 1.0
            if ratio > self.cfg["switching"]["spike_ratio"]:
                q.spike, q.spike_reason = True, f"{ratio:.1f}x above its 24h average"
        return q

    async def _wtm_index_cached(self) -> dict[str, dict]:
        if self._wtm_index and time.time() - self._wtm_index_ts < 12 * 3600:
            return self._wtm_index
        data = await self._get_json(f"{WTM_BASE}/calculators.json")
        self._wtm_index = data.get("coins", {}) if isinstance(data, dict) else {}
        self._wtm_index_ts = time.time()
        return self._wtm_index

    async def _fetch_whattomine(self) -> list[CoinQuote]:
        btc_usd = await self._btc_price()
        results = await asyncio.gather(
            self._get_json(f"{WTM_BASE}/coins.json"),
            self._get_json(f"{WTM_BASE}/asic.json"),
            return_exceptions=True,
        )
        quotes: dict[tuple[str, str], CoinQuote] = {}
        errors = []
        for res in results:
            if isinstance(res, Exception):
                errors.append(str(res))
                continue
            for name, c in (res.get("coins", {}) if isinstance(res, dict) else {}).items():
                q = self._wtm_quote(name, c, btc_usd) if isinstance(c, dict) else None
                if q:
                    quotes.setdefault((q.coin, q.algo), q)
        if not quotes:
            raise MarketDataError("whattomine: " + ("; ".join(errors) or "no usable coins"))

        # Coins that are not on the two summary lists (e.g. DERO, BTCZ) or that
        # the user explicitly configured: look them up individually (capped).
        try:
            index = await self._wtm_index_cached()
        except (MarketDataError, PermissionError) as e:
            log.warning("whattomine index unavailable: %s", e)
            index = {}
        have = {coin for coin, _ in quotes}
        wanted_coins = set(self.cfg.get("coins", {}))
        covered_algos = {algo for _, algo in quotes}
        extra: list[tuple[str, int]] = []
        for name, c in index.items():
            algo = canonical_algo(c.get("algorithm"))
            tag = str(c.get("tag", "")).upper()
            if not algo or tag in have or c.get("status") != "Active" or c.get("lagging") or c.get("testing"):
                continue
            if tag in wanted_coins or algo not in covered_algos:
                extra.append((name, int(_f(c.get("id")))))
        extra.sort(key=lambda e: index[e[0]].get("tag", "").upper() not in wanted_coins)
        lookups = extra[:MAX_EXTRA_COIN_LOOKUPS]
        details = await asyncio.gather(
            *(self._get_json(f"{WTM_BASE}/coins/{cid}.json", retries=2) for _, cid in lookups),
            return_exceptions=True,
        )
        for (name, _), d in zip(lookups, details):
            if isinstance(d, dict):
                q = self._wtm_quote(d.get("name", name), d, btc_usd)
                if q:
                    quotes.setdefault((q.coin, q.algo), q)
        return list(quotes.values())

    # ----------------------------------------------------------------- public
    async def fetch_quotes(self) -> list[CoinQuote]:
        """Fetch + sanitise live quotes. Falls back between sources and, during
        short outages, to the last good snapshot."""
        quotes: list[CoinQuote] | None = None
        errors: list[str] = []
        key = (self.cfg.get("data_source", {}).get("minerstat_api_key") or "").strip()
        if key and time.time() >= self._minerstat_disabled_until:
            try:
                quotes = await self._fetch_minerstat()
            except PermissionError as e:
                errors.append(f"{e} - check minerstat_api_key")
                self._minerstat_disabled_until = time.time() + 3600
            except MarketDataError as e:
                errors.append(str(e))
        if quotes is None:
            try:
                quotes = await self._fetch_whattomine()
            except (MarketDataError, PermissionError) as e:
                errors.append(str(e))
        if quotes is None:
            self.last_error = " | ".join(errors)
            if self._quotes and time.time() - self.last_update < STALE_QUOTES_MAX_AGE:
                log.warning("market data unavailable, using cached snapshot: %s", self.last_error)
                return self._quotes
            raise MarketDataError(self.last_error)

        self.last_error = " | ".join(errors)
        self._apply_filters(quotes)
        self.source = quotes[0].source
        self._quotes, self.last_update = quotes, time.time()
        return quotes

    def _apply_filters(self, quotes: list[CoinQuote]) -> None:
        """Flag (never silently drop) coins whose numbers cannot be trusted."""
        sw = self.cfg["switching"]
        for q in quotes:
            unit = q.reward_per_hash_day * q.price_usd
            hist = self._history[f"{q.coin}:{q.algo}"]
            if not q.spike and q.volume_usd < sw["min_volume_usd"]:
                q.spike, q.spike_reason = True, f"24h volume only ${q.volume_usd:,.0f}"
            if not q.spike and len(hist) >= 3:
                med = statistics.median(hist)
                if med > 0 and unit / med > sw["spike_ratio"]:
                    q.spike, q.spike_reason = True, f"{unit / med:.1f}x above its recent median"
            if not q.spike:
                hist.append(unit)  # spikes never pollute the baseline
            if q.diff_trend_pct is None and len(hist) >= 2 and hist[0] > 0:
                # no 24h data from the source: derive a trend from our own history
                # (unit revenue falls when difficulty rises, hence the inversion)
                q.diff_trend_pct = (hist[0] / unit - 1.0) * 100.0 if unit > 0 else None

    def rank(self, quotes: list[CoinQuote]) -> list[CoinProfit]:
        """Estimated 24h profit for the configured hardware, best first."""
        hw = self.cfg["hardware"]
        kwh = self.cfg["electricity_cost_kwh"]
        out = []
        for q in quotes:
            hr = _f(hw["hashrates"].get(q.algo))
            if hr <= 0:
                continue
            revenue = q.reward_per_hash_day * hr * q.price_usd
            cost = _f(hw["power_watts"].get(q.algo)) / 1000.0 * 24.0 * kwh
            out.append(CoinProfit(q, hr, revenue, cost, revenue - cost))
        out.sort(key=lambda p: p.profit_usd, reverse=True)
        return out


@dataclass
class SwitchDecision:
    target: CoinProfit | None = None
    gain_pct: float = 0.0
    reason: str = ""


@dataclass
class SwitchDecider:
    """Anti-flap logic: a challenger must beat the current coin by more than
    ``threshold_percent`` for ``confirmations`` consecutive checks."""
    threshold_percent: float = 7.0
    confirmations: int = 2
    _candidate: str = field(default="", repr=False)
    _streak: int = field(default=0, repr=False)

    def reset(self) -> None:
        self._candidate, self._streak = "", 0

    @property
    def pending(self) -> tuple[str, int]:
        return self._candidate, self._streak

    def decide(self, current: str | None, eligible: list[CoinProfit]) -> SwitchDecision:
        """``eligible`` = mineable, non-spiking coins sorted best-first."""
        if not eligible:
            self.reset()
            return SwitchDecision(reason="no eligible coins")
        best = eligible[0]
        cur = next((p for p in eligible if p.coin == current), None)
        if current is None or cur is None:
            # nothing running, or the current coin is no longer mineable/trustworthy
            self.reset()
            return SwitchDecision(best, 0.0, "initial selection" if current is None else f"{current} no longer eligible")
        if best.coin == current:
            self.reset()
            return SwitchDecision(reason="already on the best coin")
        if cur.profit_usd > 0:
            gain = (best.profit_usd / cur.profit_usd - 1.0) * 100.0
            beats = gain > self.threshold_percent
        else:  # current coin is losing money: any positive improvement counts
            gain = 100.0
            beats = best.profit_usd > cur.profit_usd and best.profit_usd > 0
        if not beats:
            self.reset()
            return SwitchDecision(gain_pct=gain, reason=f"{best.coin} only +{gain:.1f}% (< {self.threshold_percent:g}%)")
        if best.coin == self._candidate:
            self._streak += 1
        else:
            self._candidate, self._streak = best.coin, 1
        if self._streak >= self.confirmations:
            self.reset()
            return SwitchDecision(best, gain, "threshold confirmed")
        return SwitchDecision(gain_pct=gain, reason=f"{best.coin} +{gain:.1f}% - confirming {self._streak}/{self.confirmations}")


# --------------------------------------------------------- boost vs. static
# Established coins a "set and forget" miner would typically stick to.
ANCHOR_COINS = ("XMR", "ETC", "RVN", "ERG", "ZEC")


def pick_anchor(eligible: list[CoinProfit]) -> str | None:
    """Static-mining baseline coin: the most profitable established coin the
    rig can mine right now, or - when none of them is available - the median
    eligible coin. Chosen once per session and then held, exactly like a miner
    who picks a coin and never switches."""
    if not eligible:
        return None
    anchors = [p for p in eligible if p.coin in ANCHOR_COINS]
    if anchors:
        return max(anchors, key=lambda p: p.profit_usd).coin
    ordered = sorted(eligible, key=lambda p: p.profit_usd)
    return ordered[len(ordered) // 2].coin


@dataclass
class Boost:
    """Estimated daily profit: static mining of ``anchor`` vs. the coin
    NexusSwitch is on right now. An estimate - it ignores switch downtime."""
    anchor: str
    static_usd: float
    current_usd: float

    @property
    def gain_usd(self) -> float:
        return self.current_usd - self.static_usd

    @property
    def gain_pct(self) -> float | None:
        return self.gain_usd / self.static_usd * 100.0 if self.static_usd > 0 else None

    @property
    def positive(self) -> bool:
        return self.gain_usd >= 0.005

    def line(self) -> str:
        head = f"vs. Static Mining ({self.anchor}): ${self.static_usd:,.2f}/d ➔ ${self.current_usd:,.2f}/d"
        pct = self.gain_pct
        if self.positive:
            boost = f"🚀 +{pct:.1f}% Boost | " if pct is not None else "🚀 "
            return f"{head} ({boost}+${self.gain_usd:,.2f}/d with NexusSwitch)"
        if self.gain_usd > -0.005:
            return f"{head} (on the best coin already — watching for a better one)"
        pct_txt = f"{pct:+.1f}% | " if pct is not None else ""
        return f"{head} ({pct_txt}-${-self.gain_usd:,.2f}/d — holding, below switch threshold)"
