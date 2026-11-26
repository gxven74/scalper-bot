"""Spot (long-only) strateji kutuphanesi.

Her strateji hem backtest hem canli paper trading tarafinda birebir ayni
evaluate_row fonksiyonunu kullanir (sifir tutarsizlik).
"""

from dataclasses import dataclass
from typing import Callable, Dict, Optional

import numpy as np
import pandas as pd


@dataclass
class Signal:
    action: str  # "BUY", "SELL", "HOLD"
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_atr_mult: Optional[float] = None
    reason: str = ""


def _valid(*vals) -> bool:
    for v in vals:
        if v is None or (isinstance(v, (float, np.floating)) and np.isnan(v)):
            return False
    return True


def strategy_baseline_rsi(row: pd.Series, prev: pd.Series, in_position: bool) -> Signal:
    """Mevcut eski botun orijinal stratejisi (karsilastirma baz noktasi)."""
    rsi_val = row["rsi_14"]
    close = row["close"]
    if not _valid(rsi_val, close):
        return Signal("HOLD")

    if not in_position and rsi_val <= 30.0:
        return Signal(
            action="BUY",
            stop_loss=close * (1.0 - 0.015),
            take_profit=close * (1.0 + 0.020),
            trailing_atr_mult=None,
            reason=f"Baseline RSI={rsi_val:.1f}<=30",
        )
    return Signal("HOLD")


def strategy_rsi_trend_atr(row: pd.Series, prev: pd.Series, in_position: bool) -> Signal:
    """Yukselis trendinde (EMA50>EMA200) RSI geri cekilme + toparlanma donusu."""
    close, prev_close = row["close"], prev["close"]
    rsi_now, rsi_prev = row["rsi_14"], prev["rsi_14"]
    ema50, ema200 = row["ema_50"], row["ema_200"]
    atr_val = row["atr_14"]

    if not _valid(close, prev_close, rsi_now, rsi_prev, ema50, ema200, atr_val):
        return Signal("HOLD")

    if not in_position:
        uptrend = close > ema200 and ema50 > ema200
        pullback_bounce = (rsi_prev <= 38.0) and (rsi_now > rsi_prev) and (close > prev_close)
        if uptrend and pullback_bounce:
            return Signal(
                action="BUY",
                stop_loss=close - 2.0 * atr_val,
                take_profit=close + 3.5 * atr_val,
                trailing_atr_mult=2.2,
                reason=f"Trend+RSI bounce ({rsi_prev:.1f}->{rsi_now:.1f})",
            )
    return Signal("HOLD")


def strategy_bb_reversion_trend(row: pd.Series, prev: pd.Series, in_position: bool) -> Signal:
    """Yukselis trendinde alt Bollinger Bandi ihlali sonrasi iceri geri donus."""
    close, open_p = row["close"], row["open"]
    prev_low, prev_bb_low = prev["low"], prev["bb_lower"]
    bb_low, bb_up = row["bb_lower"], row["bb_upper"]
    ema200, rsi_now, atr_val = row["ema_200"], row["rsi_14"], row["atr_14"]

    if not _valid(close, prev_low, prev_bb_low, bb_low, bb_up, ema200, rsi_now, atr_val):
        return Signal("HOLD")

    if not in_position:
        uptrend = close > ema200
        reclaimed = prev_low <= prev_bb_low and close > bb_low and close > open_p and rsi_now < 48.0
        if uptrend and reclaimed:
            sl = min(prev_low, row["low"]) - 1.0 * atr_val
            tp = max(bb_up, close + 2.5 * atr_val)
            return Signal(
                action="BUY",
                stop_loss=sl,
                take_profit=tp,
                trailing_atr_mult=None,
                reason=f"BB lower reclaim RSI={rsi_now:.1f}",
            )
    return Signal("HOLD")


def strategy_ema_pullback_momentum(row: pd.Series, prev: pd.Series, in_position: bool) -> Signal:
    """Guclu yukselis trendinde (EMA20>EMA50>EMA200, ADX>20) EMA20'ye geri cekilme alimi."""
    close, open_p = row["close"], row["open"]
    ema20, ema50, ema200 = row["ema_20"], row["ema_50"], row["ema_200"]
    prev_low, prev_ema20 = prev["low"], prev["ema_20"]
    adx_val, atr_val = row["adx_14"], row["atr_14"]
    vol, vol_sma = row["volume"], row["vol_sma_20"]

    if not _valid(close, ema20, ema50, ema200, prev_low, prev_ema20, adx_val, atr_val, vol, vol_sma):
        return Signal("HOLD")

    if not in_position:
        strong_uptrend = (ema20 > ema50 > ema200) and (adx_val > 20.0)
        pulled_back = (prev_low <= prev_ema20 * 1.003) and (close > ema20) and (close > open_p)
        vol_ok = vol >= vol_sma * 0.9
        if strong_uptrend and pulled_back and vol_ok:
            return Signal(
                action="BUY",
                stop_loss=close - 2.0 * atr_val,
                take_profit=None,
                trailing_atr_mult=2.5,
                reason=f"EMA20 pullback ADX={adx_val:.1f}",
            )
    else:
        if close < ema50:
            return Signal(action="SELL", reason="Close < EMA50 trend exit")

    return Signal("HOLD")


def strategy_donchian_breakout(row: pd.Series, prev: pd.Series, in_position: bool) -> Signal:
    """Turtle tarzı Donchian kirilim + EMA200 filtresi + ATR iz suren stop."""
    close = row["close"]
    ema200, adx_val, atr_val = row["ema_200"], row["adx_14"], row["atr_14"]
    d_high, d_low = row["donchian_high_20"], row["donchian_low_10"]
    vol, vol_sma = row["volume"], row["vol_sma_20"]

    if not _valid(close, ema200, adx_val, atr_val, d_high, d_low, vol, vol_sma):
        return Signal("HOLD")

    if not in_position:
        if close > ema200 and adx_val > 18.0 and close > d_high and vol >= vol_sma:
            return Signal(
                action="BUY",
                stop_loss=close - 2.2 * atr_val,
                take_profit=None,
                trailing_atr_mult=2.5,
                reason=f"Donchian20 breakout ADX={adx_val:.1f}",
            )
    else:
        if close < d_low:
            return Signal(action="SELL", reason="Donchian10 low exit")

    return Signal("HOLD")


def strategy_supertrend_ema(row: pd.Series, prev: pd.Series, in_position: bool) -> Signal:
    """SuperTrend yukselis donusu + EMA200 makro trend filtresi."""
    close = row["close"]
    ema200, adx_val, atr_val = row["ema_200"], row["adx_14"], row["atr_14"]
    st_val, st_dir, prev_st_dir = row["supertrend"], row["st_dir"], prev["st_dir"]

    if not _valid(close, ema200, adx_val, atr_val, st_val):
        return Signal("HOLD")

    if not in_position:
        flipped_bull = (prev_st_dir == -1) and (st_dir == 1)
        if flipped_bull and close > ema200 and adx_val > 18.0:
            sl = max(st_val, close - 2.5 * atr_val)
            if sl >= close:
                sl = close - 2.0 * atr_val
            return Signal(
                action="BUY",
                stop_loss=sl,
                take_profit=None,
                trailing_atr_mult=2.8,
                reason=f"SuperTrend bull flip ADX={adx_val:.1f}",
            )
    else:
        if st_dir == -1:
            return Signal(action="SELL", reason="SuperTrend bear flip")

    return Signal("HOLD")


StrategyFunc = Callable[[pd.Series, pd.Series, bool], Signal]

STRATEGIES: Dict[str, StrategyFunc] = {
    "baseline_rsi": strategy_baseline_rsi,
    "rsi_trend_atr": strategy_rsi_trend_atr,
    "bb_reversion_trend": strategy_bb_reversion_trend,
    "ema_pullback_momentum": strategy_ema_pullback_momentum,
    "donchian_breakout": strategy_donchian_breakout,
    "supertrend_ema": strategy_supertrend_ema,
}
