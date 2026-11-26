"""Canli konsol izleyici (salt-okunur).

Buluttaki 7/24 botla cakismadan 5 TRY paritesinin canli inis-cikisini,
indikator/sinyal yakinligini ve acik pozisyonlarin anlik PnL durumunu
eski bottaki gibi konsolda canli akis olarak gosterir.

Kullanim:
    py watch.py
    py watch.py --interval 10
    py watch.py --sync   (acilirken GitHub'dan son kasa durumunu ceker)
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime

import numpy as np

from config import DB_PATH, DEFAULT_SYMBOLS, QUOTE_CURRENCY
from data_loader import create_exchange, fetch_latest_closed_ohlcv, fetch_live_price
from indicators import add_all_indicators
from storage import PaperStorage

# Windows 10/11 PowerShell ANSI renkleri
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
DIM = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _enable_windows_ansi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


def _fetch_24h_stats(session, symbols):
    stats = {}
    for sym in symbols:
        try:
            r = session.get(
                "https://data-api.binance.vision/api/v3/ticker/24hr",
                params={"symbol": sym.replace("/", "").upper()},
                timeout=10,
            )
            r.raise_for_status()
            d = r.json()
            stats[sym] = {
                "price": float(d["lastPrice"]),
                "chg_24h": float(d["priceChangePercent"]),
                "high_24h": float(d["highPrice"]),
                "low_24h": float(d["lowPrice"]),
            }
        except Exception:
            pass
    return stats


def _fetch_indicators_snapshot(session, symbols, timeframe="4h"):
    snap = {}
    for sym in symbols:
        try:
            df, _ = fetch_latest_closed_ohlcv(sym, timeframe=timeframe, limit=250, exchange=session)
            ind = add_all_indicators(df)
            row = ind.iloc[-1]
            snap[sym] = {
                "ema200": float(row["ema_200"]) if not np.isnan(row["ema_200"]) else None,
                "st_dir": int(row["st_dir"]),
                "adx": float(row["adx_14"]) if not np.isnan(row["adx_14"]) else 0.0,
                "rsi": float(row["rsi_14"]) if not np.isnan(row["rsi_14"]) else 0.0,
            }
        except Exception:
            pass
    return snap


def _signal_status(price: float, ind: dict) -> str:
    if not ind or ind.get("ema200") is None:
        return "Veri yok"
    above_ema = price > ind["ema200"]
    st_bull = ind["st_dir"] == 1
    adx_ok = ind["adx"] > 18.0

    if above_ema and st_bull and adx_ok:
        return f"{GREEN}Yukselis Trendi (Aktif){RESET}"
    if above_ema and not st_bull:
        return f"{YELLOW}EMA200 ustu | ST donusu bekleniyor{RESET}"
    if not above_ema and st_bull:
        return f"{DIM}ST yukari ama EMA200 altinda{RESET}"
    return f"{RED}Dusus Trendi (Korumada){RESET}"


def run_watcher(symbols, timeframe: str, interval: int, sync_git: bool, once: bool = False):
    _enable_windows_ansi()
    if sync_git:
        print(f"{CYAN}GitHub'dan son bulut kasa durumu cekiliyor...{RESET}")
        try:
            subprocess.run(["git", "pull", "--rebase"], check=False, timeout=20)
        except Exception:
            pass

    session = create_exchange()
    storage = PaperStorage(db_path=DB_PATH)
    prev_prices = {}
    ind_cache = {}
    last_ind_refresh = 0.0

    print("=" * 92)
    print(f"{BOLD}{CYAN}CANLI PIYASA & SANAL BOT IZLEYICI ({QUOTE_CURRENCY} | {timeframe} supertrend_ema){RESET}")
    print(f"{DIM}Salt-okunur izleme modu (bulut botuyla cakismaz). Cikmak icin Ctrl+C.{RESET}")
    print("=" * 92)

    try:
        while True:
            now_str = datetime.now().strftime("%H:%M:%S")
            now_ts = time.time()

            # Indikatorleri 5 dakikada bir tazele (4h mumda yeterli, cok hizli)
            if now_ts - last_ind_refresh >= 300 or not ind_cache:
                ind_cache = _fetch_indicators_snapshot(session, symbols, timeframe=timeframe)
                last_ind_refresh = now_ts

            stats = _fetch_24h_stats(session, symbols)
            try:
                usdt_try = fetch_live_price("USDT/TRY", session=session)
            except Exception:
                usdt_try = None

            account = storage.get_account()
            positions = storage.get_open_positions()

            print(f"\n{BOLD}[{now_str}] CANLI FIYATLAR & SINYAL RADARI{RESET}" + (f"  {DIM}(USDT/TRY: {usdt_try:.2f}){RESET}" if usdt_try else ""))
            print("-" * 92)

            for sym in symbols:
                if sym not in stats:
                    print(f"  {sym:<9} | Veri alinamadi")
                    continue
                price = stats[sym]["price"]
                chg24 = stats[sym]["chg_24h"]
                prev_p = prev_prices.get(sym)
                prev_prices[sym] = price

                if prev_p is None or price == prev_p:
                    tick = f"{DIM}•{RESET}"
                elif price > prev_p:
                    tick = f"{GREEN}▲{RESET}"
                else:
                    tick = f"{RED}▼{RESET}"

                chg_col = GREEN if chg24 >= 0 else RED
                ind = ind_cache.get(sym, {})
                rsi_v = ind.get("rsi", 0.0)
                adx_v = ind.get("adx", 0.0)
                st_str = f"{GREEN}AL{RESET}" if ind.get("st_dir") == 1 else f"{RED}SAT{RESET}"
                durum = _signal_status(price, ind)

                if sym in positions:
                    p = positions[sym]
                    pnl_pct = ((price - p["entry_price"]) / p["entry_price"]) * 100.0
                    pnl_col = GREEN if pnl_pct >= 0 else RED
                    durum = f"{BOLD}{pnl_col}POZISYON ACIK ({pnl_pct:+.2f}%) | SL: {p['stop_loss']:.2f}{RESET}"

                print(
                    f"  {tick} {BOLD}{sym:<9}{RESET} {price:>12,.2f} {QUOTE_CURRENCY} "
                    f"| 24s: {chg_col}{chg24:+6.2f}%{RESET} "
                    f"| RSI: {rsi_v:4.1f} | ADX: {adx_v:4.1f} | ST: {st_str:<3} | {durum}"
                )

            if account:
                pos_val = sum(
                    p["qty"] * stats.get(s, {}).get("price", p["entry_price"])
                    for s, p in positions.items()
                )
                total_eq = account["cash_usdt"] + pos_val
                init_cap = account["initial_capital"]
                net_pct = ((total_eq - init_cap) / init_cap) * 100.0 if init_cap > 0 else 0.0
                eq_col = GREEN if net_pct >= 0 else RED
                print("-" * 92)
                print(
                    f"  {BOLD}KASA:{RESET} {eq_col}{total_eq:,.2f} {QUOTE_CURRENCY} ({net_pct:+.2f}%){RESET} "
                    f"| Nakit: {account['cash_usdt']:,.2f} {QUOTE_CURRENCY} "
                    f"| Acik Poz: {len(positions)} ({pos_val:,.2f} {QUOTE_CURRENCY})"
                )

            if once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n{CYAN}Canli izleyici kapatildi. Bulut botu arka planda 7/24 calismaya devam ediyor.{RESET}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Canli Konsol Piyasa & Bot Izleyici")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--interval", type=int, default=10, help="Yenileme suresi (saniye)")
    parser.add_argument("--sync", action="store_true", help="Baslangicta GitHub'dan son kasa durumunu cek")
    parser.add_argument("--once", action="store_true", help="Tek seferlik canli goruntu goster ve cik")
    args = parser.parse_args()

    run_watcher(args.symbols, args.timeframe, args.interval, args.sync, args.once)
