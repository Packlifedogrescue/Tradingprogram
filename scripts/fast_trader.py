#!/usr/bin/env python3
"""
OptionEdge Fast Trader -- 0DTE Edition
=======================================
Intraday execution engine built exclusively for SPX 0DTE options.
Async concurrent API calls, 5-min bars, 30-second scan cycle.

Signals (12 strategies):
  trend, mean-reversion, breakout, bull/bear flag, FVG, session VWAP,
  candlestick, intraday pivot, options flow, MACD, ORB, intraday EMA cross

Entry filters (in order):
  circuit breaker, VIX level, VIX rate-of-change, news spike (ATR),
  market regime (no chop), time gate (10AM-3:30PM), FOMC/CPI/NFP blackout,
  time decay gate (2PM+), 0DTE min-strategies, max pain proximity,
  bid-ask spread, IV rank, earnings, correlation guard, confidence, sizing

Exit rules:
  stop loss (25%), tiered trailing stop (0DTE: 15%/10%/7%),
  force-close 3:30PM, MACD cross, RSI exhaustion, Bollinger touch,
  signal reversal, safety-net take-profit (500%)

Usage:
  python scripts/fast_trader.py --dry-run --loop   # paper trade
  python scripts/fast_trader.py --loop             # live trading
  python scripts/fast_trader.py --ticker SPX       # override tickers

Recommended .env.local for 0DTE SPX:
  WATCH_TICKERS=SPX
  TRADE_0DTE=true
  TRADE_MIN_DTE=0
  TRADE_MAX_DTE=1
  TRADE_MAX_VIX=30
  TRADE_MIN_CONFIDENCE=65
  SPX_MAX_CONTRACTS=1
  TRADE_FORCE_CLOSE_TIME=15:30
  TRADE_DATA_INTERVAL=5min
  TRADE_SCAN_SEC=30
  TRADE_POS_CHECK_SEC=10
"""

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import aiohttp
import numpy as np

# Import pure signal math from auto_trader (no cfg side-effects)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auto_trader as _at

ema                  = _at.ema
rsi                  = _at.rsi
rsi_series           = _at.rsi_series
bollinger            = _at.bollinger
atr                  = _at.atr
hv                   = _at.hv
trend_momentum       = _at.trend_momentum
mean_reversion       = _at.mean_reversion
breakout             = _at.breakout
bull_bear_flag       = _at.bull_bear_flag
fvg_signal           = _at.fvg_signal
rsi_divergence       = _at.rsi_divergence
candlestick_signal   = _at.candlestick_signal
options_flow_signal  = _at.options_flow_signal
iv_rank_modifier     = _at.iv_rank_modifier
atm_iv_from_chain    = _at.atm_iv_from_chain
iv_status            = _at.iv_status
macd_histogram            = _at.macd_histogram
macd_entry_signal         = _at.macd_entry_signal
indicator_exit_check      = _at.indicator_exit_check
is_trading_window         = _at.is_trading_window
is_near_high_impact_event = _at.is_near_high_impact_event
is_news_spike             = _at.is_news_spike


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _ei(k, d):
    try: return int(os.environ[k])
    except: return d

def _ef(k, d):
    try: return float(os.environ[k])
    except: return d

class Config:
    twelve_key       = os.environ.get("TWELVE_DATA_API_KEY", "")
    tastytrade_user  = os.environ.get("TASTYTRADE_USERNAME", "")
    tastytrade_pass  = os.environ.get("TASTYTRADE_PASSWORD", "")
    tastytrade_base  = ("https://api.tastytrade.com"
                        if os.environ.get("TASTYTRADE_ENV") == "production"
                        else "https://api.cert.tastytrade.com")
    rh_user          = os.environ.get("ROBINHOOD_USERNAME", "")
    rh_pass          = os.environ.get("ROBINHOOD_PASSWORD", "")
    max_contract_price = _ef("TRADE_MAX_CONTRACT_PRICE", 0)
    resend_key       = os.environ.get("RESEND_API_KEY", "")
    notify_email     = os.environ.get("NOTIFY_EMAIL", "")
    notify_phone     = os.environ.get("NOTIFY_PHONE", "")
    watch_tickers    = [t.strip().upper()
                        for t in os.environ.get("WATCH_TICKERS", "").split(",")
                        if t.strip()]
    sb_url  = os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
    sb_key  = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    # Entry sizing
    min_confidence      = _ei("TRADE_MIN_CONFIDENCE", 65)
    tier2_threshold     = _ei("TRADE_TIER2_THRESHOLD", 72)
    tier3_threshold     = _ei("TRADE_TIER3_THRESHOLD", 85)
    tier1_contracts     = _ei("TRADE_TIER1_CONTRACTS", 1)
    tier2_contracts     = _ei("TRADE_TIER2_CONTRACTS", 2)
    tier3_contracts     = _ei("TRADE_TIER3_CONTRACTS", 3)
    daily_max_contracts = _ei("TRADE_DAILY_MAX_CONTRACTS", 6)

    # Exit rules
    stop_loss_pct      = _ef("TRADE_STOP_LOSS_PCT",    0.25)
    take_profit_pct    = _ef("TRADE_TAKE_PROFIT_PCT",  5.00)
    trail_stop_pct     = _ef("TRADE_TRAIL_STOP_PCT",   0.15)
    trail_start_pct    = _ef("TRADE_TRAIL_START_PCT",  0.15)
    signal_exit        = os.environ.get("TRADE_SIGNAL_EXIT", "true").lower() == "true"
    signal_exit_agree  = _ei("TRADE_SIGNAL_EXIT_AGREE", 3)
    indicator_exit     = os.environ.get("TRADE_INDICATOR_EXIT", "true").lower() == "true"

    # Speed
    data_interval     = os.environ.get("TRADE_DATA_INTERVAL", "5min")
    scan_sec          = _ei("TRADE_SCAN_SEC", 30)
    pos_check_sec     = _ei("TRADE_POS_CHECK_SEC", 10)
    chain_cache_sec   = _ei("TRADE_CHAIN_CACHE_SEC", 120)
    force_close_time  = os.environ.get("TRADE_FORCE_CLOSE_TIME", "15:30")
    max_vix           = _ef("TRADE_MAX_VIX", 30.0)
    zero_dte          = os.environ.get("TRADE_0DTE", "true").lower() == "true"

    # Circuit breaker
    max_daily_losses  = _ei("TRADE_MAX_DAILY_LOSSES", 3)

    @property
    def min_dte(self):
        return 0 if self.zero_dte else _ei("TRADE_MIN_DTE", 1)

    @property
    def max_dte(self):
        return 1 if self.zero_dte else _ei("TRADE_MAX_DTE", 5)


cfg = Config()

_SMS_GATEWAY = "vtext.com"


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(subject: str, body: str) -> None:
    if not cfg.resend_key:
        return
    recipients = []
    if cfg.notify_email:
        recipients.append(cfg.notify_email)
    if cfg.notify_phone:
        phone = "".join(c for c in cfg.notify_phone if c.isdigit())[-10:]
        recipients.append(f"{phone}@{_SMS_GATEWAY}")
    if not recipients:
        return
    try:
        import requests as _req
        _req.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {cfg.resend_key}", "Content-Type": "application/json"},
            json={"from": "Trading Bot <onboarding@resend.dev>", "to": recipients,
                  "subject": subject, "text": body},
            timeout=10,
        )
        print(f"  [NOTIFY] Sent: {subject}")
    except Exception as e:
        print(f"  [NOTIFY] Failed: {e}")


# ---------------------------------------------------------------------------
# Timezone helper
# ---------------------------------------------------------------------------

def _now_et() -> datetime:
    utc = datetime.now(timezone.utc)
    y = utc.year
    mar_first = date(y, 3, 1)
    dst_start = mar_first + timedelta(days=(6 - mar_first.weekday()) % 7 + 7)
    nov_first = date(y, 11, 1)
    dst_end   = nov_first + timedelta(days=(6 - nov_first.weekday()) % 7)
    offset = timedelta(hours=-4) if dst_start <= utc.date() < dst_end else timedelta(hours=-5)
    return (utc + offset).replace(tzinfo=None)


def is_market_open() -> bool:
    et = _now_et()
    if et.weekday() >= 5:
        return False
    o = et.replace(hour=9,  minute=30, second=0, microsecond=0)
    c = et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return o <= et <= c


def is_past_force_close() -> bool:
    et = _now_et()
    parts = cfg.force_close_time.split(":")
    fc = et.replace(hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0)
    return et >= fc


# ---------------------------------------------------------------------------
# Tastytrade auth
# ---------------------------------------------------------------------------

_tt_session_token: Optional[str] = None
_tt_session_expiry: float = 0.0
_TT_SESSION_TTL = 23 * 3600
_tt_lock = asyncio.Lock()


async def _tt_get_token(session: aiohttp.ClientSession) -> Optional[str]:
    global _tt_session_token, _tt_session_expiry
    async with _tt_lock:
        if _tt_session_token and time.monotonic() < _tt_session_expiry:
            return _tt_session_token
        if not cfg.tastytrade_user or not cfg.tastytrade_pass:
            return None
        try:
            async with session.post(
                f"{cfg.tastytrade_base}/sessions",
                json={"login": cfg.tastytrade_user, "password": cfg.tastytrade_pass, "remember-me": True},
                headers={"Content-Type": "application/json", "User-Agent": "personal-option-bot/1.0"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                resp = await r.json()
            token = (resp.get("data") or {}).get("session-token")
            if not token:
                print(f"  [TT] Auth failed: {str(resp)[:200]}")
                return None
            _tt_session_token = token
            _tt_session_expiry = time.monotonic() + _TT_SESSION_TTL
            return token
        except Exception as e:
            print(f"  [TT] Auth error: {e}")
            return None


async def _tt_headers(session: aiohttp.ClientSession) -> dict:
    token = await _tt_get_token(session)
    return {"Authorization": token, "Accept": "application/json",
            "User-Agent": "personal-option-bot/1.0"} if token else {}


# ---------------------------------------------------------------------------
# Chain cache
# ---------------------------------------------------------------------------

_chain_cache: dict[str, tuple[dict, float]] = {}

# ---------------------------------------------------------------------------
# Correlation guard
# ---------------------------------------------------------------------------

CORRELATED_GROUPS: list[frozenset] = [
    frozenset({"SPX", "SPY", "SPXW"}),
    frozenset({"QQQ", "NDX", "QQQM"}),
]
_cycle_traded: dict[str, tuple[str, int]] = {}

# ---------------------------------------------------------------------------
# VIX rate-of-change tracker
# ---------------------------------------------------------------------------

_vix_history: list[tuple[float, float]] = []


def _record_vix(vix_val: float) -> None:
    now = time.monotonic()
    _vix_history.append((now, vix_val))
    while len(_vix_history) > 15:
        _vix_history.pop(0)


def _vix_spiked(spike_threshold: float = 2.5, window_sec: float = 600) -> tuple[bool, str]:
    if len(_vix_history) < 2:
        return False, ""
    now = time.monotonic()
    recent = [(t, v) for t, v in _vix_history if t >= now - window_sec]
    if len(recent) < 2:
        return False, ""
    jump = recent[-1][1] - recent[0][1]
    if jump >= spike_threshold:
        elapsed = int((now - recent[0][0]) / 60)
        return True, f"VIX spiked +{jump:.1f} in {elapsed}min (now {recent[-1][1]:.1f})"
    return False, ""


# ---------------------------------------------------------------------------
# Daily state -- reset each morning
# ---------------------------------------------------------------------------

_daily_used = 0
_daily_losses = 0          # consecutive losing trades
_daily_pnl = 0.0
_last_reset_date = ""


def _daily_reset() -> None:
    global _daily_used, _daily_losses, _daily_pnl, _last_reset_date
    today = date.today().isoformat()
    if _last_reset_date == today:
        return
    _daily_used = 0
    _daily_losses = 0
    _daily_pnl = 0.0
    _cycle_traded.clear()
    _last_reset_date = today
    print(f"  [RESET] Daily counters reset for {today}")


# ---------------------------------------------------------------------------
# 0DTE-specific helpers
# ---------------------------------------------------------------------------

def _today_orb(ohlcv: dict) -> tuple[float, float, bool]:
    """
    Opening Range (9:30-10:00 AM ET) high, low, and whether it is confirmed.
    Confirmed = at least 5 five-minute bars in the window have been received.
    """
    today = date.today().isoformat()
    et = _now_et()
    h_vals: list[float] = []
    l_vals: list[float] = []
    for i, dt in enumerate(ohlcv["datetimes"]):
        if not dt.startswith(today):
            continue
        t = dt[11:16] if len(dt) >= 16 else ""
        if "09:30" <= t < "10:00":
            h_vals.append(float(ohlcv["highs"][i]))
            l_vals.append(float(ohlcv["lows"][i]))
    if not h_vals:
        return 0.0, 0.0, False
    confirmed = len(h_vals) >= 5 and (et.hour > 10 or (et.hour == 10 and et.minute >= 0))
    return max(h_vals), min(l_vals), confirmed


def orb_signal(ohlcv: dict, closes: np.ndarray, volumes: np.ndarray) -> tuple[Optional[str], int]:
    """
    Opening Range Breakout -- the most reliable 0DTE entry setup.
    Signals only after 10:00 AM when the 30-min range is fully formed.
    Break above ORB high = CALL; break below ORB low = PUT.
    Confidence scales with how far price has extended beyond the range.
    Inside the ORB = no trade (noise zone).
    """
    orb_high, orb_low, confirmed = _today_orb(ohlcv)
    if not confirmed or orb_high <= 0:
        return None, 0

    cur = float(closes[-1])
    orb_range = orb_high - orb_low
    if orb_range <= 0:
        return None, 0

    # Volume context -- compare current bar vs ORB average
    today = date.today().isoformat()
    orb_vols = [float(ohlcv["volumes"][i])
                for i, dt in enumerate(ohlcv["datetimes"])
                if dt.startswith(today) and "09:30" <= dt[11:16] < "10:00"]
    avg_orb_vol = sum(orb_vols) / max(len(orb_vols), 1)
    cur_vol = float(volumes[-1]) if len(volumes) > 0 else avg_orb_vol
    vol_ok = cur_vol >= avg_orb_vol * 0.90

    # Skip if deep inside the range (wait for a side to win)
    inside_pct = (cur - orb_low) / orb_range
    if 0.15 < inside_pct < 0.85:
        return None, 0

    if cur > orb_high and vol_ok:
        ext = (cur - orb_high) / orb_range
        return "CALL", min(84, 70 + int(ext * 45))

    if cur < orb_low and vol_ok:
        ext = (orb_low - cur) / orb_range
        return "PUT", min(84, 70 + int(ext * 45))

    return None, 0


def intraday_ema_signal(closes: np.ndarray) -> tuple[Optional[str], int]:
    """
    Fast (5) / Slow (13) EMA crossover on 5-min bars.
    Crossover bar = strong signal (70). Aligned continuation = weaker (60).
    """
    if len(closes) < 15:
        return None, 0
    fast_e = ema(closes, 5)
    slow_e = ema(closes, 13)
    # Fresh cross -- strongest
    if fast_e[-1] > slow_e[-1] and fast_e[-2] <= slow_e[-2]:
        return "CALL", 70
    if fast_e[-1] < slow_e[-1] and fast_e[-2] >= slow_e[-2]:
        return "PUT", 70
    # Continuation
    if fast_e[-1] > slow_e[-1] and len(fast_e) >= 4 and fast_e[-1] > fast_e[-3]:
        return "CALL", 60
    if fast_e[-1] < slow_e[-1] and len(fast_e) >= 4 and fast_e[-1] < fast_e[-3]:
        return "PUT", 60
    return None, 0


def market_regime(ohlcv: dict, vwap_val: float,
                  highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> str:
    """
    Classify today's intraday session:
      TREND_UP   -- price above VWAP, expanding range, day moving higher
      TREND_DOWN -- price below VWAP, expanding range, day moving lower
      CHOP       -- oscillating around VWAP, compressed range, no direction
      EARLY      -- not enough bars yet to classify (<30 min of data)
    """
    today = date.today().isoformat()
    today_idx = [i for i, dt in enumerate(ohlcv["datetimes"]) if dt.startswith(today)]
    if len(today_idx) < 6:
        return "EARLY"

    today_closes = [float(ohlcv["closes"][i]) for i in today_idx]
    above = sum(1 for c in today_closes if c > vwap_val)
    pct_above = above / len(today_closes)

    open_px = float(ohlcv["opens"][today_idx[0]])
    cur_px  = float(closes[-1])
    day_chg = (cur_px - open_px) / max(open_px, 1) * 100

    # ATR expansion vs compression
    if len(today_idx) >= 12:
        th = np.array([float(ohlcv["highs"][i])   for i in today_idx])
        tl = np.array([float(ohlcv["lows"][i])    for i in today_idx])
        tc = np.array([float(ohlcv["closes"][i])  for i in today_idx])
        recent_atr_val = atr(th[-6:], tl[-6:], tc[-6:], 5)
        older_atr_val  = atr(th[:6],  tl[:6],  tc[:6],  5)
        expanding = recent_atr_val > older_atr_val * 1.05
    else:
        expanding = True

    if pct_above >= 0.70 and day_chg > 0.15:
        return "TREND_UP"
    if pct_above <= 0.30 and day_chg < -0.15:
        return "TREND_DOWN"
    if not expanding and 0.35 <= pct_above <= 0.65:
        return "CHOP"
    return "EARLY"


def max_pain_level(chain: dict) -> float:
    """
    The strike where total in-the-money options value is minimized at expiry.
    Market makers are delta-neutral and profit most when price pins here.
    On 0DTE (especially Fridays), SPX gravitates toward max pain as
    gamma hedging unwinds into the close.
    Returns 0.0 if chain data is insufficient.
    """
    options = chain.get("options", [])
    if not options:
        return 0.0
    strikes = sorted(set(float(o["strike"]) for o in options))
    if not strikes:
        return 0.0

    by_strike: dict[float, dict] = {}
    for o in options:
        s = float(o["strike"])
        if s not in by_strike:
            by_strike[s] = {"call_oi": 0, "put_oi": 0}
        oi = int(o.get("openInterest", 0) or 0)
        if o["optionType"] == "call":
            by_strike[s]["call_oi"] += oi
        else:
            by_strike[s]["put_oi"] += oi

    min_pain = float("inf")
    pain_strike = 0.0
    for test_px in strikes:
        pain = 0.0
        for strike, d in by_strike.items():
            if test_px > strike:
                pain += (test_px - strike) * d["call_oi"]
            if test_px < strike:
                pain += (strike - test_px) * d["put_oi"]
        if pain < min_pain:
            min_pain = pain
            pain_strike = test_px
    return float(pain_strike)


def spread_ok(contract: dict) -> bool:
    """False if bid-ask spread exceeds 20% of mid -- poor fill quality."""
    bid = float(contract.get("bid", 0) or 0)
    ask = float(contract.get("ask", 0) or 0)
    if not bid or not ask:
        return False
    mid = (bid + ask) / 2
    return (ask - bid) / max(mid, 0.01) <= 0.20


def circuit_breaker_tripped() -> tuple[bool, str]:
    """
    Halt new entries after too many consecutive losses.
    Protects against broken signal environment or unusual market conditions.
    Resets automatically the next trading day.
    """
    if _daily_losses >= cfg.max_daily_losses:
        return True, (f"circuit breaker -- {_daily_losses} consecutive losses today "
                      f"(limit {cfg.max_daily_losses}). Resuming tomorrow.")
    return False, ""


def correlated_blocker(ticker: str, direction: str, confidence: int) -> Optional[str]:
    for group in CORRELATED_GROUPS:
        if ticker not in group:
            continue
        for other, (other_dir, other_conf) in _cycle_traded.items():
            if other in group and other != ticker and other_dir == direction:
                if other_conf >= confidence:
                    return other
    return None


# ---------------------------------------------------------------------------
# Async data fetching
# ---------------------------------------------------------------------------

async def fetch_ohlcv(session: aiohttp.ClientSession, ticker: str) -> Optional[dict]:
    url = (f"https://api.twelvedata.com/time_series"
           f"?symbol={ticker}&interval={cfg.data_interval}&outputsize=200"
           f"&apikey={cfg.twelve_key}")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
        if data.get("status") == "error":
            print(f"  [OHLCV] {ticker}: {data.get('message')}")
            return None
        values = list(reversed(data.get("values", [])))
        if not values:
            return None
        return {
            "opens":     [float(v["open"])   for v in values],
            "closes":    [float(v["close"])  for v in values],
            "highs":     [float(v["high"])   for v in values],
            "lows":      [float(v["low"])    for v in values],
            "volumes":   [float(v["volume"]) for v in values],
            "datetimes": [v["datetime"]      for v in values],
        }
    except Exception as e:
        print(f"  [OHLCV] {ticker} error: {e}")
        return None


async def fetch_vix(session: aiohttp.ClientSession) -> float:
    url = f"https://api.twelvedata.com/quote?symbol=VIX&apikey={cfg.twelve_key}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
        return float(data.get("close", 0) or 0)
    except Exception:
        return 0.0


async def fetch_chain(session: aiohttp.ClientSession, ticker: str) -> Optional[dict]:
    now = time.monotonic()
    if ticker in _chain_cache:
        cached_chain, ts = _chain_cache[ticker]
        if now - ts < cfg.chain_cache_sec:
            return cached_chain

    headers = await _tt_headers(session)
    if not headers:
        return None

    base = cfg.tastytrade_base

    async def _get(url):
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()

    try:
        chain_data, quote_data = await asyncio.gather(
            _get(f"{base}/option-chains/{ticker}/nested"),
            _get(f"{base}/market-data/options/{ticker}"),
        )
    except Exception as e:
        print(f"  [CHAIN] {ticker} fetch error: {e}")
        return None

    raw_expirations = (chain_data.get("data") or {}).get("items") or []
    expirations: list[str] = sorted({
        item.get("expiration-date", "") for item in raw_expirations
        if item.get("expiration-date")
    })

    quote_items = (quote_data.get("data") or {}).get("items") or []
    underlying = float(quote_items[0].get("last", 0) or 0) if quote_items else 0.0

    today = date.today()
    chosen: Optional[str] = None
    chosen_items: list = []
    for item in raw_expirations:
        exp_str = item.get("expiration-date", "")
        if not exp_str:
            continue
        try:
            dte = (date.fromisoformat(exp_str) - today).days
        except ValueError:
            continue
        if cfg.min_dte <= dte <= cfg.max_dte:
            chosen = exp_str
            chosen_items = item.get("strikes") or []
            break

    if not chosen:
        chain = {"expirations": expirations, "options": [], "underlyingPrice": underlying,
                 "chosenExpiration": None, "dte": None}
        _chain_cache[ticker] = (chain, now)
        return chain

    dte_val = (date.fromisoformat(chosen) - today).days
    options: list[dict] = []
    for strike_row in chosen_items:
        strike_price = float(strike_row.get("strike-price", 0) or 0)
        for side in ("call", "put"):
            o = strike_row.get(side)
            if not o:
                continue
            greeks = o.get("greeks") or {}
            options.append({
                "strike":       strike_price,
                "optionType":   side,
                "bid":          float(o.get("bid", 0) or 0),
                "ask":          float(o.get("ask", 0) or 0),
                "last":         float(o.get("last", 0) or 0),
                "volume":       int(o.get("volume", 0) or 0),
                "openInterest": int(o.get("open-interest", 0) or 0),
                "delta":        float(greeks.get("delta", 0) or 0),
                "iv":           float(greeks.get("implied-volatility", 0) or 0),
                "symbol":       o.get("symbol", ""),
            })

    chain = {"expirations": expirations, "options": options,
             "underlyingPrice": underlying, "chosenExpiration": chosen, "dte": dte_val}
    _chain_cache[ticker] = (chain, now)
    return chain


async def fetch_option_quote(session: aiohttp.ClientSession, symbol: str) -> tuple[float, float]:
    headers = await _tt_headers(session)
    if not headers:
        return 0.0, 0.0
    url = f"{cfg.tastytrade_base}/market-data/options/{symbol}"
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=6)) as r:
            data = await r.json()
        items = (data.get("data") or {}).get("items") or []
        q = items[0] if items else {}
        return float(q.get("bid", 0) or 0), float(q.get("ask", 0) or 0)
    except Exception:
        return 0.0, 0.0


async def fetch_earnings(session: aiohttp.ClientSession, ticker: str) -> tuple[Optional[str], Optional[int]]:
    url = f"https://api.twelvedata.com/earnings?symbol={ticker}&apikey={cfg.twelve_key}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
        today = date.today()
        future = [e for e in data.get("earnings", [])
                  if e.get("date") and date.fromisoformat(e["date"]) >= today]
        if not future:
            return None, None
        future.sort(key=lambda e: e["date"])
        d = date.fromisoformat(future[0]["date"])
        return future[0]["date"], (d - today).days
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Intraday signal helpers
# ---------------------------------------------------------------------------

def session_vwap(ohlcv: dict) -> float:
    today = date.today().isoformat()
    cum_pv = cum_v = 0.0
    for i, dt in enumerate(ohlcv["datetimes"]):
        if not dt.startswith(today):
            continue
        tp = (ohlcv["highs"][i] + ohlcv["lows"][i] + ohlcv["closes"][i]) / 3
        vol = ohlcv["volumes"][i]
        cum_pv += tp * vol
        cum_v  += vol
    return cum_pv / max(cum_v, 1.0)


def session_vwap_signal(vwap_val: float, closes: np.ndarray, rsi_val: float) -> tuple[Optional[str], int]:
    if vwap_val <= 0 or len(closes) < 3:
        return None, 0
    cur, prev = float(closes[-1]), float(closes[-2])
    TOL = 0.0015
    if prev < vwap_val and cur >= vwap_val * (1 - TOL) and rsi_val > 48:
        return "CALL", 72
    if prev > vwap_val and cur <= vwap_val * (1 + TOL) and rsi_val < 52:
        return "PUT", 72
    if vwap_val * 0.998 <= cur <= vwap_val * 1.002 and prev >= vwap_val and rsi_val > 45:
        return "CALL", 62
    if vwap_val * 0.998 <= cur <= vwap_val * 1.002 and prev <= vwap_val and rsi_val < 55:
        return "PUT", 62
    return None, 0


def intraday_pivot_signal(ohlcv: dict, closes: np.ndarray, rsi_val: float) -> tuple[Optional[str], int]:
    today = date.today().isoformat()
    prev_bars = [(ohlcv["highs"][i], ohlcv["lows"][i], ohlcv["closes"][i])
                 for i, dt in enumerate(ohlcv["datetimes"]) if not dt.startswith(today)]
    if not prev_bars:
        return None, 0
    H = max(b[0] for b in prev_bars)
    L = min(b[1] for b in prev_bars)
    C = prev_bars[-1][2]
    PP = (H + L + C) / 3
    R1 = 2 * PP - L
    R2 = PP + (H - L)
    S1 = 2 * PP - H
    S2 = PP - (H - L)
    cur    = float(closes[-1])
    prev_c = float(closes[-2]) if len(closes) > 1 else cur
    TOL    = 0.003

    def near(p, lvl):
        return abs(p - lvl) / max(abs(lvl), 1e-9) <= TOL

    if S2 * (1 - TOL) <= cur <= S1 * (1 + TOL) and rsi_val < 50:
        return "CALL", 72 if near(cur, S2) else 62
    if cur > R1 and prev_c <= R1 and rsi_val > 50:
        return "CALL", 68
    if R1 * (1 - TOL) <= cur <= R2 * (1 + TOL) and rsi_val > 50:
        return "PUT", 72 if near(cur, R2) else 62
    if cur < S1 and prev_c >= S1 and rsi_val < 50:
        return "PUT", 68
    return None, 0


# ---------------------------------------------------------------------------
# Signal engine -- 12 strategies
# ---------------------------------------------------------------------------

FAST_STRATEGIES = 12  # trend, MR, breakout, flag, FVG, vwap, candle, pivot,
                      # flow, macd, ORB, intraday EMA


def run_fast_signal_engine(ohlcv: dict, chain: Optional[dict] = None) -> dict:
    closes  = np.array(ohlcv["closes"],  dtype=float)
    highs   = np.array(ohlcv["highs"],   dtype=float)
    lows    = np.array(ohlcv["lows"],    dtype=float)
    volumes = np.array(ohlcv["volumes"], dtype=float)
    opens   = np.array(ohlcv["opens"],   dtype=float)
    rsi_val  = rsi(closes)
    hv30_val = hv(closes, min(30, len(closes) - 1)) * 100
    vwap_val = session_vwap(ohlcv)

    raw: list[dict] = []
    for name, fn in [
        ("trend",     lambda: trend_momentum(closes, rsi_val)),
        ("mr",        lambda: mean_reversion(closes, rsi_val)),
        ("breakout",  lambda: breakout(closes, highs, lows, volumes, rsi_val)),
        ("flag",      lambda: bull_bear_flag(closes, highs, lows, volumes)),
        ("fvg",       lambda: fvg_signal(highs, lows, closes)),
        ("vwap",      lambda: session_vwap_signal(vwap_val, closes, rsi_val)),
        ("candle",    lambda: candlestick_signal(opens, closes, highs, lows, rsi_val)),
        ("pivot",     lambda: intraday_pivot_signal(ohlcv, closes, rsi_val)),
        ("flow",      lambda: options_flow_signal(chain)),
        ("macd",      lambda: macd_entry_signal(closes)),
        ("orb",       lambda: orb_signal(ohlcv, closes, volumes)),
        ("ema_cross", lambda: intraday_ema_signal(closes)),
    ]:
        try:
            d, c = fn()
            if d:
                raw.append({"direction": d, "confidence": c, "strategy": name})
        except Exception:
            pass

    if not raw:
        return {"direction": None, "confidence": 0, "agreement": 0, "signals": [],
                "rsi": round(rsi_val, 1), "vwap": round(vwap_val, 2),
                "hv30": round(hv30_val, 2), "iv_mult": 1.0}

    calls = [s for s in raw if s["direction"] == "CALL"]
    puts  = [s for s in raw if s["direction"] == "PUT"]
    dom   = calls if len(calls) >= len(puts) else puts
    direction = dom[0]["direction"]
    agreement = len(dom)
    avg_conf  = sum(s["confidence"] for s in dom) / len(dom)

    agree_mult = 0.65 + (agreement / FAST_STRATEGIES) * 0.70
    iv_mult    = iv_rank_modifier(chain, hv30_val)
    final_conf = min(100, int(avg_conf * agree_mult * iv_mult))

    return {
        "direction":  direction,
        "confidence": final_conf,
        "agreement":  agreement,
        "signals":    raw,
        "rsi":        round(rsi_val, 1),
        "vwap":       round(vwap_val, 2),
        "hv30":       round(hv30_val, 2),
        "iv_mult":    round(iv_mult, 2),
    }


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def calc_contracts(confidence: int) -> int:
    if confidence < cfg.min_confidence:   return 0
    if confidence >= cfg.tier3_threshold: return cfg.tier3_contracts
    if confidence >= cfg.tier2_threshold: return cfg.tier2_contracts
    return cfg.tier1_contracts


def ticker_max_contracts(ticker: str) -> int:
    key = f"{ticker.replace('^', '').replace('/', '_')}_MAX_CONTRACTS"
    try:
        return max(1, int(os.environ[key]))
    except (KeyError, ValueError):
        return cfg.tier3_contracts


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _sb_insert(table: str, row: dict) -> Optional[str]:
    import requests as _req
    if not cfg.sb_url or not cfg.sb_key:
        return None
    try:
        r = _req.post(
            f"{cfg.sb_url}/rest/v1/{table}",
            headers={"apikey": cfg.sb_key, "Authorization": f"Bearer {cfg.sb_key}",
                     "Content-Type": "application/json", "Prefer": "return=representation"},
            json=row, timeout=8,
        )
        d = r.json()
        return d[0]["id"] if isinstance(d, list) and d else None
    except Exception:
        return None


def _sb_patch(table: str, row_id: str, data: dict) -> None:
    import requests as _req
    if not cfg.sb_url or not cfg.sb_key:
        return
    try:
        _req.patch(
            f"{cfg.sb_url}/rest/v1/{table}?id=eq.{row_id}",
            headers={"apikey": cfg.sb_key, "Authorization": f"Bearer {cfg.sb_key}",
                     "Content-Type": "application/json"},
            json=data, timeout=8,
        )
    except Exception:
        pass


def _get_open_positions() -> list[dict]:
    import requests as _req
    if not cfg.sb_url or not cfg.sb_key:
        return []
    try:
        r = _req.get(
            f"{cfg.sb_url}/rest/v1/positions?status=eq.open&select=*",
            headers={"apikey": cfg.sb_key, "Authorization": f"Bearer {cfg.sb_key}"},
            timeout=8,
        )
        return r.json() if isinstance(r.json(), list) else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Contract selection
# ---------------------------------------------------------------------------

def select_contract(chain: dict, direction: str) -> Optional[dict]:
    opt_type   = "call" if direction == "CALL" else "put"
    underlying = chain.get("underlyingPrice", 0)
    options    = [o for o in chain.get("options", []) if o["optionType"] == opt_type]
    if not options:
        return None

    max_price    = cfg.max_contract_price
    target_delta = 0.50 if cfg.zero_dte else 0.40

    def score(o):
        return abs(abs(float(o.get("delta", 0))) - target_delta)

    def affordable(o) -> bool:
        mid = (o["bid"] + o["ask"]) / 2 if o["bid"] and o["ask"] else o.get("last", 0)
        return max_price <= 0 or mid <= max_price

    liquid = [o for o in options if o["bid"] > 0 and o["ask"] > 0
              and (o["volume"] >= 5 if cfg.zero_dte else o["volume"] >= 10)]
    pool = liquid if liquid else options

    affordable_pool = [o for o in pool if affordable(o)]
    if not affordable_pool:
        print(f"  [CONTRACT] No contracts under ${max_price:.2f} -- relaxing price filter")
        affordable_pool = pool

    affordable_pool.sort(key=score)
    best = affordable_pool[0]
    mid  = round((best["bid"] + best["ask"]) / 2, 2)
    return {**best, "limit_price": mid, "expiry": chain["chosenExpiration"]}


# ---------------------------------------------------------------------------
# Robinhood execution
# ---------------------------------------------------------------------------

_rh_ready = False

def rh_login() -> bool:
    global _rh_ready
    if _rh_ready:
        return True
    if not cfg.rh_user or not cfg.rh_pass:
        print("  [RH] Missing ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD")
        return False
    try:
        import robin_stocks.robinhood as r
        r.login(cfg.rh_user, cfg.rh_pass)
        _rh_ready = True
        print("  [RH] Logged in")
        return True
    except Exception as e:
        print(f"  [RH] Login failed: {e}")
        return False


def get_buying_power() -> float:
    if not rh_login():
        return 0.0
    try:
        import robin_stocks.robinhood as r
        profile = r.profiles.load_account_profile()
        return float(profile.get("option_level_2_long_calls") or
                     profile.get("buying_power") or
                     profile.get("portfolio_cash") or 0)
    except Exception as e:
        print(f"  [RH] Could not fetch buying power: {e}")
        return 0.0


def _place_order(ticker: str, direction: str, contract: dict,
                 contracts: int, dry_run: bool) -> tuple[bool, Optional[str]]:
    opt_type = "call" if direction == "CALL" else "put"
    estimated_cost = round(contract["limit_price"] * contracts * 100, 2)
    print(f"  -> {direction} {ticker} ${contract['strike']} {contract['expiry']} "
          f"x{contracts} @ ${contract['limit_price']:.2f}  (est. ${estimated_cost:.2f})")
    if dry_run:
        print("  [DRY-RUN] No order placed")
        return True, "dry-run"
    if not rh_login():
        return False, None
    buying_power = get_buying_power()
    print(f"  [RH] BP: ${buying_power:.2f}  |  Cost: ${estimated_cost:.2f}")
    if buying_power < estimated_cost:
        print(f"  [SKIP] Insufficient funds -- need ${estimated_cost:.2f}, have ${buying_power:.2f}")
        notify(f"BOT SKIPPED {ticker} {direction} -- Not enough funds",
               f"Need ${estimated_cost:.2f}\nBalance: ${buying_power:.2f}")
        return False, None
    try:
        import robin_stocks.robinhood as r
        result = r.order_buy_option_limit(
            positionEffect="open", creditOrDebit="debit",
            price=contract["limit_price"], symbol=ticker, quantity=contracts,
            expirationDate=contract["expiry"], strike=contract["strike"],
            optionType=opt_type, timeInForce="gfd",
        )
        oid = result.get("id") if result else None
        if oid:
            print(f"  [RH] Order ID: {oid}  Status: {result.get('state', '?')}")
        return bool(oid), oid
    except Exception as e:
        print(f"  [RH] Order error: {e}")
        return False, None


def _sell_to_close(pos: dict, exit_price: float, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY-RUN] Would sell {pos['option_symbol']} x{pos['contracts']}")
        return
    if not rh_login():
        return
    try:
        import robin_stocks.robinhood as r
        r.order_sell_option_limit(
            positionEffect="close", creditOrDebit="credit",
            price=exit_price, symbol=pos["ticker"], quantity=int(pos["contracts"]),
            expirationDate=pos["expiry"], strike=float(pos["strike"]),
            optionType=("call" if pos["direction"] == "CALL" else "put"),
            timeInForce="gfd",
        )
    except Exception as e:
        print(f"  [CLOSE ERROR] {e}")


# ---------------------------------------------------------------------------
# Position management
# ---------------------------------------------------------------------------

async def close_position(session: aiohttp.ClientSession,
                         pos: dict, reason: str, dry_run: bool) -> None:
    global _daily_losses, _daily_pnl

    bid, ask = await fetch_option_quote(session, pos["option_symbol"])
    exit_price = max(bid, 0.01)
    entry = float(pos["entry_price"])
    pnl   = round((exit_price - entry) * int(pos["contracts"]) * 100, 2)
    pct   = (exit_price - entry) / max(entry, 0.01) * 100

    print(f"  [EXIT-{reason.upper()}] {pos['ticker']} {pos['direction']}  "
          f"entry ${entry:.2f} -> exit ${exit_price:.2f}  "
          f"P&L ${pnl:+.2f} ({pct:+.1f}%)")

    # Track for circuit breaker -- consecutive losses only
    _daily_pnl += pnl
    if pnl < 0 and reason != "force_close":
        _daily_losses += 1
        print(f"  [CIRCUIT] Consecutive losses today: {_daily_losses}/{cfg.max_daily_losses}")
    elif pnl > 0:
        _daily_losses = 0  # winning trade resets streak

    outcome = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "SCRATCH"
    reason_label = {
        "stop_loss":          "Stop Loss hit",
        "take_profit":        "Safety-Net Take Profit",
        "trail_stop":         "Trailing Stop hit",
        "signal_reversal":    "Signal Reversed",
        "force_close":        "Force Closed (EOD)",
        "rsi_overbought":     "RSI Overbought",
        "rsi_oversold":       "RSI Oversold",
        "macd_bearish_cross": "MACD Bearish Cross",
        "macd_bullish_cross": "MACD Bullish Cross",
        "bb_upper_touch":     "Bollinger Upper Band",
        "bb_lower_touch":     "Bollinger Lower Band",
        "candle_reversal":    "Reversal Candle",
    }.get(reason, reason)
    notify(
        f"{'[DRY RUN] ' if dry_run else ''}BOT EXITED {pos['ticker']} {pos['direction']} -- {outcome}  ${pnl:+.2f}",
        f"Ticker: {pos['ticker']}\nDirection: {pos['direction']}\nOutcome: {outcome}\n"
        f"P&L: ${pnl:+.2f} ({pct:+.1f}%)\nReason: {reason_label}\n"
        f"Entry: ${entry:.2f} -> Exit: ${exit_price:.2f}\nContracts: {pos['contracts']}\n"
        f"Daily P&L: ${_daily_pnl:+.2f}",
    )
    _sell_to_close(pos, exit_price, dry_run)
    _sb_patch("positions", pos["id"], {
        "closed_at":   datetime.now(timezone.utc).isoformat(),
        "exit_price":  exit_price,
        "exit_reason": reason,
        "pnl":         pnl,
        "status":      "closed",
    })
    if pos.get("signal_id"):
        _sb_patch("signal_log", pos["signal_id"], {"outcome": outcome, "outcome_pnl": pnl})


async def check_position(session: aiohttp.ClientSession,
                         pos: dict, dry_run: bool) -> None:
    entry    = float(pos["entry_price"])
    peak     = float(pos.get("peak_price") or entry)

    bid, ask = await fetch_option_quote(session, pos["option_symbol"])
    if bid <= 0 and ask <= 0:
        return
    mid = (bid + ask) / 2

    new_peak = max(peak, mid)
    _sb_patch("positions", pos["id"], {"current_price": mid, "peak_price": new_peak})

    pnl_pct  = (mid - entry) / max(entry, 0.01)
    peak_pct = (new_peak - entry) / max(entry, 0.01)
    print(f"  [{pos['ticker']} {pos['direction']}] "
          f"${entry:.2f}->${mid:.2f} ({pnl_pct*100:+.1f}%)  peak {peak_pct*100:+.1f}%")

    # Force-close 0DTE before market close
    if pos.get("dte", 99) <= 1 and is_past_force_close():
        await close_position(session, pos, "force_close", dry_run)
        return

    # Stop loss
    if pnl_pct <= -cfg.stop_loss_pct:
        await close_position(session, pos, "stop_loss", dry_run)
        return

    # Tiered trailing stop
    is_0dte = pos.get("dte", 99) <= 1
    trail_start = 0.15 if is_0dte else cfg.trail_start_pct
    if peak_pct >= trail_start:
        if is_0dte:
            if peak_pct >= 2.00:   trail = 0.07
            elif peak_pct >= 0.50: trail = 0.10
            else:                  trail = 0.15
        else:
            if peak_pct >= 3.00:   trail = 0.10
            elif peak_pct >= 1.00: trail = 0.15
            else:                  trail = cfg.trail_stop_pct
        pullback = (new_peak - mid) / max(new_peak, 0.01)
        if pullback >= trail:
            label = "0DTE" if is_0dte else ""
            print(f"  [TRAIL{' '+label if label else ''}] Peak {peak_pct*100:+.0f}%  "
                  f"pullback {pullback*100:.1f}%  trail {trail*100:.0f}%")
            await close_position(session, pos, "trail_stop", dry_run)
            return

    # Safety-net take profit
    if pnl_pct >= cfg.take_profit_pct:
        await close_position(session, pos, "take_profit", dry_run)
        return

    # Chart-based exits
    if cfg.indicator_exit or cfg.signal_exit:
        ohlcv = await fetch_ohlcv(session, pos["ticker"])
        if ohlcv:
            closes_arr = np.array(ohlcv["closes"], dtype=float)
            highs_arr  = np.array(ohlcv["highs"],  dtype=float)
            lows_arr   = np.array(ohlcv["lows"],   dtype=float)
            opens_arr  = np.array(ohlcv["opens"],  dtype=float)
            if cfg.indicator_exit:
                ind_reason = indicator_exit_check(
                    pos["direction"], closes_arr, highs_arr, lows_arr, opens_arr, pnl_pct
                )
                if ind_reason:
                    rsi_now = round(rsi(closes_arr), 1)
                    print(f"  [INDICATOR EXIT] {ind_reason}  RSI={rsi_now}  P&L={pnl_pct*100:+.1f}%")
                    await close_position(session, pos, ind_reason, dry_run)
                    return
            if cfg.signal_exit:
                fresh_chain = await fetch_chain(session, pos["ticker"])
                sig = run_fast_signal_engine(ohlcv, chain=fresh_chain)
                if (sig["direction"] and sig["direction"] != pos["direction"] and
                        sig["agreement"] >= cfg.signal_exit_agree):
                    print(f"  [REVERSAL] -> {sig['direction']} "
                          f"({sig['agreement']} strategies, {sig['confidence']}%)")
                    await close_position(session, pos, "signal_reversal", dry_run)


async def monitor_positions(session: aiohttp.ClientSession, dry_run: bool) -> None:
    positions = await asyncio.get_event_loop().run_in_executor(None, _get_open_positions)
    if not positions:
        return
    print(f"\n[{_now_et().strftime('%H:%M:%S')} ET] Checking {len(positions)} position(s)...")
    await asyncio.gather(*[check_position(session, p, dry_run) for p in positions])


# ---------------------------------------------------------------------------
# Signal scan -- one ticker
# ---------------------------------------------------------------------------

async def scan_ticker(session: aiohttp.ClientSession,
                      ticker: str, dry_run: bool, vix: float) -> None:
    # Fetch OHLCV, chain, and earnings concurrently
    ohlcv, chain, (earnings_date, days_to_earnings) = await asyncio.gather(
        fetch_ohlcv(session, ticker),
        fetch_chain(session, ticker),
        fetch_earnings(session, ticker),
    )

    if not ohlcv:
        print(f"  [{ticker}] No price data")
        return

    closes  = np.array(ohlcv["closes"],  dtype=float)
    highs   = np.array(ohlcv["highs"],   dtype=float)
    lows    = np.array(ohlcv["lows"],    dtype=float)
    volumes = np.array(ohlcv["volumes"], dtype=float)
    vwap_val = session_vwap(ohlcv)

    # Run signal engine
    sig = run_fast_signal_engine(ohlcv, chain=chain)
    fired = ", ".join(s["strategy"] for s in sig["signals"]) or "none"
    iv_tag = f"  IV*{sig['iv_mult']:.2f}" if sig["iv_mult"] != 1.0 else ""

    # Situational awareness -- print every scan
    regime = market_regime(ohlcv, vwap_val, highs, lows, closes)
    orb_h, orb_l, orb_conf = _today_orb(ohlcv)
    orb_str = f"ORB [{orb_l:.0f}-{orb_h:.0f}]" if orb_conf else "ORB [forming]"
    pain = max_pain_level(chain) if chain else 0.0
    pain_str = f"  MaxPain ${pain:.0f}" if pain > 0 else ""

    print(f"  [{ticker}] {sig['direction'] or 'NONE'} {sig['confidence']}%  "
          f"agree {sig['agreement']}/{FAST_STRATEGIES}  RSI {sig['rsi']}"
          f"  VWAP {sig['vwap']:.2f}{iv_tag}  [{fired}]")
    print(f"  [{ticker}] Regime: {regime}  {orb_str}{pain_str}  VIX {vix:.1f}")

    if not sig["direction"]:
        return

    # =========================================================
    # Entry filters -- applied in order of cheapest check first
    # =========================================================

    # 1. Circuit breaker
    tripped, cb_reason = circuit_breaker_tripped()
    if tripped:
        print(f"  [{ticker}] SKIP -- {cb_reason}")
        return

    # 2. VIX level
    if vix > 0 and vix > cfg.max_vix:
        print(f"  [{ticker}] SKIP -- VIX {vix:.1f} > max {cfg.max_vix}")
        return

    # 3. VIX rate-of-change
    vix_spike, vix_spike_reason = _vix_spiked()
    if vix_spike:
        print(f"  [{ticker}] SKIP -- {vix_spike_reason}")
        return

    # 4. News spike (abnormal candle range)
    spike, spike_reason = is_news_spike(highs, lows, closes)
    if spike:
        print(f"  [{ticker}] SKIP -- news spike -- {spike_reason}")
        return

    # 5. Market regime -- no trades in choppy, range-bound conditions
    if regime == "CHOP":
        print(f"  [{ticker}] SKIP -- chop day, no directional edge")
        return

    # 6. Time gate (10 AM - 3:30 PM ET)
    in_window, window_reason = is_trading_window()
    if not in_window:
        print(f"  [{ticker}] SKIP -- {window_reason}")
        return

    # 7. FOMC / CPI / NFP blackout
    near_event, event_name = is_near_high_impact_event()
    if near_event:
        print(f"  [{ticker}] SKIP -- {event_name} blackout")
        return

    # 8. Time decay gate -- after 2 PM theta burns fast on 0DTE
    et = _now_et()
    late_session = et.hour >= 14
    min_conf_req  = 72 if late_session else cfg.min_confidence
    min_strat_req = 4  if late_session else 3  # 0DTE always needs 3+

    if sig["confidence"] < min_conf_req:
        sess_tag = "late session (2PM+)" if late_session else "0DTE"
        print(f"  [{ticker}] SKIP -- conf {sig['confidence']}% < {min_conf_req}% required ({sess_tag})")
        return

    if sig["agreement"] < min_strat_req:
        print(f"  [{ticker}] SKIP -- {sig['agreement']} strategies agree, need {min_strat_req}")
        return

    # 9. Max pain proximity -- price pinning, don't fight it
    if pain > 0:
        underlying = chain.get("underlyingPrice", 0) if chain else float(closes[-1])
        if abs(float(closes[-1]) - pain) / max(underlying, 1) < 0.002:
            print(f"  [{ticker}] SKIP -- price pinned near max pain ${pain:.0f}")
            return

    # 10. Earnings
    if days_to_earnings is not None and days_to_earnings <= 1 and not cfg.zero_dte:
        print(f"  [{ticker}] BLOCK -- earnings in {days_to_earnings}d")
        return

    # 11. Correlation guard
    blocker = correlated_blocker(ticker, sig["direction"], sig["confidence"])
    if blocker:
        print(f"  [{ticker}] SKIP -- correlated with {blocker} "
              f"(already {sig['direction']} this cycle)")
        return

    # 12. Confidence filter + sizing
    contracts = calc_contracts(sig["confidence"])
    if contracts == 0:
        print(f"  [{ticker}] SKIP -- confidence {sig['confidence']}% < min {cfg.min_confidence}%")
        return
    contracts = min(contracts, ticker_max_contracts(ticker))

    global _daily_used
    remaining = cfg.daily_max_contracts - _daily_used
    if remaining <= 0:
        print(f"  [{ticker}] SKIP -- daily max contracts reached")
        return
    contracts = min(contracts, remaining)

    # 13. Chain / expiration
    if not chain or not chain.get("chosenExpiration"):
        print(f"  [{ticker}] SKIP -- no expiration in DTE window {cfg.min_dte}-{cfg.max_dte}")
        return

    # 14. Contract selection
    contract = select_contract(chain, sig["direction"])
    if not contract:
        print(f"  [{ticker}] SKIP -- no suitable contract")
        return

    # 15. Bid-ask spread
    if not spread_ok(contract):
        bid_v, ask_v = contract.get("bid", 0), contract.get("ask", 0)
        mid_v = (bid_v + ask_v) / 2
        spread_v = ask_v - bid_v
        print(f"  [{ticker}] SKIP -- spread too wide "
              f"${spread_v:.2f} ({spread_v/max(mid_v,0.01)*100:.0f}% of mid)")
        return

    # 16. IV rank gate
    atm_iv_val = atm_iv_from_chain(chain) * 100
    iv_stat = iv_status(atm_iv_val, sig["hv30"]) if atm_iv_val and sig["hv30"] else "FAIR"
    print(f"  [{ticker}] -> {contract['symbol']}  mid ${contract['limit_price']:.2f}  "
          f"D{contract['delta']:.2f}  IV {atm_iv_val:.1f}%  {iv_stat}  "
          f"DTE {chain.get('dte', '?')}")

    if iv_stat == "EXPENSIVE" and sig["agreement"] < 4:
        print(f"  [{ticker}] SKIP -- IV expensive, need 4 strategies, got {sig['agreement']}")
        return

    # =========================================================
    # Execute
    # =========================================================

    signal_id = _sb_insert("signal_log", {
        "ticker": ticker, "direction": sig["direction"],
        "confidence": sig["confidence"], "strategy_agreement": sig["agreement"],
        "suggested_strike": contract["strike"],
        "dte_label": f"{chain.get('dte', '?')}d",
        "atm_iv": round(atm_iv_val, 4) if atm_iv_val else None,
        "hv30": round(sig["hv30"] / 100, 4) if sig["hv30"] else None,
        "iv_status": iv_stat,
        "earnings_warning": ("IN_WINDOW" if days_to_earnings and days_to_earnings <= 7 else "NONE"),
        "days_to_earnings": days_to_earnings,
        "matched_contract": contract,
        "price_at_signal": chain.get("underlyingPrice"),
    })

    success, order_id = _place_order(ticker, sig["direction"], contract, contracts, dry_run)
    status = "placed" if success else "failed"
    trade_id = _sb_insert("trades_log", {
        "signal_id": signal_id, "ticker": ticker, "direction": sig["direction"],
        "strike": contract["strike"], "expiry": contract["expiry"],
        "contracts": contracts, "limit_price": contract["limit_price"],
        "robinhood_order_id": order_id, "status": status,
    })

    if success:
        notify(
            f"{'[DRY RUN] ' if dry_run else ''}BOT ENTERED {ticker} {sig['direction']}",
            f"Ticker: {ticker}\nDirection: {sig['direction']}\nContracts: {contracts}\n"
            f"Strike: ${contract['strike']}\nExpiry: {contract.get('expiry','?')}\n"
            f"Fill price: ${contract['limit_price']:.2f}\nConfidence: {sig['confidence']}%\n"
            f"Regime: {regime}  {orb_str}\nContract: {contract['symbol']}",
        )
        if not dry_run:
            _daily_used += contracts
            _cycle_traded[ticker] = (sig["direction"], sig["confidence"])
        _sb_insert("positions", {
            "signal_id": signal_id, "trade_id": trade_id,
            "ticker": ticker, "direction": sig["direction"],
            "option_symbol": contract["symbol"],
            "strike": contract["strike"], "expiry": contract["expiry"],
            "contracts": contracts, "entry_price": contract["limit_price"],
            "entry_underlying": chain.get("underlyingPrice"),
            "peak_price": contract["limit_price"],
            "dte": chain.get("dte"), "status": "open",
        })
        print(f"  Daily contracts: {_daily_used}/{cfg.daily_max_contracts}  "
              f"P&L today: ${_daily_pnl:+.2f}  Losses streak: {_daily_losses}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def async_main(tickers: list[str], dry_run: bool) -> None:
    async with aiohttp.ClientSession() as session:
        if not dry_run:
            await asyncio.get_event_loop().run_in_executor(None, rh_login)

        vix = await fetch_vix(session)
        if vix > 0:
            _record_vix(vix)
            print(f"VIX: {vix:.1f}  (max allowed: {cfg.max_vix})")

        last_scan_t = 0.0
        last_pos_t  = 0.0
        last_vix_t  = time.monotonic()
        VIX_REFRESH = 300

        while True:
            now = time.monotonic()
            _daily_reset()  # no-op unless date changed

            if not is_market_open():
                et = _now_et()
                print(f"[{et.strftime('%H:%M:%S')} ET] Market closed -- waiting 60s...")
                await asyncio.sleep(60)
                continue

            if now - last_pos_t >= cfg.pos_check_sec:
                await monitor_positions(session, dry_run)
                last_pos_t = now

            if now - last_vix_t >= VIX_REFRESH:
                vix = await fetch_vix(session)
                if vix > 0:
                    _record_vix(vix)
                print(f"  [VIX] {vix:.1f}  losses today: {_daily_losses}  "
                      f"daily P&L: ${_daily_pnl:+.2f}")
                last_vix_t = now

            if now - last_scan_t >= cfg.scan_sec:
                _cycle_traded.clear()
                et = _now_et()
                print(f"\n[{et.strftime('%H:%M:%S')} ET] Scanning {tickers}...")
                for t in tickers:
                    await scan_ticker(session, t, dry_run, vix)
                    await asyncio.sleep(0.6)
                last_scan_t = now

            await asyncio.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description="OptionEdge Fast Trader -- 0DTE")
    parser.add_argument("--loop",    action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Signals only, no orders")
    parser.add_argument("--ticker",  nargs="+",           help="Override WATCH_TICKERS")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    tickers = [t.upper() for t in args.ticker] if args.ticker else cfg.watch_tickers
    if not tickers:
        print("ERROR: Set WATCH_TICKERS=SPX or use --ticker SPX")
        sys.exit(1)
    if not cfg.twelve_key:
        print("ERROR: TWELVE_DATA_API_KEY not set")
        sys.exit(1)
    if not cfg.tastytrade_user or not cfg.tastytrade_pass:
        print("ERROR: TASTYTRADE_USERNAME / TASTYTRADE_PASSWORD not set")
        sys.exit(1)

    mode = "0DTE" if cfg.zero_dte else f"{cfg.min_dte}-{cfg.max_dte}DTE"
    print(f"\nOptionEdge Fast Trader -- {cfg.data_interval} bars -- {mode}")
    print(f"Scan: {cfg.scan_sec}s  |  Pos check: {cfg.pos_check_sec}s  |  Force-close: {cfg.force_close_time} ET")
    print(f"Exits: stop {cfg.stop_loss_pct*100:.0f}%  trail start {cfg.trail_start_pct*100:.0f}%  "
          f"target {cfg.take_profit_pct*100:.0f}%")
    print(f"Filters: VIX<={cfg.max_vix}  min conf {cfg.min_confidence}%  "
          f"circuit breaker after {cfg.max_daily_losses} losses")
    if args.dry_run:
        print("** DRY-RUN -- no orders placed **")
    print()

    if args.loop:
        asyncio.run(async_main(tickers, args.dry_run))
    else:
        async def single_pass():
            async with aiohttp.ClientSession() as session:
                vix = await fetch_vix(session)
                if vix > 0:
                    _record_vix(vix)
                await monitor_positions(session, args.dry_run)
                await asyncio.gather(*[
                    scan_ticker(session, t, args.dry_run, vix) for t in tickers
                ])
        asyncio.run(single_pass())


if __name__ == "__main__":
    main()
