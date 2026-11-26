"""Vektorize teknik indikator hesaplamalari (lookahead bias icermez)."""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_prev_close = (df["high"] - df["close"].shift(1)).abs()
    low_prev_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def bollinger_bands(series: pd.Series, period: int = 20, std_mult: float = 2.0):
    mid = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return upper, mid, lower


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr_val = tr.ewm(com=period - 1, min_periods=period).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(com=period - 1, min_periods=period).mean() / atr_val.replace(0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(com=period - 1, min_periods=period).mean() / atr_val.replace(0, np.nan)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(com=period - 1, min_periods=period).mean()


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """Klasik SuperTrend hesaplamasi (sadece gecmis ve mevcut mum verisiyle)."""
    hl2 = (df["high"] + df["low"]) / 2.0
    atr_series = atr(df, period)

    upper_basic = hl2 + multiplier * atr_series
    lower_basic = hl2 - multiplier * atr_series

    n = len(df)
    final_upper = upper_basic.to_numpy(copy=True)
    final_lower = lower_basic.to_numpy(copy=True)
    close = df["close"].to_numpy()
    direction = np.zeros(n, dtype=int)  # +1 yukselis, -1 dusus
    st_line = np.full(n, np.nan)

    first_valid = atr_series.first_valid_index()
    if first_valid is None:
        return pd.Series(st_line, index=df.index), pd.Series(direction, index=df.index)

    start_pos = df.index.get_loc(first_valid)
    direction[start_pos] = 1
    st_line[start_pos] = final_lower[start_pos]

    for i in range(start_pos + 1, n):
        if np.isnan(final_upper[i]) or np.isnan(final_lower[i]):
            continue

        # Band sabitleme
        if not np.isnan(final_upper[i - 1]):
            if not (final_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]):
                final_upper[i] = final_upper[i - 1]
        if not np.isnan(final_lower[i - 1]):
            if not (final_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]):
                final_lower[i] = final_lower[i - 1]

        prev_dir = direction[i - 1]
        if prev_dir == 1:
            if close[i] < final_lower[i]:
                direction[i] = -1
                st_line[i] = final_upper[i]
            else:
                direction[i] = 1
                st_line[i] = final_lower[i]
        else:
            if close[i] > final_upper[i]:
                direction[i] = 1
                st_line[i] = final_lower[i]
            else:
                direction[i] = -1
                st_line[i] = final_upper[i]

    return pd.Series(st_line, index=df.index), pd.Series(direction, index=df.index)


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Stratejilerin ihtiyac duydugu tum indikatorleri tek seferde ekler."""
    out = df.copy()
    out["rsi_14"] = rsi(out["close"], 14)
    out["ema_20"] = ema(out["close"], 20)
    out["ema_50"] = ema(out["close"], 50)
    out["ema_200"] = ema(out["close"], 200)
    out["atr_14"] = atr(out, 14)
    out["adx_14"] = adx(out, 14)
    out["vol_sma_20"] = sma(out["volume"], 20)
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = bollinger_bands(out["close"], 20, 2.0)
    out["donchian_high_20"] = out["high"].rolling(20, min_periods=20).max().shift(1)
    out["donchian_low_10"] = out["low"].rolling(10, min_periods=10).min().shift(1)
    out["supertrend"], out["st_dir"] = supertrend(out, period=10, multiplier=3.0)
    return out
