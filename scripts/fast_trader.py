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
    max_contract_price = _ef("TRADE_MAX_CONTRACT_PRICE", 4.50)
    resend_key       = os.environ.get("RESEND_API_KEY", "")
    notify_email     = os.environ.get("NOTIFY_EMAIL", "")
    notify_phone     = os.environ.get("NOTIFY_PHONE", "")
    watch_tickers    = [t.strip().upper()
                        for t in os.environ.get("WATCH_TICKERS", "SPX").split(",")
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
    daily_max_contracts = _ei("TRADE_DAILY_MAX_CONTRACTS", 1)

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
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Daily state
# ---------------------------------------------------------------------------

_daily_used       = 0
_daily_losses     = 0
_daily_pnl        = 0.0
_last_reset_date  = ""
_cycle_traded: set = set()

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
# VIX spike tracker
# ---------------------------------------------------------------------------

_vix_history: list[tuple[float, float]] = []  # (timestamp, vix)

def _record_vix(vix: float) -> None:
    now = time.time()
    _vix_history.append((now, vix))
    cutoff = now - 600  # keep last 10 minutes
    while _vix_history and _vix_history[0][0] < cutoff:
        _vix_history.pop(0)

def _vix_spiked(threshold: float = 2.5) -> tuple[bool, str]:
    if len(_vix_history) < 2:
        return False, ""
    oldest_vix = _vix_history[0][1]
    newest_vix = _vix_history[-1][1]
    delta = newest_vix - oldest_vix
    if delta >= threshold:
        return True, (f"VIX jumped +{delta:.1f} pts in last 10 min "
                      f"({oldest_vix:.1f} -> {newest_vix:.1f}) -- systemic shock")
    return False, ""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

_ET = timezone(timedelta(hours=-4))  # EDT; adjust to -5 for EST

def _now_et() -> datetime:
    return datetime.now(_ET)


# ---------------------------------------------------------------------------
# Opening Range Breakout
# ---------------------------------------------------------------------------

def _today_orb(ohlcv: dict) -> tuple[float, float, bool]:
    """Return (orb_high, orb_low, confirmed) from today's 9:30-10:00 bars."""
    today = date.today().isoformat()
    et    = _now_et()
    h_vals, l_vals = [], []
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
    orb_hi, orb_lo, confirmed = _today_orb(ohlcv)
    if not confirmed or orb_hi == 0:
        return None, 0
    price = float(closes[-1])
    if orb_lo < price < orb_hi:  # inside range -- no signal
        return None, 0
    if len(volumes) < 2:
        return None, 0
    avg_vol = float(np.mean(volumes[-10:])) if len(volumes) >= 10 else float(np.mean(volumes))
    vol_ok  = float(volumes[-1]) >= avg_vol * 1.2
    if price > orb_hi:
        conf = 84 if vol_ok else 70
        return "CALL", conf
    else:
        conf = 84 if vol_ok else 70
        return "PUT", conf


# ---------------------------------------------------------------------------
# Intraday EMA cross signal
# ---------------------------------------------------------------------------

def intraday_ema_signal(closes: np.ndarray) -> tuple[Optional[str], int]:
    if len(closes) < 14:
        return None, 0
    fast = ema(closes, 5)
    slow = ema(closes, 13)
    if fast[-1] > slow[-1] and fast[-2] <= slow[-2]:
        return "CALL", 70  # fresh cross
    if fast[-1] < slow[-1] and fast[-2] >= slow[-2]:
        return "PUT", 70
    if fast[-1] > slow[-1]:
        return "CALL", 60  # continuation
    if fast[-1] < slow[-1]:
        return "PUT", 60
    return None, 0


# ---------------------------------------------------------------------------
# Market regime
# ---------------------------------------------------------------------------

def market_regime(ohlcv: dict, vwap_val: float,
                  highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> str:
    if len(closes) < 20:
        return "EARLY"
    above = sum(1 for c in closes[-20:] if c > vwap_val)
    pct_above = above / 20
    atr_val = atr(highs[-21:], lows[-21:], closes[-21:], 20) if len(closes) >= 21 else 0
    price_range = float(closes[-1]) - float(closes[-20])
    if pct_above >= 0.65 and atr_val > 0 and abs(price_range) > atr_val * 0.5:
        return "TREND_UP"
    if pct_above <= 0.35 and atr_val > 0 and abs(price_range) > atr_val * 0.5:
        return "TREND_DOWN"
    if 0.40 <= pct_above <= 0.60:
        return "CHOP"
    return "EARLY"


# ---------------------------------------------------------------------------
# Max pain
# ---------------------------------------------------------------------------

def max_pain_level(chain: dict) -> float:
    strikes = {}
    for o in chain.get("options", []):
        k  = float(o.get("strike", 0))
        oi = int(o.get("open_interest", 0) or 0)
        if k <= 0:
            continue
        strikes.setdefault(k, {"call": 0, "put": 0})
        if o["optionType"] == "call":
            strikes[k]["call"] += oi
        else:
            strikes[k]["put"]  += oi
    if not strikes:
        return 0.0
    best_k, best_pain = 0.0, float("inf")
    for candidate in strikes:
        pain = 0.0
        for k, oi in strikes.items():
            pain += max(0, candidate - k) * oi["call"]
            pain += max(0, k - candidate) * oi["put"]
        if pain < best_pain:
            best_pain, best_k = pain, candidate
    return best_k


# ---------------------------------------------------------------------------
# Spread check
# ---------------------------------------------------------------------------

def spread_ok(contract: dict) -> bool:
    bid = contract.get("bid", 0) or 0
    ask = contract.get("ask", 0) or 0
    if bid <= 0 or ask <= 0:
        return False
    mid = (bid + ask) / 2
    return (ask - bid) / mid <= 0.20


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def circuit_breaker_tripped() -> tuple[bool, str]:
    if _daily_losses >= cfg.max_daily_losses:
        return True, (f"circuit breaker -- {_daily_losses} consecutive losses "
                      f"(limit {cfg.max_daily_losses}). Resuming tomorrow.")
    return False, ""


# ---------------------------------------------------------------------------
# Twelve Data async helpers
# ---------------------------------------------------------------------------

async def _fetch_json(session: aiohttp.ClientSession, url: str, params: dict) -> dict:
    params = {"apikey": cfg.twelve_key, **params}
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        return await r.json()


async def fetch_ohlcv(session: aiohttp.ClientSession, ticker: str, interval: str,
                      outputsize: int = 100) -> dict:
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": ticker, "interval": interval,
              "outputsize": outputsize, "order": "ASC"}
    raw = await _fetch_json(session, url, params)
    if raw.get("status") == "error" or "values" not in raw:
        return {}
    vals = raw["values"]
    return {
        "datetimes": [v["datetime"] for v in vals],
        "opens":     np.array([float(v["open"])   for v in vals]),
        "highs":     np.array([float(v["high"])   for v in vals]),
        "lows":      np.array([float(v["low"])    for v in vals]),
        "closes":    np.array([float(v["close"])  for v in vals]),
        "volumes":   np.array([float(v.get("volume", 0)) for v in vals]),
    }


async def fetch_quote(session: aiohttp.ClientSession, ticker: str) -> dict:
    url = "https://api.twelvedata.com/quote"
    return await _fetch_json(session, url, {"symbol": ticker})


async def fetch_vix(session: aiohttp.ClientSession) -> float:
    raw = await fetch_quote(session, "VIX")
    try:
        return float(raw.get("close") or raw.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Tastytrade chain helpers (sync, cached)
# ---------------------------------------------------------------------------

_chain_cache: dict[str, tuple[float, dict]] = {}  # ticker -> (timestamp, chain)

async def fetch_chain_async(session: aiohttp.ClientSession,
                            ticker: str, min_dte: int, max_dte: int) -> dict:
    now = time.time()
    if ticker in _chain_cache:
        ts, cached = _chain_cache[ticker]
        if now - ts < cfg.chain_cache_sec:
            return cached

    token = await _tastytrade_token_async(session)
    if not token:
        return {}

    # Fetch nested chain (expirations + strikes in one call) and underlying quote concurrently
    chain_url = f"{cfg.tastytrade_base}/option-chains/{ticker}/nested"
    quote_url = f"{cfg.tastytrade_base}/market-data/quotes"
    headers   = {"Authorization": token,
                 "User-Agent": "OptionEdge/1.0",
                 "Content-Type": "application/json"}

    async def _get_chain():
        async with session.get(chain_url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json()

    async def _get_quote():
        syms = [ticker if not ticker.startswith("SPX") else "SPX"]
        async with session.get(quote_url, headers=headers,
                               params={"symbols[]": syms},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()

    chain_raw, quote_raw = await asyncio.gather(_get_chain(), _get_quote(), return_exceptions=True)

    if isinstance(chain_raw, Exception) or isinstance(quote_raw, Exception):
        return {}

    # Parse underlying price
    underlying = 0.0
    try:
        items = quote_raw.get("data", {}).get("items", [])
        if items:
            q = items[0]
            underlying = float(q.get("last") or q.get("mark") or q.get("bid") or 0)
    except Exception:
        pass

    # Pick expiration
    today = date.today()
    chosen_exp, chosen_items = None, []
    try:
        exps = chain_raw.get("data", {}).get("items", [])
        for exp_row in exps:
            exp_str = exp_row.get("expiration-date", "")
            try:
                exp_date = date.fromisoformat(exp_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if min_dte <= dte <= max_dte:
                chosen_exp   = exp_str
                chosen_items = exp_row.get("strikes") or []
                break
    except Exception:
        pass

    if not chosen_exp:
        return {}

    # Flatten strikes into option list
    options = []
    for strike_row in chosen_items:
        strike_price = float(strike_row.get("strike-price", 0) or 0)
        for side in ("call", "put"):
            o = strike_row.get(side)
            if not o:
                continue
            options.append({
                "optionType":     side,
                "strike":         strike_price,
                "bid":            float(o.get("bid",  0) or 0),
                "ask":            float(o.get("ask",  0) or 0),
                "last":           float(o.get("last", 0) or 0),
                "volume":         int(  o.get("volume", 0) or 0),
                "open_interest":  int(  o.get("open-interest", 0) or 0),
                "delta":          float(o.get("delta", 0) or 0),
                "iv":             float(o.get("implied-volatility", 0) or 0),
            })

    result = {
        "options":            options,
        "chosenExpiration":   chosen_exp,
        "underlyingPrice":    underlying,
    }
    _chain_cache[ticker] = (now, result)
    return result


_tt_token: Optional[str]  = None
_tt_token_ts: float        = 0.0
_TT_TOKEN_TTL              = 3600

async def _tastytrade_token_async(session: aiohttp.ClientSession) -> Optional[str]:
    global _tt_token, _tt_token_ts
    if _tt_token and (time.time() - _tt_token_ts) < _TT_TOKEN_TTL:
        return _tt_token
    if not cfg.tastytrade_user or not cfg.tastytrade_pass:
        return None
    url  = f"{cfg.tastytrade_base}/sessions"
    body = {"login": cfg.tastytrade_user, "password": cfg.tastytrade_pass}
    headers = {"User-Agent": "OptionEdge/1.0", "Content-Type": "application/json"}
    try:
        async with session.post(url, json=body, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
            tok  = (data.get("data") or {}).get("session-token")
            if tok:
                _tt_token    = tok
                _tt_token_ts = time.time()
                return tok
    except Exception as e:
        print(f"  [TT] Auth error: {e}")
    return None


# ---------------------------------------------------------------------------
# Session VWAP
# ---------------------------------------------------------------------------

def session_vwap(ohlcv: dict) -> float:
    today = date.today().isoformat()
    tvol, tpv = 0.0, 0.0
    for i, dt in enumerate(ohlcv.get("datetimes", [])):
        if not dt.startswith(today):
            continue
        h = float(ohlcv["highs"][i])
        l = float(ohlcv["lows"][i])
        c = float(ohlcv["closes"][i])
        v = float(ohlcv["volumes"][i])
        tvol += v
        tpv  += ((h + l + c) / 3) * v
    return tpv / tvol if tvol > 0 else 0.0


# ---------------------------------------------------------------------------
# Intraday pivot levels
# ---------------------------------------------------------------------------

def intraday_pivots(highs: np.ndarray, lows: np.ndarray,
                    closes: np.ndarray) -> dict:
    if len(closes) < 2:
        return {}
    ph, pl, pc = float(highs[-2]), float(lows[-2]), float(closes[-2])
    p  = (ph + pl + pc) / 3
    r1 = 2 * p - pl
    s1 = 2 * p - ph
    r2 = p + (ph - pl)
    s2 = p - (ph - pl)
    return {"P": p, "R1": r1, "R2": r2, "S1": s1, "S2": s2}


def pivot_signal(closes: np.ndarray, pivots: dict) -> tuple[Optional[str], int]:
    if not pivots or len(closes) < 2:
        return None, 0
    price = float(closes[-1])
    prev  = float(closes[-2])
    r1, s1 = pivots.get("R1", 0), pivots.get("S1", 0)
    if prev <= r1 < price:
        return "CALL", 68
    if prev >= s1 > price:
        return "PUT", 68
    return None, 0


# ---------------------------------------------------------------------------
# Signal aggregator
# ---------------------------------------------------------------------------

FAST_STRATEGIES = 12  # trend, MR, breakout, flag, FVG, vwap, candle, pivot,
                      # flow, macd, ORB, intraday EMA


def aggregate_signals(
    ohlcv: dict, chain: dict,
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
    volumes: np.ndarray, vwap_val: float,
) -> dict:
    signals: list[tuple[Optional[str], int]] = []

    # 1 trend-momentum
    d, c = trend_momentum(highs, lows, closes)
    signals.append((d, c))

    # 2 mean-reversion
    d, c = mean_reversion(closes)
    signals.append((d, c))

    # 3 breakout
    d, c = breakout(highs, lows, closes)
    signals.append((d, c))

    # 4 bull/bear flag
    d, c = bull_bear_flag(closes)
    signals.append((d, c))

    # 5 FVG
    d, c = fvg_signal(highs, lows, closes)
    signals.append((d, c))

    # 6 session VWAP
    price = float(closes[-1])
    if vwap_val > 0:
        if price > vwap_val * 1.001:
            signals.append(("CALL", 65))
        elif price < vwap_val * 0.999:
            signals.append(("PUT", 65))
        else:
            signals.append((None, 0))
    else:
        signals.append((None, 0))

    # 7 candlestick
    d, c = candlestick_signal(highs, lows, closes)
    signals.append((d, c))

    # 8 intraday pivots
    pivots = intraday_pivots(highs, lows, closes)
    d, c   = pivot_signal(closes, pivots)
    signals.append((d, c))

    # 9 options flow
    d, c = options_flow_signal(chain)
    signals.append((d, c))

    # 10 MACD entry
    d, c = macd_entry_signal(closes)
    signals.append((d, c))

    # 11 ORB
    d, c = orb_signal(ohlcv, closes, volumes)
    signals.append((d, c))

    # 12 Intraday EMA cross
    d, c = intraday_ema_signal(closes)
    signals.append((d, c))

    calls  = [(d, c) for d, c in signals if d == "CALL" and c > 0]
    puts   = [(d, c) for d, c in signals if d == "PUT"  and c > 0]
    call_n, put_n = len(calls), len(puts)

    if call_n == 0 and put_n == 0:
        return {"direction": None, "confidence": 0, "agreement": 0, "rsi": 50}

    direction  = "CALL" if call_n >= put_n else "PUT"
    agreement  = call_n if direction == "CALL" else put_n
    avg_conf   = sum(c for _, c in (calls if direction == "CALL" else puts)) / agreement
    agree_mult = 0.65 + (agreement / FAST_STRATEGIES) * 0.70
    confidence = int(min(98, avg_conf * agree_mult))

    rsi_val = 50
    try:
        r = rsi(closes, 14)
        rsi_val = int(r) if r else 50
    except Exception:
        pass

    return {
        "direction":  direction,
        "confidence": confidence,
        "agreement":  agreement,
        "rsi":        rsi_val,
    }


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def size_contracts(confidence: int) -> int:
    if confidence >= cfg.tier3_threshold:
        return cfg.tier3_contracts
    if confidence >= cfg.tier2_threshold:
        return cfg.tier2_contracts
    return cfg.tier1_contracts


# ---------------------------------------------------------------------------
# Correlation guard
# ---------------------------------------------------------------------------

_price_history: dict[str, list[float]] = {}

def _record_price(ticker: str, price: float) -> None:
    _price_history.setdefault(ticker, []).append(price)
    if len(_price_history[ticker]) > 30:
        _price_history[ticker].pop(0)

def correlation_ok(new_ticker: str, open_tickers: list[str]) -> bool:
    if not open_tickers or new_ticker not in _price_history:
        return True
    new_arr = np.array(_price_history[new_ticker])
    if len(new_arr) < 5:
        return True
    for ot in open_tickers:
        if ot not in _price_history:
            continue
        ot_arr = np.array(_price_history[ot])
        n = min(len(new_arr), len(ot_arr))
        if n < 5:
            continue
        corr = float(np.corrcoef(new_arr[-n:], ot_arr[-n:])[0, 1])
        if abs(corr) > 0.80:
            return False
    return True


# ---------------------------------------------------------------------------
# Earnings check
# ---------------------------------------------------------------------------

async def days_to_earnings_async(
        session: aiohttp.ClientSession, ticker: str) -> Optional[int]:
    try:
        raw = await _fetch_json(
            session, "https://api.twelvedata.com/earnings",
            {"symbol": ticker, "outputsize": "1"},
        )
        rows = raw.get("earnings", [])
        if not rows:
            return None
        earn_date = date.fromisoformat(rows[0]["date"])
        return (earn_date - date.today()).days
    except Exception:
        return None


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

    # For 0DTE: prefer ATM (delta closer to 0.50 for max leverage)
    # For longer DTE: prefer slightly OTM (delta ~0.40)
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


def place_order_rh(
        ticker: str, direction: str, contracts: int,
        strike: float, expiry: str, limit_price: float,
        dry_run: bool = False) -> Optional[str]:

    cost = limit_price * 100 * contracts
    bp   = get_buying_power()
    if bp > 0 and cost > bp:
        print(f"  [RH] Insufficient buying power: need ${cost:.2f}, have ${bp:.2f}")
        return None

    opt_type = "call" if direction == "CALL" else "put"
    print(f"  {'[DRY-RUN] ' if dry_run else ''}ORDER {direction} {ticker} "
          f"${strike} {expiry} x{contracts} @ ${limit_price:.2f} "
          f"(cost ~${cost:.2f})")

    if dry_run:
        return "dry-run-ok"

    if not rh_login():
        return None
    try:
        import robin_stocks.robinhood as r
        res = r.orders.order_buy_option_limit(
            positionEffect="open",
            creditOrDebit="debit",
            price=str(round(limit_price, 2)),
            symbol=ticker,
            quantity=contracts,
            expirationDate=expiry,
            strike=str(strike),
            optionType=opt_type,
            timeInForce="gfd",
        )
        order_id = res.get("id") if isinstance(res, dict) else None
        print(f"  [RH] Order submitted: {order_id}")
        return order_id
    except Exception as e:
        print(f"  [RH] Order error: {e}")
        return None


def close_position_rh(
        ticker: str, direction: str, contracts: int,
        strike: float, expiry: str, limit_price: float,
        dry_run: bool = False) -> bool:

    opt_type = "call" if direction == "CALL" else "put"
    print(f"  {'[DRY-RUN] ' if dry_run else ''}CLOSE {direction} {ticker} "
          f"${strike} {expiry} x{contracts} @ ${limit_price:.2f}")

    if dry_run:
        return True

    if not rh_login():
        return False
    try:
        import robin_stocks.robinhood as r
        res = r.orders.order_sell_option_limit(
            positionEffect="close",
            creditOrDebit="credit",
            price=str(round(limit_price, 2)),
            symbol=ticker,
            quantity=contracts,
            expirationDate=expiry,
            strike=str(strike),
            optionType=opt_type,
            timeInForce="gfd",
        )
        return bool(res)
    except Exception as e:
        print(f"  [RH] Close error: {e}")
        return False


# ---------------------------------------------------------------------------
# Position tracker
# ---------------------------------------------------------------------------

_positions: dict[str, dict] = {}  # ticker -> position info


def open_position(
        ticker: str, direction: str, contracts: int,
        strike: float, expiry: str, entry_price: float,
        order_id: Optional[str] = None) -> None:
    global _daily_used
    _positions[ticker] = {
        "direction":   direction,
        "contracts":   contracts,
        "strike":      strike,
        "expiry":      expiry,
        "entry_price": entry_price,
        "peak_price":  entry_price,
        "order_id":    order_id,
        "opened_at":   time.time(),
        "expiry_date": expiry,
    }
    _daily_used += 1
    print(f"  [POS] Opened {direction} {ticker} ${strike} {expiry} "
          f"x{contracts} @ ${entry_price:.2f}  (daily: {_daily_used}/{cfg.daily_max_contracts})")


def close_position(
        ticker: str, current_price: float,
        reason: str = "", dry_run: bool = False) -> Optional[float]:
    global _daily_pnl, _daily_losses
    pos = _positions.get(ticker)
    if not pos:
        return None

    pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"]
    pnl     = (current_price - pos["entry_price"]) * 100 * pos["contracts"]
    _daily_pnl += pnl

    ok = close_position_rh(
        ticker, pos["direction"], pos["contracts"],
        pos["strike"], pos["expiry"], current_price,
        dry_run=dry_run,
    )
    if ok:
        print(f"  [POS] Closed {ticker}: {pnl_pct*100:+.1f}%  ${pnl:+.2f}  reason={reason}")
        if pnl < 0 and reason != "force_close":
            _daily_losses += 1
        elif pnl > 0:
            _daily_losses = 0
        del _positions[ticker]
    return pnl_pct if ok else None


# ---------------------------------------------------------------------------
# Position monitor (10-second loop)
# ---------------------------------------------------------------------------

async def monitor_positions(session: aiohttp.ClientSession, dry_run: bool = False) -> None:
    if not _positions:
        return
    tickers = list(_positions.keys())
    for ticker in tickers:
        pos = _positions.get(ticker)
        if not pos:
            continue

        quote = await fetch_quote(session, ticker)
        cur_price_str = quote.get("close") or quote.get("price") or quote.get("last") or 0
        try:
            cur_price = float(cur_price_str)
        except (TypeError, ValueError):
            continue
        if cur_price <= 0:
            continue

        # Update peak
        if cur_price > pos["peak_price"]:
            _positions[ticker]["peak_price"] = cur_price
        peak = _positions[ticker]["peak_price"]

        entry  = pos["entry_price"]
        pnl_pct = (cur_price - entry) / entry
        is_0dte = _is_today_expiry(pos.get("expiry_date", ""))

        # Force-close check
        et = _now_et()
        force_t = cfg.force_close_time
        ft_h, ft_m = int(force_t.split(":")[0]), int(force_t.split(":")[1])
        if et.hour > ft_h or (et.hour == ft_h and et.minute >= ft_m):
            print(f"  [FORCE] {ticker} -- past {force_t} ET, closing")
            close_position(ticker, cur_price, reason="force_close", dry_run=dry_run)
            continue

        # Stop loss
        if pnl_pct <= -cfg.stop_loss_pct:
            print(f"  [STOP] {ticker} {pnl_pct*100:.1f}% -- stop loss hit")
            close_position(ticker, cur_price, reason="stop_loss", dry_run=dry_run)
            continue

        # Tiered trailing stop
        trail_start = 0.15 if is_0dte else cfg.trail_start_pct
        if pnl_pct >= trail_start:
            peak_gain = (peak - entry) / entry
            if is_0dte:
                if   peak_gain >= 3.00: trail = 0.07
                elif peak_gain >= 1.00: trail = 0.10
                else:                   trail = 0.15
            else:
                if   peak_gain >= 3.00: trail = 0.10
                elif peak_gain >= 1.00: trail = 0.15
                else:                   trail = cfg.trail_stop_pct
            floor = peak * (1 - trail)
            if cur_price < floor:
                print(f"  [TRAIL] {ticker} {pnl_pct*100:.1f}% -- trail hit "
                      f"(peak {(peak_gain)*100:.0f}%, trail {trail*100:.0f}%)")
                close_position(ticker, cur_price, reason="trail_stop", dry_run=dry_run)
                continue

        # Indicator exit
        try:
            ohlcv = await fetch_ohlcv(session, ticker, cfg.data_interval, outputsize=60)
            if ohlcv and indicator_exit     and pnl_pct > 0:
                cl   = ohlcv["closes"]
                hi   = ohlcv["highs"]
                lo   = ohlcv["lows"]
                should_exit, reason_str = indicator_exit_check(
                    cl, hi, lo, pos["direction"])
                if should_exit:
                    print(f"  [IND-EXIT] {ticker}: {reason_str}")
                    close_position(ticker, cur_price, reason="indicator", dry_run=dry_run)
                    continue

            # Signal reversal exit
            if ohlcv and cfg.signal_exit and pnl_pct > 0:
                cl2 = ohlcv["closes"]
                hi2 = ohlcv["highs"]
                lo2 = ohlcv["lows"]
                vol2 = ohlcv["volumes"]
                vwap2 = session_vwap(ohlcv)
                chain2 = await fetch_chain_async(session, ticker, cfg.min_dte, cfg.max_dte)
                sig2   = aggregate_signals(ohlcv, chain2, hi2, lo2, cl2, vol2, vwap2)
                if (sig2["direction"] and
                        sig2["direction"] != pos["direction"] and
                        sig2["agreement"] >= cfg.signal_exit_agree):
                    print(f"  [SIG-EXIT] {ticker}: reversal {sig2['direction']} "
                          f"agree {sig2['agreement']}/{FAST_STRATEGIES}  RSI {sig2['rsi']}")
                    close_position(ticker, cur_price, reason="signal_reversal", dry_run=dry_run)
                    continue
        except Exception:
            pass

        print(f"  [POS] {ticker} {pos['direction']} ${pos['strike']} "
              f"@ ${cur_price:.2f}  {pnl_pct*100:+.1f}%  "
              f"peak {((peak-entry)/entry)*100:.0f}%")


def _is_today_expiry(expiry_str: str) -> bool:
    try:
        return date.fromisoformat(expiry_str) == date.today()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Main scan logic
# ---------------------------------------------------------------------------

async def scan_ticker(
        session: aiohttp.ClientSession, ticker: str,
        dry_run: bool = False) -> None:

    _daily_reset()

    # 1. Circuit breaker
    tripped, reason = circuit_breaker_tripped()
    if tripped:
        print(f"  [{ticker}] SKIP -- {reason}")
        return

    # Already in a position for this ticker
    if ticker in _positions:
        return

    # Already traded this ticker today
    if ticker in _cycle_traded:
        return

    # Daily contract limit
    if _daily_used >= cfg.daily_max_contracts:
        print(f"  [{ticker}] SKIP -- daily limit reached ({_daily_used}/{cfg.daily_max_contracts})")
        return

    # Fetch OHLCV and VIX concurrently
    ohlcv, vix = await asyncio.gather(
        fetch_ohlcv(session, ticker, cfg.data_interval, outputsize=100),
        fetch_vix(session),
    )

    if not ohlcv or len(ohlcv.get("closes", [])) < 10:
        print(f"  [{ticker}] SKIP -- insufficient OHLCV data")
        return

    closes  = ohlcv["closes"]
    highs   = ohlcv["highs"]
    lows    = ohlcv["lows"]
    volumes = ohlcv["volumes"]
    price   = float(closes[-1])
    _record_price(ticker, price)

    # 2. VIX level
    _record_vix(vix)
    if vix > 0 and vix > cfg.max_vix:
        print(f"  [{ticker}] SKIP -- VIX {vix:.1f} > max {cfg.max_vix}")
        return

    # 3. VIX rate-of-change spike
    spiked, spike_reason = _vix_spiked()
    if spiked:
        print(f"  [{ticker}] SKIP -- {spike_reason}")
        return

    # 4. News/ATR spike
    spike, spike_msg = is_news_spike(highs, lows, closes)
    if spike:
        print(f"  [{ticker}] SKIP -- {spike_msg}")
        return

    vwap_val = session_vwap(ohlcv)

    # 5. Market regime
    regime = market_regime(ohlcv, vwap_val, highs, lows, closes)
    if regime == "CHOP":
        print(f"  [{ticker}] SKIP -- CHOP regime, waiting for trend")
        return

    # 6. Time gate
    if not is_trading_window():
        print(f"  [{ticker}] SKIP -- outside trading window")
        return

    # 7. High-impact event blackout
    near, label = is_near_high_impact_event()
    if near:
        print(f"  [{ticker}] SKIP -- near high-impact event: {label}")
        return

    # Signal aggregation (before chain fetch to fail fast)
    chain = await fetch_chain_async(session, ticker, cfg.min_dte, cfg.max_dte)
    if not chain:
        print(f"  [{ticker}] SKIP -- no chain data")
        return

    sig = aggregate_signals(ohlcv, chain, highs, lows, closes, volumes, vwap_val)

    print(f"  [{ticker}] regime={regime}  dir={sig['direction']}  "
          f"conf={sig['confidence']}%  agree={sig['agreement']}/{FAST_STRATEGIES}  "
          f"RSI={sig['rsi']}  VIX={vix:.1f}")

    if sig["direction"] is None:
        return

    # 8. Time decay gate (after 2 PM: tighter thresholds)
    et = _now_et()
    late_session = et.hour >= 14
    min_agree_req = 4 if late_session else 3
    min_conf_req  = 72 if late_session else cfg.min_confidence

    if sig["agreement"] < min_agree_req:
        print(f"  [{ticker}] SKIP -- only {sig['agreement']} strategies agree "
              f"(need {min_agree_req}{'+ after 2PM' if late_session else ''})")
        return

    # 9. Max pain proximity
    pain_k = max_pain_level(chain)
    if pain_k > 0:
        dist_pct = abs(price - pain_k) / price
        if dist_pct < 0.002:
            print(f"  [{ticker}] SKIP -- price within 0.2% of max pain ${pain_k:.0f}")
            return

    # 10. Earnings blackout
    days_to_earn = await days_to_earnings_async(session, ticker)
    if days_to_earn is not None and days_to_earn <= 1 and not cfg.zero_dte:
        print(f"  [{ticker}] SKIP -- earnings in {days_to_earn}d")
        return

    # 11. Correlation guard
    open_tickers = list(_positions.keys())
    if not correlation_ok(ticker, open_tickers):
        print(f"  [{ticker}] SKIP -- highly correlated with open position")
        return

    # 12. Confidence filter
    if sig["confidence"] < min_conf_req:
        print(f"  [{ticker}] SKIP -- confidence {sig['confidence']}% < min {cfg.min_confidence}%")
        return

    # 13. Fetch / validate chain
    if not chain.get("options"):
        print(f"  [{ticker}] SKIP -- empty chain")
        return

    # 14. Select contract
    contract = select_contract(chain, sig["direction"])
    if not contract:
        print(f"  [{ticker}] SKIP -- no suitable contract found")
        return

    # 15. Spread check
    if not spread_ok(contract):
        print(f"  [{ticker}] SKIP -- bid-ask spread too wide")
        return

    # 16. IV rank gate
    iv_mod = iv_rank_modifier(chain)
    if iv_mod < 0.8:
        print(f"  [{ticker}] SKIP -- IV rank too low (mod={iv_mod:.2f})")
        return

    n_contracts = size_contracts(sig["confidence"])
    remaining   = cfg.daily_max_contracts - _daily_used
    n_contracts = min(n_contracts, remaining)

    print(f"  --> {sig['direction']} {ticker} ${contract['strike']} "
          f"{contract['expiry']}  conf={sig['confidence']}%  "
          f"agree={sig['agreement']}/{FAST_STRATEGIES}  x{n_contracts}")

    order_id = place_order_rh(
        ticker, sig["direction"], n_contracts,
        contract["strike"], contract["expiry"], contract["limit_price"],
        dry_run=dry_run,
    )
    if order_id:
        open_position(
            ticker, sig["direction"], n_contracts,
            contract["strike"], contract["expiry"], contract["limit_price"],
            order_id=order_id,
        )
        _cycle_traded.add(ticker)
        notify(
            f"ENTRY: {sig['direction']} {ticker}",
            f"Strike: ${contract['strike']}\nExpiry: {contract.get('expiry','?')}\n"
            f"Price: ${contract['limit_price']:.2f}\nConf: {sig['confidence']}%\n"
            f"Agree: {sig['agreement']}/{FAST_STRATEGIES}\nRegime: {regime}",
        )


# ---------------------------------------------------------------------------
# Async main loop
# ---------------------------------------------------------------------------

async def async_main(tickers: list[str], dry_run: bool, loop: bool) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            _daily_reset()
            vix = await fetch_vix(session)
            _record_vix(vix)
            et  = _now_et()
            print(f"\n{'='*60}")
            print(f"  {et.strftime('%H:%M:%S ET')}  VIX: {vix:.1f}  "
                  f"losses today: {_daily_losses}  daily P&L: ${_daily_pnl:+.2f}")
            print(f"{'='*60}")

            if _positions:
                await monitor_positions(session, dry_run=dry_run)

            # Scan all tickers concurrently
            await asyncio.gather(*[
                scan_ticker(session, t, dry_run=dry_run) for t in tickers
            ])

            if not loop:
                break

            # Sleep scan interval (shorter if in a position)
            sleep_sec = cfg.pos_check_sec if _positions else cfg.scan_sec
            print(f"  Sleeping {sleep_sec}s ...")
            await asyncio.sleep(sleep_sec)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="OptionEdge Fast Trader")
    parser.add_argument("--dry-run", action="store_true", help="No real orders")
    parser.add_argument("--loop",    action="store_true", help="Run continuously")
    parser.add_argument("--ticker",  nargs="+",           help="Override WATCH_TICKERS")
    args = parser.parse_args()

    tickers = [t.upper() for t in args.ticker] if args.ticker else cfg.watch_tickers
    if not tickers:
        print("ERROR: Set WATCH_TICKERS=SPX or use --ticker SPX")
        sys.exit(1)

    mode = "0DTE" if cfg.zero_dte else f"{cfg.min_dte}-{cfg.max_dte}DTE"
    print(f"OptionEdge Fast Trader -- {mode}")
    print(f"Tickers: {tickers}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print(f"Max contract price: ${cfg.max_contract_price:.2f}")
    print(f"Daily limit: {cfg.daily_max_contracts} contract(s)")
    print(f"Stop loss: {cfg.stop_loss_pct*100:.0f}%  "
          f"Trail start: {cfg.trail_start_pct*100:.0f}%  "
          f"VIX max: {cfg.max_vix}")
    print(f"Exits: stop {cfg.stop_loss_pct*100:.0f}%  trail start {cfg.trail_start_pct*100:.0f}%  "
          f"trail 15/10/7%  force-close {cfg.force_close_time} ET")

    asyncio.run(async_main(tickers, args.dry_run, args.loop))


if __name__ == "__main__":
    main()
