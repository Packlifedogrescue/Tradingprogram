#!/usr/bin/env python3
"""
OptionEdge Auto-Trader
======================
Reads market data, runs the 3-strategy signal engine, and places options orders
automatically via Robinhood when a signal fires.

Usage:
  python scripts/auto_trader.py                # run once immediately
  python scripts/auto_trader.py --loop         # run every N minutes during market hours
  python scripts/auto_trader.py --ticker SPY   # override tickers for one run
  python scripts/auto_trader.py --dry-run      # signal only, no orders placed

Required env vars (set in your shell or a .env file):
  TWELVE_DATA_API_KEY
  TASTYTRADE_USERNAME
  TASTYTRADE_PASSWORD
  TASTYTRADE_ENV           (sandbox | production)
  ROBINHOOD_USERNAME
  ROBINHOOD_PASSWORD
  WATCH_TICKERS            comma-separated, e.g. SPY,QQQ,AAPL
  TRADE_MIN_CONFIDENCE     skip trades below this % (default 60)
  TRADE_TIER2_THRESHOLD    confidence threshold for 2 contracts (default 70)
  TRADE_TIER3_THRESHOLD    confidence threshold for 3 contracts (default 85)
  TRADE_TIER1_CONTRACTS    contracts at tier 1 (default 1)
  TRADE_TIER2_CONTRACTS    contracts at tier 2 (default 2)
  TRADE_TIER3_CONTRACTS    contracts at tier 3 (default 3)
  TRADE_DAILY_MAX_CONTRACTS hard cap per day (default 6)
  TRADE_MIN_DTE            min days to expiry (default 21)
  TRADE_MAX_DTE            max days to expiry (default 45)
  TRADE_LOOP_INTERVAL_MIN  minutes between scans in --loop mode (default 5)

Optional:
  NEXT_PUBLIC_SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY  (to log signals + trades)
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return default

class Config:
    twelve_key:           str   = os.environ.get("TWELVE_DATA_API_KEY", "")
    tastytrade_user:      str   = os.environ.get("TASTYTRADE_USERNAME", "")
    tastytrade_pass:      str   = os.environ.get("TASTYTRADE_PASSWORD", "")
    tastytrade_base:      str   = ("https://api.tastytrade.com" if os.environ.get("TASTYTRADE_ENV") == "production"
                                   else "https://api.cert.tastytrade.com")
    rh_user:              str   = os.environ.get("ROBINHOOD_USERNAME", "")
    rh_pass:              str   = os.environ.get("ROBINHOOD_PASSWORD", "")
    watch_tickers:        list  = [t.strip().upper() for t in os.environ.get("WATCH_TICKERS", "").split(",") if t.strip()]

    # Contract price ceiling — bot won't buy contracts more expensive than this
    # Set to 0 to disable. With $400 account, try 3.50 (= $350/contract max)
    max_contract_price:   float = _env_float("TRADE_MAX_CONTRACT_PRICE", 0)

    # Notifications
    resend_key:           str   = os.environ.get("RESEND_API_KEY", "")
    notify_email:         str   = os.environ.get("NOTIFY_EMAIL", "")
    notify_phone:         str   = os.environ.get("NOTIFY_PHONE", "")  # 10 digits, no dashes

    min_confidence:       int   = _env_int("TRADE_MIN_CONFIDENCE", 60)
    tier2_threshold:      int   = _env_int("TRADE_TIER2_THRESHOLD", 70)
    tier3_threshold:      int   = _env_int("TRADE_TIER3_THRESHOLD", 85)
    tier1_contracts:      int   = _env_int("TRADE_TIER1_CONTRACTS", 1)
    tier2_contracts:      int   = _env_int("TRADE_TIER2_CONTRACTS", 2)
    tier3_contracts:      int   = _env_int("TRADE_TIER3_CONTRACTS", 3)
    daily_max_contracts:  int   = _env_int("TRADE_DAILY_MAX_CONTRACTS", 6)
    min_dte:              int   = _env_int("TRADE_MIN_DTE", 21)
    max_dte:              int   = _env_int("TRADE_MAX_DTE", 45)
    loop_interval_min:    int   = _env_int("TRADE_LOOP_INTERVAL_MIN", 5)
    pos_check_min:        int   = _env_int("TRADE_POS_CHECK_MIN", 1)

    # Exit rules
    stop_loss_pct:     float = _env_float("TRADE_STOP_LOSS_PCT",    0.20)  # exit if option down 20%
    take_profit_pct:   float = _env_float("TRADE_TAKE_PROFIT_PCT",  5.00)  # safety net only (500%)
    trail_stop_pct:    float = _env_float("TRADE_TRAIL_STOP_PCT",   0.20)  # base trail 20% off peak
    trail_start_pct:   float = _env_float("TRADE_TRAIL_START_PCT",  0.30)  # activate trail after 30% gain
    signal_exit:       bool  = os.environ.get("TRADE_SIGNAL_EXIT", "true").lower() == "true"
    signal_exit_agree: int   = _env_int("TRADE_SIGNAL_EXIT_AGREE", 2)  # min strategies for reversal exit
    indicator_exit:    bool  = os.environ.get("TRADE_INDICATOR_EXIT", "true").lower() == "true"

    sb_url:   str = os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")
    sb_key:   str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


cfg = Config()

# Verizon SMS gateway (email-to-text, completely free)
_SMS_GATEWAY = "vtext.com"


# ---------------------------------------------------------------------------
# Notifications (email via Resend + free SMS via carrier gateway)
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
# Technical indicator helpers
# ---------------------------------------------------------------------------

def ema(prices: np.ndarray, period: int) -> np.ndarray:
    result = np.zeros(len(prices))
    result[0] = prices[0]
    k = 2.0 / (period + 1)
    for i in range(1, len(prices)):
        result[i] = prices[i] * k + result[i - 1] * (1 - k)
    return result


def rsi(prices: np.ndarray, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices[-(period + 1):])
    ups = deltas[deltas > 0]
    downs = -deltas[deltas < 0]
    avg_up = ups.mean() if len(ups) > 0 else 0.0
    avg_dn = downs.mean() if len(downs) > 0 else 0.0
    if avg_dn == 0:
        return 100.0
    rs = avg_up / avg_dn
    return 100.0 - 100.0 / (1 + rs)


def bollinger(prices: np.ndarray, period: int = 20, dev: float = 2.0):
    if len(prices) < period:
        return None, None, None
    w = prices[-period:]
    mid = w.mean()
    std = w.std(ddof=1)
    return mid - dev * std, mid, mid + dev * std


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < 2:
        return 0.0
    trs = [max(highs[i] - lows[i],
               abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1]))
           for i in range(1, len(closes))]
    return float(np.array(trs[-period:]).mean()) if trs else 0.0


def hv(closes: np.ndarray, window: int) -> float:
    if len(closes) < window + 1:
        return 0.0
    sl = closes[-(window + 1):]
    lr = np.log(sl[1:] / sl[:-1])
    return float(lr.std(ddof=1) * math.sqrt(252))


def rsi_series(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Vectorized RSI for every bar — used by divergence detection."""
    result = np.full(len(prices), 50.0)
    if len(prices) < period + 2:
        return result
    deltas = np.diff(prices)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = gains[:period].mean()
    avg_l  = losses[:period].mean()
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        result[i + 1] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    return result


# ---------------------------------------------------------------------------
# Signal strategies
# ---------------------------------------------------------------------------

def trend_momentum(closes: np.ndarray, rsi_val: float) -> tuple[Optional[str], int]:
    if len(closes) < 51:
        return None, 0
    e9  = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, 50)[-1]
    cur = closes[-1]
    if cur > e9 > e21 > e50 and rsi_val > 55:
        return "CALL", min(100, int((rsi_val - 55) * 4))
    if cur < e9 < e21 < e50 and rsi_val < 45:
        return "PUT", min(100, int((45 - rsi_val) * 4))
    return None, 0


def mean_reversion(closes: np.ndarray, rsi_val: float) -> tuple[Optional[str], int]:
    lower, _mid, upper = bollinger(closes)
    if lower is None or _mid is None or upper is None:
        return None, 0
    bw = (upper - lower) / _mid
    cur = closes[-1]
    if cur <= lower and rsi_val < 30 and bw > 0.04:
        return "CALL", min(100, int((30 - rsi_val) * 5))
    if cur >= upper and rsi_val > 70 and bw > 0.04:
        return "PUT", min(100, int((rsi_val - 70) * 5))
    return None, 0


def breakout(
    closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
    volumes: np.ndarray, rsi_val: float
) -> tuple[Optional[str], int]:
    if len(closes) < 20:
        return None, 0
    cur     = closes[-1]
    res20   = highs[-20:].max()
    sup20   = lows[-20:].min()
    vol_avg = volumes[-20:].mean()
    vol_surge = volumes[-1] > vol_avg * 1.5

    atr_short = atr(highs[-12:], lows[-12:], closes[-12:], 5)
    atr_long  = atr(highs, lows, closes, 14)
    compressed = atr_long > 0 and atr_short < atr_long * 0.7

    if cur >= res20 * 0.99 and vol_surge and rsi_val > 55:
        conf = 60 + (25 if compressed else 0) + (15 if rsi_val > 65 else 0)
        return "CALL", min(100, conf)
    if cur <= sup20 * 1.01 and vol_surge and rsi_val < 45:
        conf = 60 + (25 if compressed else 0) + (15 if rsi_val < 35 else 0)
        return "PUT", min(100, conf)
    return None, 0


def bull_bear_flag(
    closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, volumes: np.ndarray
) -> tuple[Optional[str], int]:
    """
    Bull flag: strong up-pole -> brief consolidation (lower highs, lower volume) -> breakout above flag high.
    Bear flag: strong down-pole -> brief consolidation (higher lows, lower volume) -> breakdown below flag low.
    Uses a 10-candle pole window and a 5-candle flag window on daily data.
    """
    POLE_LEN = 10
    FLAG_LEN = 5
    MIN_POLE_PCT = 0.025  # pole must move at least 2.5%

    if len(closes) < POLE_LEN + FLAG_LEN + 1:
        return None, 0

    pole_start = closes[-(POLE_LEN + FLAG_LEN)]
    pole_end   = closes[-FLAG_LEN]
    pole_move  = (pole_end - pole_start) / pole_start

    flag_closes  = closes[-FLAG_LEN:]
    flag_highs   = highs[-FLAG_LEN:]
    flag_lows    = lows[-FLAG_LEN:]
    flag_vols    = volumes[-FLAG_LEN:]
    pole_vols    = volumes[-(POLE_LEN + FLAG_LEN):-FLAG_LEN]

    avg_pole_vol = pole_vols.mean() if len(pole_vols) > 0 else 1.0
    avg_flag_vol = flag_vols.mean()
    vol_declining = avg_flag_vol < avg_pole_vol * 0.80

    # Flag range should be narrow relative to the pole (coiling)
    flag_range_pct = (flag_highs.max() - flag_lows.min()) / max(abs(pole_end), 1)
    tight_flag = flag_range_pct < abs(pole_move) * 0.55

    cur = closes[-1]

    if pole_move >= MIN_POLE_PCT:
        # Flag drift: slight pullback or flat (bearish tilt against the bullish pole)
        flag_slope = (flag_closes[-1] - flag_closes[0]) / max(flag_closes[0], 1)
        consolidating = -0.025 <= flag_slope <= 0.005

        # Breakout: current close above the highest close in the flag body (excl. today)
        flag_body_high = flag_highs[:-1].max() if len(flag_highs) > 1 else flag_highs[0]
        breaking_out = cur >= flag_body_high * 0.998

        if consolidating and breaking_out and (vol_declining or tight_flag):
            conf = 60
            if vol_declining: conf += 15
            if tight_flag:    conf += 10
            if pole_move > 0.05: conf += 10
            return "CALL", min(100, conf)

    if pole_move <= -MIN_POLE_PCT:
        flag_slope = (flag_closes[-1] - flag_closes[0]) / max(flag_closes[0], 1)
        consolidating = -0.005 <= flag_slope <= 0.025

        flag_body_low = flag_lows[:-1].min() if len(flag_lows) > 1 else flag_lows[0]
        breaking_down = cur <= flag_body_low * 1.002

        if consolidating and breaking_down and (vol_declining or tight_flag):
            conf = 60
            if vol_declining:   conf += 15
            if tight_flag:      conf += 10
            if pole_move < -0.05: conf += 10
            return "PUT", min(100, conf)

    return None, 0


def fvg_signal(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray
) -> tuple[Optional[str], int]:
    """
    Fair Value Gap (3-candle imbalance).
    Bullish FVG:  highs[i-2] < lows[i]  -- gap zone is [highs[i-2], lows[i]]
    Bearish FVG:  lows[i-2]  > highs[i] -- gap zone is [highs[i],   lows[i-2]]

    Signal fires when the current candle retests the most recent unfilled FVG zone.
    The gap is considered "filled" if a close has traded through it since it formed.
    """
    if len(closes) < 5:
        return None, 0

    cur = closes[-1]
    n   = len(closes)
    LOOKBACK = min(30, n - 1)

    # Walk back through recent candles looking for the freshest unfilled FVG
    best_bull: Optional[tuple[float, float, int]] = None
    best_bear: Optional[tuple[float, float, int]] = None

    for i in range(n - 1, n - LOOKBACK, -1):
        if i < 2:
            break

        # Bullish FVG at candle i
        gap_lo = highs[i - 2]
        gap_hi = lows[i]
        if gap_lo < gap_hi:
            # Check it hasn't been fully closed by a subsequent low
            filled = any(lows[j] < gap_lo for j in range(i + 1, n))
            if not filled and best_bull is None:
                best_bull = (gap_lo, gap_hi, i)

        # Bearish FVG at candle i
        gap_hi2 = lows[i - 2]
        gap_lo2 = highs[i]
        if gap_lo2 < gap_hi2:
            filled = any(highs[j] > gap_hi2 for j in range(i + 1, n))
            if not filled and best_bear is None:
                best_bear = (gap_lo2, gap_hi2, i)

    # Bullish FVG retest: price pulls back into the gap (potential support)
    if best_bull:
        g_lo, g_hi, g_idx = best_bull
        if g_lo * 0.999 <= cur <= g_hi * 1.001:
            freshness = max(0, 20 - (n - 1 - g_idx))
            conf = 55 + int(freshness * 1.75)
            return "CALL", min(88, conf)

    # Bearish FVG retest: price rallies into the gap (potential resistance)
    if best_bear:
        g_lo, g_hi, g_idx = best_bear
        if g_lo * 0.999 <= cur <= g_hi * 1.001:
            freshness = max(0, 20 - (n - 1 - g_idx))
            conf = 55 + int(freshness * 1.75)
            return "PUT", min(88, conf)

    return None, 0


def pivot_signal(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, rsi_val: float
) -> tuple[Optional[str], int]:
    """
    Standard pivot points from the prior session.
    PP = (H + L + C) / 3
    R1 = 2*PP - L,  R2 = PP + (H - L)
    S1 = 2*PP - H,  S2 = PP - (H - L)

    Signals:
    - Bounce off S1/S2 with RSI < 50  -> CALL
    - Rejection at R1/R2 with RSI > 50 -> PUT
    - Momentum break above R1 / below S1 -> CALL / PUT
    """
    if len(closes) < 3:
        return None, 0

    H, L, C = float(highs[-2]), float(lows[-2]), float(closes[-2])
    prev_c   = float(closes[-3])

    PP = (H + L + C) / 3
    R1 = 2 * PP - L
    R2 = PP + (H - L)
    S1 = 2 * PP - H
    S2 = PP - (H - L)

    cur = float(closes[-1])
    TOL = 0.005  # within 0.5% of a level

    def near(price: float, level: float) -> bool:
        return abs(price - level) / max(abs(level), 1e-9) <= TOL

    def between(price: float, lo: float, hi: float) -> bool:
        return lo * (1 - TOL) <= price <= hi * (1 + TOL)

    # Bullish: bounce off S1 / S2 support zone
    if between(cur, S2, S1) and rsi_val < 50:
        conf = 72 if near(cur, S2) else 62
        return "CALL", conf

    # Bullish momentum break: closes above R1 when prior close was below R1
    if cur > R1 and prev_c <= R1 and rsi_val > 50:
        return "CALL", 68

    # Bearish: rejection at R1 / R2 resistance zone
    if between(cur, R1, R2) and rsi_val > 50:
        conf = 72 if near(cur, R2) else 62
        return "PUT", conf

    # Bearish momentum break: closes below S1 when prior close was above S1
    if cur < S1 and prev_c >= S1 and rsi_val < 50:
        return "PUT", 68

    return None, 0


def vwap_signal(
    closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
    volumes: np.ndarray, rsi_val: float
) -> tuple[Optional[str], int]:
    """
    20-day rolling VWAP using typical price x volume.
    Signals on reclaim (close crosses above VWAP) and rejection (close crosses below).
    Also signals when price holds VWAP as dynamic support/resistance.
    """
    if len(closes) < 21:
        return None, 0
    typical = (highs[-20:] + lows[-20:] + closes[-20:]) / 3
    vwap    = float((typical * volumes[-20:]).sum() / volumes[-20:].sum())
    cur, prev = float(closes[-1]), float(closes[-2])
    TOL = 0.002  # 0.2%

    # Reclaim: crossed above VWAP from below
    if prev < vwap and cur >= vwap * (1 - TOL) and rsi_val > 48:
        return "CALL", 66

    # Rejection: crossed below VWAP from above
    if prev > vwap and cur <= vwap * (1 + TOL) and rsi_val < 52:
        return "PUT", 66

    # Holding VWAP as support (price dipping to VWAP and holding)
    if vwap * 0.998 <= cur <= vwap * 1.003 and prev >= vwap and rsi_val > 45:
        return "CALL", 58

    # VWAP as resistance (price rallying to VWAP and stalling)
    if vwap * 0.997 <= cur <= vwap * 1.002 and prev <= vwap and rsi_val < 55:
        return "PUT", 58

    return None, 0


def gap_signal(
    opens: np.ndarray, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray
) -> tuple[Optional[str], int]:
    """
    Price gaps -- when today's open left a gap vs prior close that hasn't been filled.
    Unfilled gap above current price = bullish (acts as support below price moved up).
    Unfilled gap below current price = bearish (acts as resistance above price moved down).
    Also signals when current candle is actively filling a gap (mean-reversion momentum).
    """
    n = len(closes)
    if n < 5 or len(opens) < n:
        return None, 0

    MIN_GAP = 0.005  # 0.5% minimum gap size
    LOOKBACK = min(25, n - 1)
    cur = float(closes[-1])

    for i in range(n - 2, n - LOOKBACK - 1, -1):
        if i < 1:
            break
        gap_pct = (float(opens[i]) - float(closes[i - 1])) / float(closes[i - 1])

        if gap_pct >= MIN_GAP:
            # Upward gap: zone = [closes[i-1], opens[i]]
            g_lo, g_hi = float(closes[i - 1]), float(opens[i])
            # Unfilled if no subsequent candle low reached back into the zone
            unfilled = all(float(lows[j]) >= g_lo * 0.999 for j in range(i, n))
            if unfilled:
                age = n - 1 - i
                conf = max(55, 72 - age * 2)
                if cur >= g_lo * 0.997:
                    return "CALL", conf  # gap below acting as support
                break

        elif gap_pct <= -MIN_GAP:
            # Downward gap: zone = [opens[i], closes[i-1]]
            g_lo, g_hi = float(opens[i]), float(closes[i - 1])
            unfilled = all(float(highs[j]) <= g_hi * 1.001 for j in range(i, n))
            if unfilled:
                age = n - 1 - i
                conf = max(55, 72 - age * 2)
                if cur <= g_hi * 1.003:
                    return "PUT", conf  # gap above acting as resistance
                break

    return None, 0


def macd_histogram(closes: np.ndarray) -> np.ndarray:
    """MACD histogram (12 EMA - 26 EMA - 9-signal). Positive = bullish momentum."""
    if len(closes) < 35:
        return np.zeros(len(closes))
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    macd_line = fast - slow
    signal_line = ema(macd_line, 9)
    return macd_line - signal_line


def macd_entry_signal(closes: np.ndarray) -> tuple[Optional[str], int]:
    """
    MACD histogram as an entry signal.
    Zero-line cross = strongest (momentum just shifted direction).
    Three consecutive bars trending = momentum building.
    """
    hist = macd_histogram(closes)
    if len(hist) < 3:
        return None, 0
    h0, h1, h2 = float(hist[-1]), float(hist[-2]), float(hist[-3])
    if h0 > 0 and h1 <= 0:                # bullish zero-cross
        return "CALL", min(72, 60 + int(abs(h0) * 400))
    if h0 < 0 and h1 >= 0:                # bearish zero-cross
        return "PUT",  min(72, 60 + int(abs(h0) * 400))
    if h0 > h1 > h2 > 0:                  # momentum building bullish
        return "CALL", min(66, 54 + int(abs(h0) * 300))
    if h0 < h1 < h2 < 0:                  # momentum building bearish
        return "PUT",  min(66, 54 + int(abs(h0) * 300))
    return None, 0


def indicator_exit_check(
    direction: str,
    closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, opens: np.ndarray,
    pnl_pct: float,
    min_profit_pct: float = 0.15,
) -> Optional[str]:
    """
    Read the chart and return an exit reason if momentum has turned against the position.
    Only fires when pnl_pct >= min_profit_pct so we don't exit on noise right after entry.
    Checks: RSI exhaustion, MACD cross, Bollinger band touch, reversal candle.
    """
    rsi_val = rsi(closes)

    # RSI overbought/oversold -- momentum exhaustion
    if direction == "CALL" and rsi_val >= 75 and pnl_pct >= min_profit_pct:
        return "rsi_overbought"
    if direction == "PUT" and rsi_val <= 25 and pnl_pct >= min_profit_pct:
        return "rsi_oversold"

    # MACD histogram flip -- momentum crossed against position
    hist = macd_histogram(closes)
    if len(hist) >= 2:
        if direction == "CALL" and hist[-1] < 0 < hist[-2] and pnl_pct >= min_profit_pct:
            return "macd_bearish_cross"
        if direction == "PUT" and hist[-1] > 0 > hist[-2] and pnl_pct >= min_profit_pct:
            return "macd_bullish_cross"

    # Bollinger band extension -- price stretched beyond 2-sigma band, likely to revert
    lo, _mid, hi = bollinger(closes)
    if hi is not None and lo is not None:
        cur = float(closes[-1])
        if direction == "CALL" and cur >= hi * 0.998 and pnl_pct >= min_profit_pct:
            return "bb_upper_touch"
        if direction == "PUT" and cur <= lo * 1.002 and pnl_pct >= min_profit_pct:
            return "bb_lower_touch"

    # Strong reversal candle against the position
    if len(opens) >= len(closes):
        pattern_dir, pattern_conf = candlestick_signal(opens, closes, highs, lows, rsi_val)
        if (pattern_dir and pattern_dir != direction
                and pattern_conf >= 72 and pnl_pct >= min_profit_pct):
            return "candle_reversal"

    return None


def rsi_divergence(closes: np.ndarray) -> tuple[Optional[str], int]:
    """
    Classic RSI divergence using swing highs/lows over a 20-bar lookback.
    Bearish: price makes higher high, RSI makes lower high -> exhaustion.
    Bullish: price makes lower low, RSI makes higher low -> hidden strength.
    """
    if len(closes) < 35:
        return None, 0

    rsi_vals = rsi_series(closes)
    LOOKBACK = 20
    WING = 3  # bars on each side to define a swing point

    c = closes[-LOOKBACK:]
    r = rsi_vals[-LOOKBACK:]

    def peaks(arr: np.ndarray) -> list[int]:
        return [i for i in range(WING, len(arr) - WING)
                if arr[i] == arr[max(0, i - WING):i + WING + 1].max()]

    def troughs(arr: np.ndarray) -> list[int]:
        return [i for i in range(WING, len(arr) - WING)
                if arr[i] == arr[max(0, i - WING):i + WING + 1].min()]

    p = peaks(c)
    t = troughs(c)

    # Bearish divergence: two price peaks, second > first, RSI second < first
    if len(p) >= 2:
        p1, p2 = p[-2], p[-1]
        if c[p2] > c[p1] * 1.001 and r[p2] < r[p1] - 4:
            div_strength = r[p1] - r[p2]
            return "PUT", min(82, 54 + int(div_strength * 1.1))

    # Bullish divergence: two price troughs, second < first, RSI second > first
    if len(t) >= 2:
        t1, t2 = t[-2], t[-1]
        if c[t2] < c[t1] * 0.999 and r[t2] > r[t1] + 4:
            div_strength = r[t2] - r[t1]
            return "CALL", min(82, 54 + int(div_strength * 1.1))

    return None, 0


def candlestick_signal(
    opens: np.ndarray, closes: np.ndarray, highs: np.ndarray,
    lows: np.ndarray, rsi_val: float
) -> tuple[Optional[str], int]:
    """
    Four single/two-candle patterns:
    - Bullish engulfing: prior red candle fully wrapped by current green candle
    - Bearish engulfing: prior green candle fully wrapped by current red candle
    - Hammer: long lower wick (>=2x body), small upper wick -- bullish reversal
    - Shooting star: long upper wick (>=2x body), small lower wick -- bearish reversal
    """
    if len(closes) < 3 or len(opens) < len(closes):
        return None, 0

    co, cc = float(opens[-1]), float(closes[-1])
    po, pc = float(opens[-2]), float(closes[-2])
    ch, cl = float(highs[-1]), float(lows[-1])

    body      = abs(cc - co)
    candle_rng = ch - cl
    if candle_rng < 1e-9:
        return None, 0

    upper_wick = ch - max(co, cc)
    lower_wick = min(co, cc) - cl

    # Bullish engulfing
    if pc < po and cc > co and co <= pc and cc >= po and rsi_val < 62:
        conf = 72 + (10 if rsi_val < 40 else 0)
        return "CALL", min(90, conf)

    # Bearish engulfing
    if pc > po and cc < co and co >= pc and cc <= po and rsi_val > 38:
        conf = 72 + (10 if rsi_val > 60 else 0)
        return "PUT", min(90, conf)

    if body > 0:
        # Hammer: lower wick >= 2x body, upper wick <= 30% of body
        if lower_wick >= 2.0 * body and upper_wick <= 0.3 * body and rsi_val < 52:
            conf = 65 + (12 if rsi_val < 35 else 0)
            return "CALL", min(85, conf)

        # Shooting star: upper wick >= 2x body, lower wick <= 30% of body
        if upper_wick >= 2.0 * body and lower_wick <= 0.3 * body and rsi_val > 48:
            conf = 65 + (12 if rsi_val > 65 else 0)
            return "PUT", min(85, conf)

    return None, 0


def options_flow_signal(chain: Optional[dict]) -> tuple[Optional[str], int]:
    """
    Unusual options activity -- call/put volume ratio and volume-to-OI ratio.
    High call volume with low PCR -> smart money positioning bullish.
    High put volume with high PCR -> smart money positioning bearish.
    """
    if not chain:
        return None, 0
    options = chain.get("options", [])
    if not options:
        return None, 0

    calls = [o for o in options if o.get("optionType") == "call"]
    puts  = [o for o in options if o.get("optionType") == "put"]

    call_vol = sum(o.get("volume", 0) or 0 for o in calls)
    put_vol  = sum(o.get("volume", 0) or 0 for o in puts)
    call_oi  = sum(o.get("openInterest", 0) or 0 for o in calls)
    put_oi   = sum(o.get("openInterest", 0) or 0 for o in puts)

    if call_vol + put_vol < 50:  # not enough activity to be meaningful
        return None, 0

    pcr     = put_vol / max(call_vol, 1)
    call_vor = call_vol / max(call_oi, 1)  # volume/OI -- unusually high = informed flow
    put_vor  = put_vol  / max(put_oi, 1)

    # Bullish: PCR < 0.6 with meaningful call activity
    if pcr < 0.60 and call_vor > 0.10:
        conf = min(80, 58 + int((0.60 - pcr) * 60) + int(min(call_vor * 120, 15)))
        return "CALL", conf

    # Bearish: PCR > 1.5 with meaningful put activity
    if pcr > 1.50 and put_vor > 0.10:
        conf = min(80, 58 + int((pcr - 1.50) * 35) + int(min(put_vor * 120, 15)))
        return "PUT", conf

    return None, 0


def iv_rank_modifier(chain: Optional[dict], hv30_val: float) -> float:
    """
    Returns a confidence multiplier based on how cheap/expensive implied vol is.
    For single-leg long options: cheap IV is a tailwind, expensive IV is a headwind.
      IV/HV < 0.80  -> +15% confidence (cheap to buy)
      IV/HV 0.80-1.20 -> no change
      IV/HV > 1.40  -> -15% confidence (expensive to buy)
    """
    if not chain or hv30_val <= 0:
        return 1.0
    atm = atm_iv_from_chain(chain) * 100
    if atm <= 0:
        return 1.0
    ratio = atm / hv30_val
    if ratio < 0.80:
        return 1.15
    if ratio > 1.40:
        return 0.85
    return 1.0


TOTAL_STRATEGIES = 12  # trend, mean_reversion, breakout, flag, fvg, pivot,
                       # vwap, gap, rsi_divergence, candlestick, options_flow, macd


def run_signal_engine(ohlcv: dict, chain: Optional[dict] = None) -> dict:
    closes  = np.array(ohlcv["closes"],  dtype=float)
    highs   = np.array(ohlcv["highs"],   dtype=float)
    lows    = np.array(ohlcv["lows"],    dtype=float)
    volumes = np.array(ohlcv["volumes"], dtype=float)
    opens   = np.array(ohlcv.get("opens", []), dtype=float)
    rsi_val = rsi(closes)
    hv30_val = hv(closes, 30) * 100

    raw_signals = []
    for name, fn in [
        ("trend",          lambda: trend_momentum(closes, rsi_val)),
        ("mean_reversion", lambda: mean_reversion(closes, rsi_val)),
        ("breakout",       lambda: breakout(closes, highs, lows, volumes, rsi_val)),
        ("flag",           lambda: bull_bear_flag(closes, highs, lows, volumes)),
        ("fvg",            lambda: fvg_signal(highs, lows, closes)),
        ("pivot",          lambda: pivot_signal(highs, lows, closes, rsi_val)),
        ("vwap",           lambda: vwap_signal(closes, highs, lows, volumes, rsi_val)),
        ("gap",            lambda: gap_signal(opens, closes, highs, lows)),
        ("divergence",     lambda: rsi_divergence(closes)),
        ("candle",         lambda: candlestick_signal(opens, closes, highs, lows, rsi_val)),
        ("flow",           lambda: options_flow_signal(chain)),
        ("macd",           lambda: macd_entry_signal(closes)),
    ]:
        direction, conf = fn()
        if direction:
            raw_signals.append({"direction": direction, "confidence": conf, "strategy": name})

    if not raw_signals:
        return {"direction": None, "confidence": 0, "agreement": 0, "signals": []}

    calls = [s for s in raw_signals if s["direction"] == "CALL"]
    puts  = [s for s in raw_signals if s["direction"] == "PUT"]
    dominant  = calls if len(calls) >= len(puts) else puts
    direction = dominant[0]["direction"]
    agreement = len(dominant)
    avg_conf  = sum(s["confidence"] for s in dominant) / len(dominant)

    # Agreement multiplier scales 0.715 -> 1.35 across 1->11 strategies
    agree_mult = 0.65 + (agreement / TOTAL_STRATEGIES) * 0.70
    # IV rank modifier: cheap IV boosts, expensive IV penalises
    iv_mult = iv_rank_modifier(chain, hv30_val)
    final_conf = int(avg_conf * agree_mult * iv_mult)

    return {
        "direction":  direction,
        "confidence": min(100, final_conf),
        "agreement":  agreement,
        "signals":    raw_signals,
        "rsi":        round(rsi_val, 1),
        "hv30":       round(hv30_val, 2),
        "hv60":       round(hv(closes, 60) * 100, 2),
        "iv_mult":    round(iv_mult, 2),
    }


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def calc_contracts(confidence: int) -> int:
    if confidence < cfg.min_confidence:
        return 0
    if confidence >= cfg.tier3_threshold:
        return cfg.tier3_contracts
    if confidence >= cfg.tier2_threshold:
        return cfg.tier2_contracts
    return cfg.tier1_contracts


# ---------------------------------------------------------------------------
# Tastytrade session auth
# ---------------------------------------------------------------------------

_tt_session_token: Optional[str] = None
_tt_session_expiry: float = 0.0
_TT_SESSION_TTL = 23 * 3600  # tokens last ~24 h; refresh after 23 h


def _tt_get_token() -> Optional[str]:
    """Return a cached Tastytrade session token, logging in if needed."""
    global _tt_session_token, _tt_session_expiry
    if _tt_session_token and time.time() < _tt_session_expiry:
        return _tt_session_token
    if not cfg.tastytrade_user or not cfg.tastytrade_pass:
        print("  [TT] TASTYTRADE_USERNAME / TASTYTRADE_PASSWORD not set")
        return None
    try:
        r = requests.post(
            f"{cfg.tastytrade_base}/sessions",
            json={"login": cfg.tastytrade_user, "password": cfg.tastytrade_pass, "remember-me": True},
            headers={"Content-Type": "application/json", "User-Agent": "personal-option-bot/1.0"},
            timeout=10,
        )
        token = r.json().get("data", {}).get("session-token")
        if not token:
            print(f"  [TT] Auth failed: {r.text[:200]}")
            return None
        _tt_session_token = token
        _tt_session_expiry = time.time() + _TT_SESSION_TTL
        return token
    except Exception as e:
        print(f"  [TT] Auth error: {e}")
        return None


def _tt_headers() -> dict:
    token = _tt_get_token()
    return {"Authorization": token, "Accept": "application/json", "User-Agent": "personal-option-bot/1.0"} if token else {}


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def fetch_ohlcv(ticker: str) -> Optional[dict]:
    url = (f"https://api.twelvedata.com/time_series"
           f"?symbol={ticker}&interval=1day&outputsize=90"
           f"&apikey={cfg.twelve_key}")
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            print(f"  [PRICE] Twelve Data error for {ticker}: {data.get('message')}")
            return None
        values = list(reversed(data.get("values", [])))
        if not values:
            return None
        return {
            "opens":   [float(v["open"])   for v in values],
            "closes":  [float(v["close"])  for v in values],
            "highs":   [float(v["high"])   for v in values],
            "lows":    [float(v["low"])    for v in values],
            "volumes": [float(v["volume"]) for v in values],
            "dates":   [v["datetime"]      for v in values],
        }
    except Exception as e:
        print(f"  [PRICE] Exception for {ticker}: {e}")
        return None


def fetch_earnings(ticker: str) -> tuple[Optional[str], Optional[int]]:
    url = (f"https://api.twelvedata.com/earnings"
           f"?symbol={ticker}&apikey={cfg.twelve_key}")
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("status") == "error":
            return None, None
        today = date.today()
        future = [
            e for e in data.get("earnings", [])
            if e.get("date") and date.fromisoformat(e["date"]) >= today
        ]
        if not future:
            return None, None
        future.sort(key=lambda e: e["date"])
        next_date = future[0]["date"]
        days = (date.fromisoformat(next_date) - today).days
        return next_date, days
    except Exception:
        return None, None


def fetch_chain(ticker: str) -> Optional[dict]:
    """Fetch options chain from Tastytrade nested endpoint."""
    headers = _tt_headers()
    if not headers:
        return None

    base = cfg.tastytrade_base

    # Nested chain (expirations + strikes in one call)
    try:
        r = requests.get(
            f"{base}/option-chains/{ticker}/nested",
            headers=headers, timeout=15,
        )
        data = r.json()
    except Exception as e:
        print(f"  [CHAIN] Tastytrade chain error for {ticker}: {e}")
        return None

    raw_expirations = (data.get("data") or {}).get("items") or []

    # Collect all expiration date strings
    expirations: list[str] = sorted({
        item.get("expiration-date", "") for item in raw_expirations
        if item.get("expiration-date")
    })

    # Underlying price -- fetch from a separate quotes call
    try:
        qr = requests.get(
            f"{base}/market-data/options/{ticker}",
            headers=headers, timeout=10,
        )
        qdata = qr.json()
        items = (qdata.get("data") or {}).get("items") or []
        underlying = float(items[0].get("last", 0) or 0) if items else 0.0
    except Exception:
        underlying = 0.0

    # Find an expiration within DTE window
    today = date.today()
    chosen_exp: Optional[str] = None
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
            chosen_exp = exp_str
            chosen_items = item.get("strikes") or []
            break

    if not chosen_exp:
        return {"expirations": expirations, "options": [], "underlyingPrice": underlying,
                "chosenExpiration": None, "dte": None}

    dte_chosen = (date.fromisoformat(chosen_exp) - today).days

    # Normalise strikes into the shared option dict shape.
    # Tastytrade nested strikes look like:
    #   {"strike-price": "450.0", "call": {...}, "put": {...}}
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

    return {
        "expirations":      expirations,
        "options":          options,
        "underlyingPrice":  underlying,
        "chosenExpiration": chosen_exp,
        "dte":              dte_chosen,
    }


# ---------------------------------------------------------------------------
# Contract selection
# ---------------------------------------------------------------------------

def select_contract(chain: dict, direction: str) -> Optional[dict]:
    opt_type = "call" if direction == "CALL" else "put"
    underlying = chain.get("underlyingPrice", 0)
    options = [o for o in chain.get("options", []) if o["optionType"] == opt_type]
    if not options:
        return None

    max_price = cfg.max_contract_price  # per-contract price ceiling

    # Target delta ~0.40 (slightly OTM for risk management)
    target_delta = 0.40 if direction == "CALL" else -0.40

    def delta_score(o: dict) -> float:
        d = o.get("delta", 0)
        if direction == "PUT":
            d = abs(d)
        return abs(d - abs(target_delta))

    def has_liquidity(o: dict) -> bool:
        return o["bid"] > 0 and o["ask"] > 0 and o["volume"] >= 10

    def affordable(o: dict) -> bool:
        mid = (o["bid"] + o["ask"]) / 2 if o["bid"] and o["ask"] else o.get("last", 0)
        return max_price <= 0 or mid <= max_price

    liquid = [o for o in options if has_liquidity(o)]
    candidates = liquid if liquid else options

    # Apply price ceiling filter
    affordable_candidates = [o for o in candidates if affordable(o)]
    if not affordable_candidates:
        print(f"  [CONTRACT] No contracts under ${max_price:.2f} -- relaxing price filter")
        affordable_candidates = candidates  # fall back to all if nothing affordable

    # Prefer OTM for calls (strike > underlying), ITM for risk mgmt
    if direction == "CALL":
        otm = [o for o in affordable_candidates if o["strike"] > underlying * 0.99]
    else:
        otm = [o for o in affordable_candidates if o["strike"] < underlying * 1.01]

    pool = otm if otm else affordable_candidates
    pool.sort(key=delta_score)
    best = pool[0]

    mid = round((best["bid"] + best["ask"]) / 2, 2) if best["bid"] and best["ask"] else best["last"]
    return {**best, "limit_price": mid, "expiry": chain["chosenExpiration"]}


# ---------------------------------------------------------------------------
# IV analysis
# ---------------------------------------------------------------------------

def iv_status(atm_iv: float, hv30_val: float) -> str:
    if atm_iv <= 0 or hv30_val <= 0:
        return "FAIR"
    ratio = atm_iv / hv30_val
    if ratio > 1.20:
        return "EXPENSIVE"
    if ratio < 0.85:
        return "CHEAP"
    return "FAIR"


def atm_iv_from_chain(chain: dict) -> float:
    underlying = chain.get("underlyingPrice", 0)
    if not underlying:
        return 0.0
    opt_type = "call"
    options = [o for o in chain.get("options", []) if o["optionType"] == opt_type]
    if not options:
        return 0.0
    options.sort(key=lambda o: abs(o["strike"] - underlying))
    return options[0].get("iv", 0.0) or 0.0


# ---------------------------------------------------------------------------
# Supabase logging
# ---------------------------------------------------------------------------

def _sb_insert(table: str, row: dict) -> Optional[str]:
    if not cfg.sb_url or not cfg.sb_key:
        return None
    try:
        r = requests.post(
            f"{cfg.sb_url}/rest/v1/{table}",
            headers={
                "apikey": cfg.sb_key,
                "Authorization": f"Bearer {cfg.sb_key}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json=row,
            timeout=10,
        )
        data = r.json()
        if isinstance(data, list) and data:
            return data[0].get("id")
    except Exception:
        pass
    return None


def log_signal(ticker: str, direction: str, sig: dict, chain: dict,
               earnings_date: Optional[str], days_to_earnings: Optional[int],
               contract: Optional[dict]) -> Optional[str]:
    ew = "NONE"
    if days_to_earnings is not None:
        ew = "URGENT" if days_to_earnings <= 7 else "IN_WINDOW" if days_to_earnings <= 30 else "NONE"

    atm_iv_val = atm_iv_from_chain(chain) * 100
    hv30_val   = sig.get("hv30", 0)
    iv_stat    = iv_status(atm_iv_val, hv30_val) if atm_iv_val and hv30_val else None

    row = {
        "ticker":              ticker,
        "direction":           direction,
        "confidence":          sig["confidence"],
        "strategy_agreement":  sig["agreement"],
        "suggested_strike":    contract["strike"] if contract else None,
        "dte_label":           f"{chain.get('dte', '?')}d" if chain.get("dte") else None,
        "atm_iv":              round(atm_iv_val, 4) if atm_iv_val else None,
        "hv30":                round(hv30_val / 100, 4) if hv30_val else None,
        "iv_status":           iv_stat,
        "earnings_warning":    ew,
        "days_to_earnings":    days_to_earnings,
        "matched_contract":    contract,
        "price_at_signal":     chain.get("underlyingPrice"),
    }
    return _sb_insert("signal_log", row)


def log_trade(ticker: str, direction: str, contract: dict, contracts: int,
              signal_id: Optional[str], status: str,
              order_id: Optional[str] = None, error: Optional[str] = None,
              skip_reason: Optional[str] = None) -> Optional[str]:
    row = {
        "signal_id":           signal_id,
        "ticker":              ticker,
        "direction":           direction,
        "strike":              contract["strike"],
        "expiry":              contract["expiry"],
        "contracts":           contracts,
        "limit_price":         contract["limit_price"],
        "robinhood_order_id":  order_id,
        "status":              status,
        "skip_reason":         skip_reason,
        "error":               error,
    }
    return _sb_insert("trades_log", row)


# ---------------------------------------------------------------------------
# Robinhood order
# ---------------------------------------------------------------------------

_rh_logged_in = False

def rh_login() -> bool:
    global _rh_logged_in
    if _rh_logged_in:
        return True
    if not cfg.rh_user or not cfg.rh_pass:
        print("  [RH] ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD not set")
        return False
    try:
        import robin_stocks.robinhood as r
        r.login(cfg.rh_user, cfg.rh_pass)
        _rh_logged_in = True
        return True
    except Exception as e:
        print(f"  [RH] Login failed: {e}")
        return False


def get_buying_power() -> float:
    """Return available options buying power from Robinhood."""
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


def place_order(ticker: str, direction: str, contract: dict, contracts: int, dry_run: bool) -> tuple[bool, Optional[str]]:
    opt_type = "call" if direction == "CALL" else "put"
    estimated_cost = round(contract["limit_price"] * contracts * 100, 2)
    print(f"  -> {direction} {ticker} ${contract['strike']} {contract['expiry']} "
          f"x{contracts} @ ${contract['limit_price']:.2f}  (est. cost ${estimated_cost:.2f})")

    if dry_run:
        print("  [DRY-RUN] Order not placed.")
        return True, "dry-run"

    if not rh_login():
        return False, None

    # Check buying power before placing
    buying_power = get_buying_power()
    print(f"  [RH] Buying power: ${buying_power:.2f}  |  Trade cost: ${estimated_cost:.2f}")
    if buying_power < estimated_cost:
        msg = f"Insufficient funds -- need ${estimated_cost:.2f}, have ${buying_power:.2f}"
        print(f"  [SKIP] {msg}")
        notify(f"BOT SKIPPED {ticker} {direction} -- Not enough funds",
               f"Need ${estimated_cost:.2f} to place trade\nRobinhood balance: ${buying_power:.2f}\n"
               f"Deposit more funds or lower contract sizes in .env.local")
        return False, None

    try:
        import robin_stocks.robinhood as r
        result = r.order_buy_option_limit(
            positionEffect="open",
            creditOrDebit="debit",
            price=contract["limit_price"],
            symbol=ticker,
            quantity=contracts,
            expirationDate=contract["expiry"],
            strike=contract["strike"],
            optionType=opt_type,
            timeInForce="gfd",
        )
        order_id = result.get("id") if result else None
        if order_id:
            print(f"  [RH] Order placed -- ID: {order_id}  Status: {result.get('state', '?')}")
        else:
            print(f"  [RH] Order response: {json.dumps(result, indent=2)}")
        return bool(order_id), order_id
    except Exception as e:
        print(f"  [RH] Order error: {e}")
        return False, None


# ---------------------------------------------------------------------------
# Position management
# ---------------------------------------------------------------------------

def fetch_option_quote(option_symbol: str) -> tuple[float, float]:
    """Returns (bid, ask) for an option symbol from Tastytrade."""
    headers = _tt_headers()
    if not headers:
        return 0.0, 0.0
    url = f"{cfg.tastytrade_base}/market-data/options/{option_symbol}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        items = (r.json().get("data") or {}).get("items") or []
        q = items[0] if items else {}
        return float(q.get("bid", 0) or 0), float(q.get("ask", 0) or 0)
    except Exception:
        return 0.0, 0.0


def _sb_patch(table: str, row_id: str, data: dict) -> None:
    if not cfg.sb_url or not cfg.sb_key:
        return
    try:
        requests.patch(
            f"{cfg.sb_url}/rest/v1/{table}?id=eq.{row_id}",
            headers={
                "apikey": cfg.sb_key,
                "Authorization": f"Bearer {cfg.sb_key}",
                "Content-Type": "application/json",
            },
            json=data,
            timeout=10,
        )
    except Exception:
        pass


def get_open_positions() -> list[dict]:
    if not cfg.sb_url or not cfg.sb_key:
        return []
    try:
        r = requests.get(
            f"{cfg.sb_url}/rest/v1/positions?status=eq.open&select=*",
            headers={"apikey": cfg.sb_key, "Authorization": f"Bearer {cfg.sb_key}"},
            timeout=10,
        )
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def open_position(
    ticker: str, direction: str, contract: dict, contracts: int,
    underlying_price: float, signal_id: Optional[str], trade_id: Optional[str]
) -> None:
    entry = contract["limit_price"]
    _sb_insert("positions", {
        "signal_id":        signal_id,
        "trade_id":         trade_id,
        "ticker":           ticker,
        "direction":        direction,
        "option_symbol":    contract["symbol"],
        "strike":           contract["strike"],
        "expiry":           contract["expiry"],
        "contracts":        contracts,
        "entry_price":      entry,
        "entry_underlying": underlying_price,
        "peak_price":       entry,
        "status":           "open",
    })


def close_position(pos: dict, exit_price: float, reason: str, dry_run: bool) -> None:
    ticker     = pos["ticker"]
    direction  = pos["direction"]
    opt_symbol = pos["option_symbol"]
    contracts  = int(pos["contracts"])
    entry      = float(pos["entry_price"])
    opt_type   = "call" if direction == "CALL" else "put"
    pnl        = round((exit_price - entry) * contracts * 100, 2)

    print(f"  [EXIT-{reason.upper()}] {ticker} {direction} "
          f"entry ${entry:.2f} -> exit ${exit_price:.2f}  P&L: ${pnl:+.2f}")

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
        f"{'[DRY RUN] ' if dry_run else ''}BOT EXITED {ticker} {direction} -- {outcome}  ${pnl:+.2f}",
        f"Ticker: {ticker}\nDirection: {direction}\nOutcome: {outcome}\nP&L: ${pnl:+.2f}\n"
        f"Reason: {reason_label}\nEntry: ${entry:.2f} -> Exit: ${exit_price:.2f}\nContracts: {contracts}",
    )

    if dry_run:
        print(f"  [DRY-RUN] Would close {opt_symbol} x{contracts}")
        return

    if rh_login():
        try:
            import robin_stocks.robinhood as r
            r.order_sell_option_limit(
                positionEffect="close",
                creditOrDebit="credit",
                price=exit_price,
                symbol=ticker,
                quantity=contracts,
                expirationDate=pos["expiry"],
                strike=float(pos["strike"]),
                optionType=opt_type,
                timeInForce="gfd",
            )
        except Exception as e:
            print(f"  [CLOSE ERROR] {e}")

    _sb_patch("positions", pos["id"], {
        "closed_at":   datetime.now(timezone.utc).isoformat(),
        "exit_price":  exit_price,
        "exit_reason": reason,
        "pnl":         pnl,
        "status":      "closed",
    })
    # Sync outcome back to signal_log
    if pos.get("signal_id"):
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "SCRATCH")
        _sb_patch("signal_log", pos["signal_id"], {"outcome": outcome, "outcome_pnl": pnl})


def check_position(pos: dict, dry_run: bool) -> bool:
    """Returns True if position was closed."""
    ticker     = pos["ticker"]
    direction  = pos["direction"]
    opt_symbol = pos["option_symbol"]
    entry      = float(pos["entry_price"])
    peak       = float(pos.get("peak_price") or entry)
    contracts  = int(pos["contracts"])

    bid, ask = fetch_option_quote(opt_symbol)
    if bid <= 0 and ask <= 0:
        return False  # Can't get a quote -- skip this cycle
    mid = (bid + ask) / 2

    # Update live price and peak
    new_peak = max(peak, mid)
    _sb_patch("positions", pos["id"], {"current_price": mid, "peak_price": new_peak})

    pnl_pct  = (mid - entry) / max(entry, 0.01)
    peak_pct = (new_peak - entry) / max(entry, 0.01)

    print(f"  [{ticker} {direction}] entry ${entry:.2f}  now ${mid:.2f}  "
          f"P&L {pnl_pct*100:+.1f}%  peak {peak_pct*100:+.1f}%")

    # -- Stop loss
    if pnl_pct <= -cfg.stop_loss_pct:
        close_position(pos, max(bid, 0.01), "stop_loss", dry_run)
        return True

    # -- Tiered trailing stop -- rides the move, never caps it
    # Kicks in after trail_start_pct gain, tightens as position grows larger.
    # SPX example: $1.90 entry -> $1000 peak -> exits near $900, not at $3.80.
    if peak_pct >= cfg.trail_start_pct:
        if peak_pct >= 3.00:    # +300%+ peak -> very tight trail (10%)
            trail = 0.10
        elif peak_pct >= 1.00:  # +100%+ peak -> tighter trail (15%)
            trail = 0.15
        else:                   # +30%-100% peak -> base trail from config
            trail = cfg.trail_stop_pct
        pullback = (new_peak - mid) / max(new_peak, 0.01)
        if pullback >= trail:
            print(f"  [TRAIL] Peak {peak_pct*100:+.0f}%  pullback {pullback*100:.1f}%  trail {trail*100:.0f}%")
            close_position(pos, bid, "trail_stop", dry_run)
            return True

    # -- Safety-net take profit (very high -- rarely fires, prevents runaway loss of gains)
    if pnl_pct >= cfg.take_profit_pct:
        close_position(pos, bid, "take_profit", dry_run)
        return True

    # -- Chart-based exits (indicator checks + signal reversal)
    # Both reuse the same OHLCV fetch to save API calls.
    if cfg.indicator_exit or cfg.signal_exit:
        ohlcv = fetch_ohlcv(ticker)
        if ohlcv:
            closes_arr = np.array(ohlcv["closes"], dtype=float)
            highs_arr  = np.array(ohlcv["highs"],  dtype=float)
            lows_arr   = np.array(ohlcv["lows"],   dtype=float)
            opens_arr  = np.array(ohlcv.get("opens", []), dtype=float)

            if cfg.indicator_exit:
                ind_reason = indicator_exit_check(
                    direction, closes_arr, highs_arr, lows_arr, opens_arr, pnl_pct
                )
                if ind_reason:
                    rsi_now = round(rsi(closes_arr), 1)
                    print(f"  [INDICATOR EXIT] {ind_reason}  RSI={rsi_now}  P&L={pnl_pct*100:+.1f}%")
                    close_position(pos, bid, ind_reason, dry_run)
                    return True

            if cfg.signal_exit:
                fresh_chain = fetch_chain(ticker)
                sig = run_signal_engine(ohlcv, chain=fresh_chain)
                if (sig["direction"] and
                        sig["direction"] != direction and
                        sig["agreement"] >= cfg.signal_exit_agree):
                    print(f"  [REVERSAL] Signal flipped -> {sig['direction']} "
                          f"({sig['agreement']} strategies, {sig['confidence']}% conf)")
                    close_position(pos, bid, "signal_reversal", dry_run)
                    return True

    return False


def monitor_positions(dry_run: bool) -> None:
    positions = get_open_positions()
    if not positions:
        return
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Monitoring {len(positions)} open position(s)...")
    for pos in positions:
        try:
            check_position(pos, dry_run)
        except Exception as e:
            print(f"  [MONITOR ERROR] {pos.get('ticker', '?')}: {e}")


# ---------------------------------------------------------------------------
# Daily contracts guard
# ---------------------------------------------------------------------------

_daily_contracts_used = 0


def daily_remaining() -> int:
    return cfg.daily_max_contracts - _daily_contracts_used


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------

def _et_now() -> datetime:
    """Current time in US/Eastern, DST-aware (no external dependency)."""
    utc = datetime.now(timezone.utc)
    y   = utc.year
    mar1 = date(y, 3, 1)
    dst_start = date(y, 3, 1 + (6 - mar1.weekday()) % 7 + 7)   # 2nd Sunday of March
    nov1 = date(y, 11, 1)
    dst_end = date(y, 11, 1 + (6 - nov1.weekday()) % 7)         # 1st Sunday of November
    offset = timedelta(hours=-4) if dst_start <= utc.date() < dst_end else timedelta(hours=-5)
    return (utc + offset).replace(tzinfo=None)


def is_market_open() -> bool:
    et = _et_now()
    if et.weekday() >= 5:
        return False
    o = et.replace(hour=9,  minute=30, second=0, microsecond=0)
    c = et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return o <= et <= c


def is_trading_window() -> tuple[bool, str]:
    """
    True only 10:00 AM - 3:30 PM ET.
    Skips the opening 30-min whipsaw and the closing rush.
    Only affects new entries -- position monitoring runs regardless.
    """
    et = _et_now()
    h, m = et.hour, et.minute
    if h < 10:
        return False, f"waiting for 10:00 AM ET (now {h:02d}:{m:02d})"
    if (h == 15 and m >= 30) or h >= 16:
        return False, "no new entries after 3:30 PM ET"
    return True, ""


# Known high-impact events: bot will not open new positions during these windows.
# FOMC: blackout 30 min before -> 45 min after 2:00 PM announcement.
# CPI/NFP: released pre-market at 8:30 AM -- block 9:30 AM -> 10:30 AM (first hour of trading).
_HIGH_IMPACT_EVENTS: list[tuple[str, str, int, int]] = [
    # (date, name, hour_ET, minute_ET)
    # -- FOMC decision dates
    ("2025-01-29", "FOMC", 14, 0), ("2025-03-19", "FOMC", 14, 0),
    ("2025-05-07", "FOMC", 14, 0), ("2025-06-18", "FOMC", 14, 0),
    ("2025-07-30", "FOMC", 14, 0), ("2025-09-17", "FOMC", 14, 0),
    ("2025-10-29", "FOMC", 14, 0), ("2025-12-10", "FOMC", 14, 0),
    ("2026-01-28", "FOMC", 14, 0), ("2026-03-18", "FOMC", 14, 0),
    ("2026-05-06", "FOMC", 14, 0), ("2026-06-17", "FOMC", 14, 0),
    ("2026-07-29", "FOMC", 14, 0), ("2026-09-16", "FOMC", 14, 0),
    ("2026-10-28", "FOMC", 14, 0), ("2026-12-09", "FOMC", 14, 0),
    # -- CPI release dates
    ("2025-01-15", "CPI",  8, 30), ("2025-02-12", "CPI",  8, 30),
    ("2025-03-12", "CPI",  8, 30), ("2025-04-10", "CPI",  8, 30),
    ("2025-05-13", "CPI",  8, 30), ("2025-06-11", "CPI",  8, 30),
    ("2025-07-15", "CPI",  8, 30), ("2025-08-12", "CPI",  8, 30),
    ("2025-09-10", "CPI",  8, 30), ("2025-10-14", "CPI",  8, 30),
    ("2025-11-12", "CPI",  8, 30), ("2025-12-10", "CPI",  8, 30),
    ("2026-01-14", "CPI",  8, 30), ("2026-02-11", "CPI",  8, 30),
    ("2026-03-11", "CPI",  8, 30), ("2026-04-08", "CPI",  8, 30),
    ("2026-05-13", "CPI",  8, 30), ("2026-06-10", "CPI",  8, 30),
    ("2026-07-15", "CPI",  8, 30), ("2026-08-12", "CPI",  8, 30),
    ("2026-09-09", "CPI",  8, 30), ("2026-10-14", "CPI",  8, 30),
    ("2026-11-12", "CPI",  8, 30), ("2026-12-09", "CPI",  8, 30),
    # -- NFP / Jobs report
    ("2025-01-10", "NFP",  8, 30), ("2025-02-07", "NFP",  8, 30),
    ("2025-03-07", "NFP",  8, 30), ("2025-04-04", "NFP",  8, 30),
    ("2025-05-02", "NFP",  8, 30), ("2025-06-06", "NFP",  8, 30),
    ("2025-07-03", "NFP",  8, 30), ("2025-08-01", "NFP",  8, 30),
    ("2025-09-05", "NFP",  8, 30), ("2025-10-03", "NFP",  8, 30),
    ("2025-11-07", "NFP",  8, 30), ("2025-12-05", "NFP",  8, 30),
    ("2026-01-09", "NFP",  8, 30), ("2026-02-06", "NFP",  8, 30),
    ("2026-03-06", "NFP",  8, 30), ("2026-04-03", "NFP",  8, 30),
    ("2026-05-01", "NFP",  8, 30), ("2026-06-05", "NFP",  8, 30),
    ("2026-07-02", "NFP",  8, 30), ("2026-08-07", "NFP",  8, 30),
    ("2026-09-04", "NFP",  8, 30), ("2026-10-02", "NFP",  8, 30),
    ("2026-11-06", "NFP",  8, 30), ("2026-12-04", "NFP",  8, 30),
]


def is_near_high_impact_event() -> tuple[bool, str]:
    """
    Returns (True, event_name) when inside a no-trade blackout window.
    FOMC: 30 min before -> 45 min after 2 PM announcement.
    CPI/NFP: 9:30 AM open -> 10:30 AM (first trading hour, vol still settling).
    """
    et = _et_now()
    today = et.strftime("%Y-%m-%d")
    for ev_date, ev_name, ev_h, ev_m in _HIGH_IMPACT_EVENTS:
        if ev_date != today:
            continue
        ev_time = et.replace(hour=ev_h, minute=ev_m, second=0, microsecond=0)
        if ev_name == "FOMC":
            blk_start = ev_time - timedelta(minutes=30)
            blk_end   = ev_time + timedelta(minutes=45)
        else:
            blk_start = et.replace(hour=9,  minute=30, second=0, microsecond=0)
            blk_end   = et.replace(hour=10, minute=30, second=0, microsecond=0)
        if blk_start <= et <= blk_end:
            return True, ev_name
    return False, ""


def is_news_spike(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
    multiplier: float = 2.5, lookback: int = 20,
) -> tuple[bool, str]:
    """
    Detects surprise intraday news spikes by comparing the current bar's range
    to recent ATR.  Fires for any unscheduled event: Fed speak, geopolitical
    headlines, flash crashes, surprise data revisions, circuit breakers, etc.
    Returns (True, reason) -- caller should skip entry and wait for next scan.
    """
    if len(closes) < lookback + 2:
        return False, ""
    recent_atr = atr(
        highs[-(lookback + 1):-1], lows[-(lookback + 1):-1],
        closes[-(lookback + 1):-1], lookback,
    )
    if recent_atr <= 0:
        return False, ""
    cur_range = float(highs[-1]) - float(lows[-1])
    ratio = cur_range / recent_atr
    if ratio >= multiplier:
        return True, f"bar range {cur_range:.2f} is {ratio:.1f}x ATR -- possible news spike"
    # Two consecutive outsized bars = market still digesting news
    if len(closes) >= lookback + 3:
        prev_range = float(highs[-2]) - float(lows[-2])
        if cur_range >= recent_atr * 1.8 and prev_range >= recent_atr * 1.8:
            return True, f"2 consecutive outsized bars ({ratio:.1f}x ATR) -- news digestion"
    return False, ""


def _market_breadth_agrees(direction: str, ticker: str) -> bool:
    """
    Macro breadth proxy: SPY confirms QQQ trades and vice-versa; SPY confirms all others.
    Only blocks when the reference index is in a confirmed opposing trend (conf >= 65).
    Returns True when uncertain -- never silences a valid signal over a minor divergence.
    """
    t = ticker.upper()
    ref = "QQQ" if t in ("SPY", "SPX", "SPXW") else "SPY"
    if t == ref:
        return True
    ohlcv = fetch_ohlcv(ref)
    if not ohlcv:
        return True  # network issue -- allow trade
    closes  = np.array(ohlcv["closes"], dtype=float)
    rsi_val = rsi(closes)
    trend_dir, trend_conf = trend_momentum(closes, rsi_val)
    return not (trend_dir and trend_dir != direction and trend_conf >= 65)


# ---------------------------------------------------------------------------
# Core scan -- one ticker
# ---------------------------------------------------------------------------

def scan_ticker(ticker: str, dry_run: bool) -> None:
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning {ticker}...")

    # Time-of-day gate -- entries only 10:00 AM - 3:30 PM ET
    in_window, window_reason = is_trading_window()
    if not in_window:
        print(f"  [SKIP] {window_reason}")
        return

    # High-impact event blackout -- no new entries near FOMC / CPI / NFP
    near_event, event_name = is_near_high_impact_event()
    if near_event:
        print(f"  [SKIP] {event_name} blackout -- no new entries during event window")
        return

    # 1. OHLCV
    ohlcv = fetch_ohlcv(ticker)
    if not ohlcv:
        print(f"  [SKIP] No price data")
        return

    # News spike guard -- skip entry if current bar is abnormally large
    spike, spike_reason = is_news_spike(
        np.array(ohlcv["highs"],  dtype=float),
        np.array(ohlcv["lows"],   dtype=float),
        np.array(ohlcv["closes"], dtype=float),
    )
    if spike:
        print(f"  [SKIP] News spike -- {spike_reason}")
        return

    # 2. Fetch chain early -- options flow and IV rank vote in the signal engine
    chain = fetch_chain(ticker)
    if not chain or not chain.get("chosenExpiration"):
        print(f"  [SKIP] No expiration in {cfg.min_dte}-{cfg.max_dte} DTE window")
        return

    # 3. Signal engine (chain passed in for flow + IV rank)
    sig = run_signal_engine(ohlcv, chain=chain)
    fired = ", ".join(s["strategy"] for s in sig["signals"]) or "none"
    iv_tag = f"  IV*{sig.get('iv_mult', 1.0):.2f}" if sig.get("iv_mult", 1.0) != 1.0 else ""
    print(f"  Signal: {sig['direction'] or 'NONE'}  Confidence: {sig['confidence']}%  "
          f"Agreement: {sig['agreement']}/{TOTAL_STRATEGIES}  RSI: {sig['rsi']}{iv_tag}")
    print(f"  Patterns fired: [{fired}]")

    if not sig["direction"]:
        print(f"  [SKIP] No signal")
        return

    # 4. Earnings guard
    earnings_date, days_to_earnings = fetch_earnings(ticker)
    if days_to_earnings is not None and days_to_earnings <= 7:
        print(f"  [BLOCK] Earnings in {days_to_earnings} days -- skipping")
        return

    # 5. Confidence filter
    contracts = calc_contracts(sig["confidence"])
    if contracts == 0:
        print(f"  [SKIP] Confidence {sig['confidence']}% < min {cfg.min_confidence}%")
        return

    # 6. Daily cap
    remaining = daily_remaining()
    if remaining <= 0:
        print(f"  [SKIP] Daily max contracts ({cfg.daily_max_contracts}) reached")
        return
    contracts = min(contracts, remaining)

    # 7. Contract selection
    contract = select_contract(chain, sig["direction"])
    if not contract:
        print(f"  [SKIP] No suitable contract found")
        return

    atm_iv_val = atm_iv_from_chain(chain) * 100
    iv_stat = iv_status(atm_iv_val, sig["hv30"]) if atm_iv_val and sig["hv30"] else "FAIR"
    print(f"  Contract: {contract['symbol']}  Mid: ${contract['limit_price']:.2f}  "
          f"Delta: {contract['delta']:.2f}  IV: {atm_iv_val:.1f}%  IV/HV: {iv_stat}")
    if days_to_earnings:
        print(f"  Earnings in {days_to_earnings} days -- IN_WINDOW, proceeding with caution")

    # IVR entry gate -- expensive options require stronger signal agreement
    if iv_stat == "EXPENSIVE" and sig["agreement"] < 3:
        print(f"  [SKIP] IV expensive ({atm_iv_val:.1f}% vs HV {sig['hv30']:.1f}%) "
              f"-- need >=3 strategies agreeing, got {sig['agreement']}")
        return

    # Market breadth -- macro backdrop must not be in a clear opposing trend
    if not _market_breadth_agrees(sig["direction"], ticker):
        print(f"  [SKIP] Market breadth opposing {sig['direction']} -- macro mismatch")
        return

    # 8. Log signal
    signal_id = log_signal(ticker, sig["direction"], sig, chain, earnings_date, days_to_earnings, contract)

    # 9. Place order
    success, order_id = place_order(ticker, sig["direction"], contract, contracts, dry_run)
    status = "placed" if success else "failed"
    trade_id = log_trade(ticker, sig["direction"], contract, contracts, signal_id, status, order_id)

    if success:
        global _daily_contracts_used
        if not dry_run:
            _daily_contracts_used += contracts
        print(f"  Daily contracts used: {_daily_contracts_used}/{cfg.daily_max_contracts}")
        notify(
            f"{'[DRY RUN] ' if dry_run else ''}BOT ENTERED {ticker} {sig['direction']}",
            f"Ticker: {ticker}\nDirection: {sig['direction']}\nContracts: {contracts}\n"
            f"Strike: ${contract['strike']}\nExpiry: {contract.get('expiry','?')}\n"
            f"Fill price: ${contract['limit_price']:.2f}\nConfidence: {sig['confidence']}%\n"
            f"Contract: {contract['symbol']}",
        )
        # Track open position for exit management
        open_position(
            ticker, sig["direction"], contract, contracts,
            chain.get("underlyingPrice", 0), signal_id, trade_id
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="OptionEdge automated trading bot")
    parser.add_argument("--loop",      action="store_true", help="Run continuously on a schedule")
    parser.add_argument("--dry-run",   action="store_true", help="Generate signals but do not place orders")
    parser.add_argument("--ticker",    nargs="+",           help="Override WATCH_TICKERS for this run")
    args = parser.parse_args()

    # Load .env if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    tickers = [t.upper() for t in args.ticker] if args.ticker else cfg.watch_tickers
    if not tickers:
        print("ERROR: No tickers configured. Set WATCH_TICKERS=SPY,QQQ or use --ticker SPY.")
        sys.exit(1)

    if not cfg.twelve_key:
        print("ERROR: TWELVE_DATA_API_KEY not set.")
        sys.exit(1)
    if not cfg.tastytrade_user or not cfg.tastytrade_pass:
        print("ERROR: TASTYTRADE_USERNAME / TASTYTRADE_PASSWORD not set.")
        sys.exit(1)

    if args.dry_run:
        print("** DRY-RUN MODE -- signals generated, no orders placed **")

    def run_scan() -> None:
        for i, ticker in enumerate(tickers):
            try:
                scan_ticker(ticker, dry_run=args.dry_run)
            except Exception as e:
                print(f"  [ERROR] {ticker}: {e}")
            # Tastytrade market data rate limit: 2 req/s -- stagger tickers
            if i < len(tickers) - 1:
                time.sleep(0.6)

    if args.loop:
        print(f"Starting loop -- signal scan every {cfg.loop_interval_min}min, "
              f"position check every {cfg.pos_check_min}min")
        print(f"Exit rules: stop={cfg.stop_loss_pct*100:.0f}%  "
              f"target={cfg.take_profit_pct*100:.0f}%  "
              f"trail={cfg.trail_stop_pct*100:.0f}%  "
              f"signal_reversal={'ON' if cfg.signal_exit else 'OFF'}")

        last_signal_t   = datetime.min
        last_pos_t      = datetime.min
        TICK            = 20  # seconds between loop ticks

        while True:
            now = datetime.now()
            if not is_market_open():
                print(f"[{now.strftime('%H:%M:%S')}] Market closed -- waiting...")
                time.sleep(60)
                continue

            # Position monitor runs first -- fastest exit possible
            if (now - last_pos_t).total_seconds() >= cfg.pos_check_min * 60:
                try:
                    monitor_positions(dry_run=args.dry_run)
                except Exception as e:
                    print(f"  [MONITOR ERROR] {e}")
                last_pos_t = now

            # Signal scan for new entries
            if (now - last_signal_t).total_seconds() >= cfg.loop_interval_min * 60:
                run_scan()
                last_signal_t = now

            time.sleep(TICK)
    else:
        # Single run -- check positions first, then scan for new signals
        monitor_positions(dry_run=args.dry_run)
        run_scan()


if __name__ == "__main__":
    main()
