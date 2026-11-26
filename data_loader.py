"""Binance gecmis ve canli mum (OHLCV) verisi cekme ve yerel onbellekleme modulu.

Bulut sunuculari (GitHub Actions / ABD IP'leri) icin Binance'in resmi ve
cografi engelsiz piyasa verisi uc noktasi (data-api.binance.vision) desteklenir.
"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd
import requests

from config import DATA_DIR

BINANCE_DATA_BASE_URL = "https://data-api.binance.vision"

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


def _to_binance_symbol(symbol: str) -> str:
    return symbol.replace("/", "").upper()


def create_exchange():
    """HTTP Session dondurur (ccxt gerektirmez, bulutta cok hizli ve hafiftir)."""
    session = requests.Session()
    session.headers.update({"User-Agent": "ScalperPaperBot/1.0"})
    return session


def _fetch_klines_rest(
    symbol: str,
    timeframe: str,
    limit: int = 300,
    start_time_ms: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> List[list]:
    sess = session if isinstance(session, requests.Session) else create_exchange()
    params = {
        "symbol": _to_binance_symbol(symbol),
        "interval": timeframe,
        "limit": min(1000, limit),
    }
    if start_time_ms is not None:
        params["startTime"] = int(start_time_ms)

    url = f"{BINANCE_DATA_BASE_URL}/api/v3/klines"
    resp = sess.get(url, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    # [open_time, open, high, low, close, volume, ...]
    return [
        [
            int(k[0]),
            float(k[1]),
            float(k[2]),
            float(k[3]),
            float(k[4]),
            float(k[5]),
        ]
        for k in raw
    ]


def fetch_live_price(symbol: str, session: Optional[requests.Session] = None) -> float:
    sess = session if isinstance(session, requests.Session) else create_exchange()
    url = f"{BINANCE_DATA_BASE_URL}/api/v3/ticker/price"
    resp = sess.get(url, params={"symbol": _to_binance_symbol(symbol)}, timeout=10)
    resp.raise_for_status()
    return float(resp.json()["price"])


def _cache_path(symbol: str, timeframe: str, days: int) -> Path:
    safe_sym = symbol.replace("/", "_")
    return DATA_DIR / f"{safe_sym}_{timeframe}_{days}d.csv"


def fetch_historical_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    days: int = 365,
    use_cache: bool = True,
    max_cache_age_hours: int = 12,
    exchange=None,
) -> pd.DataFrame:
    """Belirtilen gun sayisi kadar kapanmis gecmis OHLCV verisini ceker ve CSV'ye onbellekler."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_path(symbol, timeframe, days)

    if use_cache and cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600.0
        if age_hours < max_cache_age_hours:
            return pd.read_csv(cache_file, parse_dates=["timestamp"])

    sess = exchange if isinstance(exchange, requests.Session) else create_exchange()
    tf_ms = TIMEFRAME_MS[timeframe]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    end_ms = (now_ms // tf_ms) * tf_ms
    since_ms = end_ms - int(timedelta(days=days).total_seconds() * 1000)

    all_rows = []
    cursor = since_ms
    limit = 1000

    while cursor < end_ms:
        batch = _fetch_klines_rest(symbol, timeframe=timeframe, limit=limit, start_time_ms=cursor, session=sess)
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= cursor:
            break
        cursor = last_ts + tf_ms
        if len(batch) < limit:
            break
        time.sleep(0.15)

    if not all_rows:
        raise RuntimeError(f"Veri cekilemedi: {symbol} {timeframe}")

    df = pd.DataFrame(all_rows, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    df = df[df["timestamp_ms"] < end_ms].drop_duplicates(subset=["timestamp_ms"]).sort_values("timestamp_ms")
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df[["timestamp", "timestamp_ms", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

    df.to_csv(cache_file, index=False)
    return df


def fetch_latest_closed_ohlcv(
    symbol: str,
    timeframe: str = "4h",
    limit: int = 300,
    exchange=None,
) -> tuple[pd.DataFrame, float]:
    """
    Son 'limit' adet KAPANMIS mumu ve anlik (son) fiyati dondurur.
    Kapanmamis son mum sinyal uretimine DAHIL EDILMEZ (repaint onleme).
    """
    sess = exchange if isinstance(exchange, requests.Session) else create_exchange()
    tf_ms = TIMEFRAME_MS[timeframe]
    raw = _fetch_klines_rest(symbol, timeframe=timeframe, limit=limit + 2, session=sess)
    if not raw or len(raw) < 5:
        raise RuntimeError(f"Yetersiz canli veri: {symbol} {timeframe}")

    now_ms = int(time.time() * 1000)
    current_candle_open_ms = (now_ms // tf_ms) * tf_ms
    live_price = float(raw[-1][4])

    df = pd.DataFrame(raw, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    closed_df = df[df["timestamp_ms"] < current_candle_open_ms].copy()
    closed_df["timestamp"] = pd.to_datetime(closed_df["timestamp_ms"], unit="ms", utc=True)
    closed_df = closed_df[["timestamp", "timestamp_ms", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    return closed_df, live_price
