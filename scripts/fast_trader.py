#!/usr/bin/env python3
"""
OptionEdge Fast Trader
======================
Intraday execution engine with async concurrent API calls.
Built for SPX/SPY 0DTE and short-dated options.

How it differs from auto_trader.py:
  - asyncio + aiohttp  -> OHLCV and chain fetched in parallel (~2-4s/scan)
  - 5-min bars         -> intraday signals, not daily-close dependent
  - 30s scan / 10s pos -> position checks every 10 seconds
  - chain caching      -> options chain cached 2 min; only quotes are live
  - session VWAP       -> computed from today's 9:30 open, not 20-day rolling
  - force-close        -> all 0DTE positions closed before TRADE_FORCE_CLOSE_TIME
  - VIX filter         -> skips new entries when VIX > TRADE_MAX_VIX
  - concurrent tickers -> all watch-list tickers scanned simultaneously

Usage:
  python scripts/fast_trader.py --dry-run           # signals, no orders
  python scripts/fast_trader.py                     # live single pass
  python scripts/fast_trader.py --loop              # live continuous loop
  python scripts/fast_trader.py --ticker SPX SPY    # override tickers

Extra env vars (all .env.local vars still apply):
  TRADE_DATA_INTERVAL=5min     1min | 5min | 15min
  TRADE_SCAN_SEC=30            seconds between signal scans
  TRADE_POS_CHECK_SEC=10       seconds between position checks
  TRADE_CHAIN_CACHE_SEC=120    chain cache TTL in seconds
  TRADE_FORCE_CLOSE_TIME=15:30 force-close all positions at this time (ET)
  TRADE_MAX_VIX=35             skip new entries if VIX > this level
  TRADE_0DTE=false             true = target same-day expirations (SPXW)

SPX note: Robinhood supports SPX index options. For 0DTE set
  WATCH_TICKERS=SPX TRADE_0DTE=true TRADE_MIN_DTE=0 TRADE_MAX_DTE=1
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
macd_histogram       = _at.macd_histogram
indicator_exit_check = _at.indicator_exit_check


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
    max_contract_price = _ef("TRADE_MAX_CONTRACT_PRICE", 0)  # 0 = no limit
    resend_key       = os.environ.get("RESEND_API_KEY", "")
    notify_email     = os.environ.get("NOTIFY_EMAIL", "")
    notify_phone     = os.environ.get("NOTIFY_PHONE", "")
    watch_tickers   = [t.strip().upper()
                       for t in os.environ.get("WATCH_TICKERS", "").split(",")
                       if t.strip()]
    sb_url  = os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
    sb_key  = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    # Entry sizing
    min_confidence      = _ei("TRADE_MIN_CONFIDENCE", 60)
    tier2_threshold     = _ei("TRADE_TIER2_THRESHOLD", 70)
    tier3_threshold     = _ei("TRADE_TIER3_THRESHOLD", 85)
    tier1_contracts     = _ei("TRADE_TIER1_CONTRACTS", 1)
    tier2_contracts     = _ei("TRADE_TIER2_CONTRACTS", 2)
    tier3_contracts     = _ei("TRADE_TIER3_CONTRACTS", 3)
    daily_max_contracts = _ei("TRADE_DAILY_MAX_CONTRACTS", 6)

    # Exit rules
    stop_loss_pct      = _ef("TRADE_STOP_LOSS_PCT",    0.20)
    take_profit_pct    = _ef("TRADE_TAKE_PROFIT_PCT",  5.00)  # safety net only (500%)
    trail_stop_pct     = _ef("TRADE_TRAIL_STOP_PCT",   0.20)  # base trail 20% off peak
    trail_start_pct    = _ef("TRADE_TRAIL_START_PCT",  0.30)  # activate trail after 30% gain
    signal_exit        = os.environ.get("TRADE_SIGNAL_EXIT", "true").lower() == "true"
    signal_exit_agree  = _ei("TRADE_SIGNAL_EXIT_AGREE", 2)
    indicator_exit     = os.environ.get("TRADE_INDICATOR_EXIT", "true").lower() == "true"

    # Speed
    data_interval     = os.environ.get("TRADE_DATA_INTERVAL", "5min")
    scan_sec          = _ei("TRADE_SCAN_SEC", 30)
    pos_check_sec     = _ei("TRADE_POS_CHECK_SEC", 10)
    chain_cache_sec   = _ei("TRADE_CHAIN_CACHE_SEC", 120)
    force_close_time  = os.environ.get("TRADE_FORCE_CLOSE_TIME", "15:30")
    max_vix           = _ef("TRADE_MAX_VIX", 35.0)
    zero_dte          = os.environ.get("TRADE_0DTE", "false").lower() == "true"

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
# Timezone helper (no pytz dependency)
# ---------------------------------------------------------------------------

def _now_et() -> datetime:
    """Approximate US/Eastern -- UTC-4 during EDT, UTC-5 during EST."""
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
# Tastytrade session auth (async-safe -- login is sync but only called once)
# ---------------------------------------------------------------------------

_tt_session_token: Optional[str] = None
_tt_session_expiry: float = 0.0
_TT_SESSION_TTL = 23 * 3600  # tokens last ~24 h; refresh after 23 h
_tt_lock = asyncio.Lock()


async def _tt_get_token(session: aiohttp.ClientSession) -> Optional[str]:
    """Return a cached Tastytrade session token, refreshing if expired."""
    global _tt_session_token, _tt_session_expiry
    async with _tt_lock:
        if _tt_session_token and time.monotonic() < _tt_session_expiry:
            return _tt_session_token
        if not cfg.tastytrade_user or not cfg.tastytrade_pass:
            print("  [TT] TASTYTRADE_USERNAME / TASTYTRADE_PASSWORD not set")
            return None
        try:
            async with session.post(
                f"{cfg.tastytrade_base}/sessions",
                json={"login": cfg.tastytrade_user, "password": cfg.tastytrade_pass,
                      "remember-me": True},
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
    return {"Authorization": token, "Accept": "application/json", "User-Agent": "personal-option-bot/1.0"} if token else {}


# ---------------------------------------------------------------------------
# Chain cache
# ---------------------------------------------------------------------------

_chain_cache: dict[str, tuple[dict, float]] = {}

# ---------------------------------------------------------------------------
# Correlation guard -- prevents doubling up on the same macro move
# ---------------------------------------------------------------------------

CORRELATED_GROUPS: list[frozenset] = [
    frozenset({"SPX", "SPY", "SPXW"}),
    frozenset({"QQQ", "NDX", "QQQM"}),
]

_cycle_traded: dict[str, tuple[str, int]] = {}


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
            "opens":    [float(v["open"])   for v in values],
            "closes":   [float(v["close"])  for v in values],
            "highs":    [float(v["high"])   for v in values],
            "lows":     [float(v["low"])    for v in values],
            "volumes":  [float(v["volume"]) for v in values],
            "datetimes":[v["datetime"]       for v in values],
        }
    except Exception as e:
        print(f"  [OHLCV] {ticker} error: {e}")
        return None


async def fetch_vix(session: aiohttp.ClientSession) -> float:
    url = (f"https://api.twelvedata.com/quote"
           f"?symbol=VIX&apikey={cfg.twelve_key}")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
        return float(data.get("close", 0) or 0)
    except Exception:
        return 0.0


async def fetch_chain(session: aiohttp.ClientSession, ticker: str) -> Optional[dict]:
    """Fetch options chain from Tastytrade nested endpoint, with per-ticker caching."""
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
    """Returns (bid, ask) for an option symbol from Tastytrade."""
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
# Intraday-aware signal helpers
# ---------------------------------------------------------------------------

def session_vwap(ohlcv: dict) -> float:
    """VWAP computed from today's session bars only."""
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
    """Signal on VWAP reclaim, rejection, and bounce using today's session VWAP."""
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
    """Pivot points computed from prior day's intraday bars (no separate daily fetch needed)."""
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


FAST_STRATEGIES = 9  # trend, MR, breakout, flag, FVG, vwap, candle, pivot, flow


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
        ("trend",      lambda: trend_momentum(closes, rsi_val)),
        ("mr",         lambda: mean_reversion(closes, rsi_val)),
        ("breakout",   lambda: breakout(closes, highs, lows, volumes, rsi_val)),
        ("flag",       lambda: bull_bear_flag(closes, highs, lows, volumes)),
        ("fvg",        lambda: fvg_signal(highs, lows, closes)),
        ("vwap",       lambda: session_vwap_signal(vwap_val, closes, rsi_val)),
        ("candle",     lambda: candlestick_signal(opens, closes, highs, lows, rsi_val)),
        ("pivot",      lambda: intraday_pivot_signal(ohlcv, closes, rsi_val)),
        ("flow",       lambda: options_flow_signal(chain)),
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
    if confidence < cfg.min_confidence:    return 0
    if confidence >= cfg.tier3_threshold:  return cfg.tier3_contracts
    if confidence >= cfg.tier2_threshold:  return cfg.tier2_contracts
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

    max_price = cfg.max_contract_price

    target_delta = 0.50 if cfg.zero_dte else 0.40

    def score(o):
        d = abs(float(o.get("delta", 0)))
        return abs(d - target_delta)

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
        bp = float(profile.get("option_level_2_long_calls") or
                   profile.get("buying_power") or
                   profile.get("portfolio_cash") or 0)
        return bp
    except Exception as e:
        print(f"  [RH] Could not fetch buying power: {e}")
        return 0.0


def _place_order(ticker: str, direction: str, contract: dict,
                 contracts: int, dry_run: bool) -> tuple[bool, Optional[str]]:
    opt_type = "call" if direction == "CALL" else "put"
    estimated_cost = round(contract["limit_price"] * contracts * 100, 2)
    print(f"  -> {direction} {ticker} ${contract['strike']} {contract['expiry']} "
          f"x{contracts} @ ${contract['limit_price']:.2f}  (est. cost ${estimated_cost:.2f})")
    if dry_run:
        print("  [DRY-RUN] No order placed")
        return True, "dry-run"
    if not rh_login():
        return False, None

    buying_power = get_buying_power()
    print(f"  [RH] Buying power: ${buying_power:.2f}  |  Trade cost: ${estimated_cost:.2f}")
    if buying_power < estimated_cost:
        print(f"  [SKIP] Insufficient funds -- need ${estimated_cost:.2f}, have ${buying_power:.2f}")
        notify(f"BOT SKIPPED {ticker} {direction} -- Not enough funds",
               f"Need ${estimated_cost:.2f} to place trade\nRobinhood balance: ${buying_power:.2f}\n"
               f"Add funds to your Robinhood account.")
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

_daily_used = 0


async def close_position(session: aiohttp.ClientSession,
                         pos: dict, reason: str, dry_run: bool) -> None:
    bid, ask = await fetch_option_quote(session, pos["option_symbol"])
    exit_price = max(bid, 0.01)
    entry = float(pos["entry_price"])
    pnl   = round((exit_price - entry) * int(pos["contracts"]) * 100, 2)
    pct   = (exit_price - entry) / max(entry, 0.01) * 100

    print(f"  [EXIT-{reason.upper()}] {pos['ticker']} {pos['direction']}  "
          f"entry ${entry:.2f} -> exit ${exit_price:.2f}  "
          f"P&L ${pnl:+.2f} ({pct:+.1f}%)")

    outcome = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "SCRATCH"
    reason_label = {
        "stop_loss":          "Stop Loss hit",
        "take_profit":        "Safety-Net Take Profit",
        "trail_stop":         "Trailing Stop hit",
        "signal_reversal":    "Signal Reversed",
        "reversal":           "Signal Reversed",
        "force_close":        "Force Closed (EOD)",
        "rsi_overbought":     "RSI Overbought -- locking gains",
        "rsi_oversold":       "RSI Oversold -- locking gains",
        "macd_bearish_cross": "MACD Bearish Cross",
        "macd_bullish_cross": "MACD Bullish Cross",
        "bb_upper_touch":     "Bollinger Upper Band -- stretched",
        "bb_lower_touch":     "Bollinger Lower Band -- stretched",
        "candle_reversal":    "Reversal Candle Pattern",
    }.get(reason, reason)
    notify(
        f"{'[DRY RUN] ' if dry_run else ''}BOT EXITED {pos['ticker']} {pos['direction']} -- {outcome}  ${pnl:+.2f}",
        f"Ticker: {pos['ticker']}\nDirection: {pos['direction']}\nOutcome: {outcome}\n"
        f"P&L: ${pnl:+.2f} ({pct:+.1f}%)\nReason: {reason_label}\n"
        f"Entry: ${entry:.2f} -> Exit: ${exit_price:.2f}\nContracts: {pos['contracts']}",
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
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "SCRATCH")
        _sb_patch("signal_log", pos["signal_id"], {"outcome": outcome, "outcome_pnl": pnl})


async def check_position(session: aiohttp.ClientSession,
                         pos: dict, dry_run: bool) -> None:
    entry    = float(pos["entry_price"])
    peak     = float(pos.get("peak_price") or entry)
    contracts = int(pos["contracts"])

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

    # Tiered trailing stop -- rides the move, never caps it
    if peak_pct >= cfg.trail_start_pct:
        if peak_pct >= 3.00:    # +300%+ -> very tight trail (10%)
            trail = 0.10
        elif peak_pct >= 1.00:  # +100%+ -> tighter trail (15%)
            trail = 0.15
        else:                   # +30-100% -> base trail from config
            trail = cfg.trail_stop_pct
        pullback = (new_peak - mid) / max(new_peak, 0.01)
        if pullback >= trail:
            print(f"  [TRAIL] Peak {peak_pct*100:+.0f}%  pullback {pullback*100:.1f}%  trail {trail*100:.0f}%")
            await close_position(session, pos, "trail_stop", dry_run)
            return

    # Safety-net take profit (500% default -- almost never fires)
    if pnl_pct >= cfg.take_profit_pct:
        await close_position(session, pos, "take_profit", dry_run)
        return

    # Chart-based exits (indicator checks + signal reversal)
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
    ohlcv, chain, (earnings_date, days_to_earnings) = await asyncio.gather(
        fetch_ohlcv(session, ticker),
        fetch_chain(session, ticker),
        fetch_earnings(session, ticker),
    )

    if not ohlcv:
        print(f"  [{ticker}] No price data")
        return

    sig = run_fast_signal_engine(ohlcv, chain=chain)
    fired = ", ".join(s["strategy"] for s in sig["signals"]) or "none"
    iv_tag = f"  IV*{sig['iv_mult']:.2f}" if sig["iv_mult"] != 1.0 else ""
    print(f"  [{ticker}] {sig['direction'] or 'NONE'} {sig['confidence']}%  "
          f"agree {sig['agreement']}/{FAST_STRATEGIES}  RSI {sig['rsi']}"
          f"  VWAP {sig['vwap']:.2f}{iv_tag}  [{fired}]")

    if not sig["direction"]:
        return

    if vix > 0 and vix > cfg.max_vix:
        print(f"  [{ticker}] SKIP -- VIX {vix:.1f} > max {cfg.max_vix}")
        return

    if days_to_earnings is not None and days_to_earnings <= 1 and not cfg.zero_dte:
        print(f"  [{ticker}] BLOCK -- earnings in {days_to_earnings}d")
        return

    blocker = correlated_blocker(ticker, sig["direction"], sig["confidence"])
    if blocker:
        print(f"  [{ticker}] SKIP -- correlated with {blocker} "
              f"(already in {sig['direction']} this cycle)")
        return

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

    if not chain or not chain.get("chosenExpiration"):
        print(f"  [{ticker}] SKIP -- no expiration in DTE window {cfg.min_dte}-{cfg.max_dte}")
        return

    contract = select_contract(chain, sig["direction"])
    if not contract:
        print(f"  [{ticker}] SKIP -- no suitable contract")
        return

    atm_iv_val = atm_iv_from_chain(chain) * 100
    iv_stat = iv_status(atm_iv_val, sig["hv30"]) if atm_iv_val and sig["hv30"] else "FAIR"
    print(f"  [{ticker}] -> {contract['symbol']}  mid ${contract['limit_price']:.2f}  "
          f"D{contract['delta']:.2f}  IV {atm_iv_val:.1f}%  {iv_stat}  "
          f"DTE {chain.get('dte', '?')}")

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
            f"Contract: {contract['symbol']}",
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
        print(f"  Daily contracts: {_daily_used}/{cfg.daily_max_contracts}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def async_main(tickers: list[str], dry_run: bool) -> None:
    async with aiohttp.ClientSession() as session:
        if not dry_run:
            await asyncio.get_event_loop().run_in_executor(None, rh_login)

        vix = await fetch_vix(session)
        if vix > 0:
            print(f"VIX: {vix:.1f}  (max allowed: {cfg.max_vix})")

        last_scan_t  = 0.0
        last_pos_t   = 0.0
        last_vix_t   = time.monotonic()
        VIX_REFRESH  = 300

        while True:
            now = time.monotonic()

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
                print(f"  [VIX] {vix:.1f}")
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
    parser = argparse.ArgumentParser(description="OptionEdge Fast Trader -- intraday/0DTE")
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
    print(f"OptionEdge Fast Trader -- {cfg.data_interval} bars -- {mode} -- "
          f"scan {cfg.scan_sec}s / pos {cfg.pos_check_sec}s")
    print(f"Exits: stop {cfg.stop_loss_pct*100:.0f}%  "
          f"trail starts +{cfg.trail_start_pct*100:.0f}%  "
          f"base trail {cfg.trail_stop_pct*100:.0f}%  "
          f"force-close {cfg.force_close_time} ET")
    if args.dry_run:
        print("** DRY-RUN -- no orders placed **")

    if args.loop:
        asyncio.run(async_main(tickers, args.dry_run))
    else:
        async def single_pass():
            async with aiohttp.ClientSession() as session:
                vix = await fetch_vix(session)
                await monitor_positions(session, args.dry_run)
                await asyncio.gather(*[
                    scan_ticker(session, t, args.dry_run, vix) for t in tickers
                ])
        asyncio.run(single_pass())


if __name__ == "__main__":
    main()
