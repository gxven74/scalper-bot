"""Terminal ve Markdown (STATUS.md) uzerinden Sanal Borsa durum ve performans raporu."""

import argparse
from pathlib import Path
from typing import Optional

from tabulate import tabulate

from config import DB_PATH
from data_loader import create_exchange, fetch_live_price
from storage import PaperStorage


def build_report(
    db_path: Path,
    fetch_live: bool = True,
    limit: int = 15,
    markdown_out: Optional[Path] = None,
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
    if fetch_live and positions:
        sess = create_exchange()
        for sym in positions:
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

    summary_rows = [
        ["Baslangic Kasa", f"{init_cap:.2f} USDT"],
        ["Guncel Toplam Kasa", f"{total_equity:.2f} USDT ({net_pnl:+.2f} USDT / {net_pnl_pct:+.2f}%)"],
        ["Serbest Nakit", f"{account['cash_usdt']:.2f} USDT"],
        ["Acik Pozisyon Degeri", f"{positions_val:.2f} USDT ({len(positions)} pozisyon)"],
        ["Zirve Kasa / Anlik DD", f"{account['peak_equity']:.2f} USDT / %{dd_pct:.2f}"],
        ["Gunluk Kill-Switch", "AKTIF (Durduruldu)" if account["daily_halted"] else "Normal"],
        ["Max DD Korumasi", "AKTIF (Durduruldu)" if account["dd_halted"] else "Normal"],
        ["Son Guncelleme", updated_str],
    ]

    pos_rows = []
    for sym, p in positions.items():
        cur = live_prices.get(sym, p["entry_price"])
        unr_usdt = (p["qty"] * cur) - p["notional_usdt"]
        unr_pct = (unr_usdt / p["notional_usdt"]) * 100.0 if p["notional_usdt"] > 0 else 0.0
        pos_rows.append(
            {
                "Parite": sym,
                "Strateji": p["strategy"],
                "Giris": round(p["entry_price"], 4),
                "Anlik": round(cur, 4),
                "Tutar (USDT)": round(p["notional_usdt"], 2),
                "Anlik PnL": f"{unr_usdt:+.2f} ({unr_pct:+.2f}%)",
                "Stop-Loss": round(p["stop_loss"], 4) if p["stop_loss"] else "-",
                "Take-Profit": round(p["take_profit"], 4) if p["take_profit"] else "Trailing",
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
            f"Win Rate: **%{win_rate:.1f}** | Profit Factor: **{pf:.2f}** | Komisyon: **{total_fees:.2f} USDT**"
        )
        for t in trades[:limit]:
            trade_rows.append(
                {
                    "Cikis (UTC)": t["exit_time"][:16].replace("T", " "),
                    "Parite": t["symbol"],
                    "Giris": round(t["entry_price"], 4),
                    "Cikis": round(t["exit_price"], 4),
                    "PnL (USDT)": f"{t['pnl_usdt']:+.2f}",
                    "PnL %": f"{t['pnl_pct']:+.2f}%",
                    "Neden": t["exit_reason"],
                }
            )

    # Terminal ciktisi
    print("=" * 78)
    print("SANAL BORSA (PAPER TRADING) DURUM RAPORU")
    print("=" * 78)
    print(tabulate(summary_rows, tablefmt="simple"))
    print("\n--- ACIK POZISYONLAR ---")
    print(tabulate(pos_rows, headers="keys", tablefmt="github") if pos_rows else "Acik pozisyon yok.")
    print("\n--- KAPALI ISLEM ISTATISTIKLERI ---")
    print(trade_stats_line.replace("**", ""))
    if trade_rows:
        print(f"\n--- SON {len(trade_rows)} KAPALI ISLEM ---")
        print(tabulate(trade_rows, headers="keys", tablefmt="github"))

    # Markdown ciktisi (GitHub'da telefon/web'den anlik takip icin)
    md_parts = [
        "# Sanal Borsa (Paper Trading) Canli Durum\n",
        tabulate(summary_rows, headers=["Metrik", "Deger"], tablefmt="github"),
        "\n\n## Acik Pozisyonlar\n",
        tabulate(pos_rows, headers="keys", tablefmt="github") if pos_rows else "_Acik pozisyon yok._",
        "\n\n## Kapali Islem Istatistikleri\n",
        trade_stats_line,
    ]
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
