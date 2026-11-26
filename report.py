"""Terminal ve Markdown (STATUS.md) uzerinden Sanal Borsa durum, piyasa radari ve performans raporu."""

import argparse
from pathlib import Path
from typing import Optional

import numpy as np
from tabulate import tabulate

from config import DB_PATH, DEFAULT_SYMBOLS, QUOTE_CURRENCY
from data_loader import create_exchange, fetch_latest_closed_ohlcv, fetch_live_price
from indicators import add_all_indicators
from storage import PaperStorage


def _build_market_radar(session, symbols, timeframe: str, positions: dict):
    rows = []
    live_prices = {}
    for sym in symbols:
        try:
            df, live_price = fetch_latest_closed_ohlcv(sym, timeframe=timeframe, limit=250, exchange=session)
            live_prices[sym] = live_price
            ind = add_all_indicators(df)
            row = ind.iloc[-1]
            prev_close = float(df["close"].iloc[-1])
            chg_4h = ((live_price - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0

            # 24 saatlik degisim (6 adet 4h mum oncesi)
            p24 = float(df["close"].iloc[-7]) if len(df) >= 7 else prev_close
            chg_24h = ((live_price - p24) / p24) * 100.0 if p24 > 0 else 0.0

            ema200 = float(row["ema_200"]) if not np.isnan(row["ema_200"]) else None
            st_dir = int(row["st_dir"])
            adx_v = float(row["adx_14"]) if not np.isnan(row["adx_14"]) else 0.0
            rsi_v = float(row["rsi_14"]) if not np.isnan(row["rsi_14"]) else 0.0

            yon = "▲" if chg_24h >= 0 else "▼"
            ema_txt = "Ustunde" if (ema200 and live_price > ema200) else "Altinda"
            st_txt = "Yukselis" if st_dir == 1 else "Dusus"

            if sym in positions:
                p = positions[sym]
                pnl_pct = ((live_price - p["entry_price"]) / p["entry_price"]) * 100.0
                durum = f"POZISYON ACIK ({pnl_pct:+.2f}%)"
            elif ema200 and live_price > ema200 and st_dir == 1 and adx_v > 18:
                durum = "Trend Aktif"
            elif ema200 and live_price > ema200 and st_dir == -1:
                durum = "Sinyale Yakin (ST bekleniyor)"
            else:
                durum = "Beklemede (Dusus/Yatay)"

            rows.append(
                {
                    "Parite": sym,
                    f"Anlik ({QUOTE_CURRENCY})": f"{live_price:,.2f}",
                    "4s Degisim": f"{chg_4h:+.2f}%",
                    "24s Degisim": f"{yon} {chg_24h:+.2f}%",
                    "RSI": round(rsi_v, 1),
                    "ADX": round(adx_v, 1),
                    "EMA200": ema_txt,
                    "SuperTrend": st_txt,
                    "Durum": durum,
                }
            )
        except Exception:
            continue
    return rows, live_prices


def build_report(
    db_path: Path,
    fetch_live: bool = True,
    limit: int = 15,
    markdown_out: Optional[Path] = None,
    timeframe: str = "4h",
) -> str:
    storage = PaperStorage(db_path=db_path)
    account = storage.get_account()
    if not account:
        msg = "Henuz kayitli sanal hesap yok. Once 'py paper_trader.py' ile botu baslatin."
        print(msg)
        return msg

    positions = storage.get_open_positions()
    trades = storage.get_closed_trades(limit=500)

    live_prices = {}
    radar_rows = []
    usdt_try_rate: Optional[float] = None
    sess = create_exchange() if fetch_live else None

    if fetch_live and sess is not None:
        if QUOTE_CURRENCY == "TRY":
            try:
                usdt_try_rate = fetch_live_price("USDT/TRY", session=sess)
            except Exception:
                usdt_try_rate = None
        radar_rows, live_prices = _build_market_radar(sess, DEFAULT_SYMBOLS, timeframe, positions)
        for sym in positions:
            if sym not in live_prices:
                try:
                    live_prices[sym] = fetch_live_price(sym, session=sess)
                except Exception:
                    live_prices[sym] = positions[sym]["entry_price"]

    positions_val = sum(
        p["qty"] * live_prices.get(sym, p["entry_price"]) for sym, p in positions.items()
    )
    total_equity = account["cash_usdt"] + positions_val
    init_cap = account["initial_capital"]
    net_pnl = total_equity - init_cap
    net_pnl_pct = (net_pnl / init_cap) * 100.0 if init_cap > 0 else 0.0
    dd_pct = (
        ((account["peak_equity"] - total_equity) / account["peak_equity"]) * 100.0
        if account["peak_equity"] > 0
        else 0.0
    )
    updated_str = account["updated_at"][:19].replace("T", " ") + " UTC"

    usdt_note = ""
    if usdt_try_rate and usdt_try_rate > 0:
        usdt_eq = total_equity / usdt_try_rate
        usdt_note = f" (~{usdt_eq:.2f} USDT, kur: {usdt_try_rate:.2f})"

    summary_rows = [
        ["Baslangic Kasa", f"{init_cap:,.2f} {QUOTE_CURRENCY}"],
        ["Guncel Toplam Kasa", f"{total_equity:,.2f} {QUOTE_CURRENCY} ({net_pnl:+,.2f} {QUOTE_CURRENCY} / {net_pnl_pct:+.2f}%){usdt_note}"],
        ["Serbest Nakit", f"{account['cash_usdt']:,.2f} {QUOTE_CURRENCY}"],
        ["Acik Pozisyon Degeri", f"{positions_val:,.2f} {QUOTE_CURRENCY} ({len(positions)} pozisyon)"],
        ["Zirve Kasa / Anlik DD", f"{account['peak_equity']:,.2f} {QUOTE_CURRENCY} / %{dd_pct:.2f}"],
        ["Gunluk Kill-Switch", "AKTIF (Durduruldu)" if account["daily_halted"] else "Normal"],
        ["Max DD Korumasi", "AKTIF (Durduruldu)" if account["dd_halted"] else "Normal"],
        ["Son Guncelleme", updated_str],
    ]

    pos_rows = []
    for sym, p in positions.items():
        cur = live_prices.get(sym, p["entry_price"])
        unr_val = (p["qty"] * cur) - p["notional_usdt"]
        unr_pct = (unr_val / p["notional_usdt"]) * 100.0 if p["notional_usdt"] > 0 else 0.0
        pos_rows.append(
            {
                "Parite": sym,
                "Strateji": p["strategy"],
                "Giris": round(p["entry_price"], 2),
                "Anlik": round(cur, 2),
                f"Tutar ({QUOTE_CURRENCY})": round(p["notional_usdt"], 2),
                "Anlik PnL": f"{unr_val:+.2f} ({unr_pct:+.2f}%)",
                "Stop-Loss": round(p["stop_loss"], 2) if p["stop_loss"] else "-",
                "Take-Profit": round(p["take_profit"], 2) if p["take_profit"] else "Trailing",
            }
        )

    trade_stats_line = "Henuz tamamlanmis islem yok."
    trade_rows = []
    if trades:
        wins = [t for t in trades if t["pnl_usdt"] > 0]
        losses = [t for t in trades if t["pnl_usdt"] <= 0]
        win_rate = (len(wins) / len(trades)) * 100.0
        gross_p = sum(t["pnl_usdt"] for t in wins)
        gross_l = abs(sum(t["pnl_usdt"] for t in losses))
        pf = (gross_p / gross_l) if gross_l > 0 else (99.0 if gross_p > 0 else 0.0)
        total_fees = sum(t["fees_usdt"] for t in trades)
        trade_stats_line = (
            f"Toplam Islem: **{len(trades)}** | Kazanan: **{len(wins)}** | Kaybeden: **{len(losses)}** | "
            f"Win Rate: **%{win_rate:.1f}** | Profit Factor: **{pf:.2f}** | Komisyon: **{total_fees:,.2f} {QUOTE_CURRENCY}**"
        )
        for t in trades[:limit]:
            trade_rows.append(
                {
                    "Cikis (UTC)": t["exit_time"][:16].replace("T", " "),
                    "Parite": t["symbol"],
                    "Giris": round(t["entry_price"], 2),
                    "Cikis": round(t["exit_price"], 2),
                    f"PnL ({QUOTE_CURRENCY})": f"{t['pnl_usdt']:+.2f}",
                    "PnL %": f"{t['pnl_pct']:+.2f}%",
                    "Neden": t["exit_reason"],
                }
            )

    # Terminal ciktisi
    print("=" * 92)
    print(f"SANAL BORSA (PAPER TRADING) DURUM RAPORU ({QUOTE_CURRENCY})")
    print("=" * 92)
    print(tabulate(summary_rows, tablefmt="simple"))
    if radar_rows:
        print("\n--- PIYASA RADARI (ANLIK INIS-CIKIS & SINYAL DURUMU) ---")
        print(tabulate(radar_rows, headers="keys", tablefmt="github", disable_numparse=True))
    print("\n--- ACIK POZISYONLAR ---")
    print(tabulate(pos_rows, headers="keys", tablefmt="github") if pos_rows else "Acik pozisyon yok.")
    print("\n--- KAPALI ISLEM ISTATISTIKLERI ---")
    print(trade_stats_line.replace("**", ""))
    if trade_rows:
        print(f"\n--- SON {len(trade_rows)} KAPALI ISLEM ---")
        print(tabulate(trade_rows, headers="keys", tablefmt="github"))

    # Markdown ciktisi (GitHub'da telefon/web'den anlik takip icin)
    md_parts = [
        f"# Sanal Borsa (Paper Trading) Canli Durum ({QUOTE_CURRENCY})\n",
        tabulate(summary_rows, headers=["Metrik", "Deger"], tablefmt="github"),
    ]
    if radar_rows:
        md_parts.append("\n\n## Piyasa Radari (Anlik Inis-Cikis & Sinyal Durumu)\n")
        md_parts.append(tabulate(radar_rows, headers="keys", tablefmt="github", disable_numparse=True))
    md_parts.extend(
        [
            "\n\n## Acik Pozisyonlar\n",
            tabulate(pos_rows, headers="keys", tablefmt="github") if pos_rows else "_Acik pozisyon yok._",
            "\n\n## Kapali Islem Istatistikleri\n",
            trade_stats_line,
        ]
    )
    if trade_rows:
        md_parts.append(f"\n\n## Son {len(trade_rows)} Kapali Islem\n")
        md_parts.append(tabulate(trade_rows, headers="keys", tablefmt="github"))
    md_text = "\n".join(md_parts) + "\n"

    if markdown_out is not None:
        markdown_out.write_text(md_text, encoding="utf-8")

    return md_text


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sanal Borsa Durum Raporu")
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    parser.add_argument("--no-live", action="store_true", help="Canli fiyat cekmeden raporla")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--markdown", type=str, default=None, help="Markdown rapor dosyasi (orn. STATUS.md)")
    args = parser.parse_args()

    build_report(
        db_path=Path(args.db),
        fetch_live=not args.no_live,
        limit=args.limit,
        markdown_out=Path(args.markdown) if args.markdown else None,
    )
