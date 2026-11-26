"""Coklu parite, coklu zaman dilimi ve coklu strateji backtest karsilastirma araci."""

import argparse
from typing import Dict, List

import pandas as pd
from tabulate import tabulate

from backtest import run_portfolio_backtest, run_single_backtest
from config import DEFAULT_SYMBOLS, ExchangeSimConfig, LOGS_DIR, QUOTE_CURRENCY, RiskConfig
from data_loader import create_exchange, fetch_historical_ohlcv
from strategies import STRATEGIES


def run_benchmark(symbols: List[str], timeframes: List[str], days_1h_4h: int = 365, days_15m: int = 180):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ex = create_exchange()
    ex_cfg = ExchangeSimConfig()
    risk_cfg = RiskConfig()

    print("=" * 85)
    print(f"BINANCE GECMIS VERI BACKTEST KARSILASTIRMASI ({QUOTE_CURRENCY})")
    print(f"Baslangic Kasa: {ex_cfg.initial_capital:.0f} {QUOTE_CURRENCY} | Komisyon: %{ex_cfg.fee_rate*100:.2f}/yon | Slippage: %{ex_cfg.slippage_rate*100:.2f}/yon")
    print(f"Islem Basi Risk: %{risk_cfg.risk_per_trade_pct*100:.1f} | Gunluk Max Zarar: %{risk_cfg.daily_max_loss_pct*100:.1f}")
    print("=" * 85)

    single_rows = []
    portfolio_rows = []
    data_cache: Dict[str, Dict[str, pd.DataFrame]] = {}

    for tf in timeframes:
        days = days_15m if tf == "15m" else days_1h_4h
        print(f"\n[Veri yukleniyor] Zaman dilimi: {tf} (son {days} gun)...")
        data_cache[tf] = {}
        for sym in symbols:
            df = fetch_historical_ohlcv(sym, timeframe=tf, days=days, use_cache=True, exchange=ex)
            data_cache[tf][sym] = df
            print(f"  - {sym:<10}: {len(df):>6} mum ({df['timestamp'].iloc[0].strftime('%Y-%m-%d')} -> {df['timestamp'].iloc[-1].strftime('%Y-%m-%d')})")

        # Portfoy (5 parite ortak kasa) backtestleri
        for strat_name in STRATEGIES:
            res = run_portfolio_backtest(
                data_by_symbol=data_cache[tf],
                strategy_name=strat_name,
                timeframe=tf,
                ex_cfg=ex_cfg,
                risk_cfg=risk_cfg,
            )
            # In-sample (ilk %50) ve Out-of-sample (son %50) ayrimi
            half_data_1 = {}
            half_data_2 = {}
            for sym, df in data_cache[tf].items():
                mid = len(df) // 2
                half_data_1[sym] = df.iloc[:mid].reset_index(drop=True)
                # OOS icin 205 mumluk indikator isinma payi birak
                half_data_2[sym] = df.iloc[max(0, mid - 205):].reset_index(drop=True)

            oos_ret = None
            is_ret = None
            try:
                r1 = run_portfolio_backtest(half_data_1, strat_name, tf, ex_cfg, risk_cfg)
                r2 = run_portfolio_backtest(half_data_2, strat_name, tf, ex_cfg, risk_cfg)
                is_ret = r1.total_return_pct
                oos_ret = r2.total_return_pct
            except Exception:
                pass

            portfolio_rows.append(
                {
                    "TF": tf,
                    "Strateji": strat_name,
                    "Getiri %": round(res.total_return_pct, 2),
                    "1.Yari %": round(is_ret, 2) if is_ret is not None else None,
                    "2.Yari(OOS) %": round(oos_ret, 2) if oos_ret is not None else None,
                    "B&H %": round(res.buy_and_hold_pct, 2),
                    "MaxDD %": round(res.max_drawdown_pct, 2),
                    "WinRate %": round(res.win_rate_pct, 1),
                    "PF": round(res.profit_factor, 2),
                    "Sharpe": round(res.sharpe_ratio, 2),
                    "Islem": res.num_trades,
                    f"Komisyon {QUOTE_CURRENCY}": round(res.total_fees_usdt, 2),
                    f"Son Kasa {QUOTE_CURRENCY}": round(res.final_equity, 2),
                }
            )

            # Tek parite sonuclari
            for sym in symbols:
                use_risk = strat_name != "baseline_rsi"
                sres = run_single_backtest(
                    df_raw=data_cache[tf][sym],
                    strategy_name=strat_name,
                    symbol=sym,
                    timeframe=tf,
                    ex_cfg=ex_cfg,
                    risk_cfg=risk_cfg,
                    use_risk_sizing=use_risk,
                )
                single_rows.append(
                    {
                        "TF": tf,
                        "Parite": sym,
                        "Strateji": strat_name,
                        "Getiri %": round(sres.total_return_pct, 2),
                        "B&H %": round(sres.buy_and_hold_pct, 2),
                        "MaxDD %": round(sres.max_drawdown_pct, 2),
                        "WinRate %": round(sres.win_rate_pct, 1),
                        "PF": round(sres.profit_factor, 2),
                        "Sharpe": round(sres.sharpe_ratio, 2),
                        "Islem": sres.num_trades,
                        f"Komisyon {QUOTE_CURRENCY}": round(sres.total_fees_usdt, 2),
                        f"Son Kasa {QUOTE_CURRENCY}": round(sres.final_equity, 2),
                    }
                )

    port_df = pd.DataFrame(portfolio_rows).sort_values(by=["Sharpe", "Getiri %"], ascending=False)
    single_df = pd.DataFrame(single_rows).sort_values(by=["Sharpe", "Getiri %"], ascending=False)

    port_df.to_csv(LOGS_DIR / "backtest_portfolio_summary.csv", index=False)
    single_df.to_csv(LOGS_DIR / "backtest_single_summary.csv", index=False)

    print("\n" + "=" * 85)
    print(f"1) COKLU PARITE PORTFOY SONUCLARI (Ortak {ex_cfg.initial_capital:.0f} {QUOTE_CURRENCY} Kasa, Max 3 Acik Pozisyon)")
    print("=" * 85)
    print(tabulate(port_df, headers="keys", tablefmt="github", showindex=False))

    print("\n" + "=" * 85)
    print("2) MEVCUT ESKI BOT (baseline_rsi) GERCEK PERFORMANSI")
    print("=" * 85)
    base_df = single_df[single_df["Strateji"] == "baseline_rsi"].sort_values(["TF", "Getiri %"], ascending=[True, False])
    print(tabulate(base_df, headers="keys", tablefmt="github", showindex=False))

    print("\n" + "=" * 85)
    print("3) TEK PARITE EN IYI 15 KOMBINASYON (Sharpe & Getiri Sirali)")
    print("=" * 85)
    print(tabulate(single_df.head(15), headers="keys", tablefmt="github", showindex=False))

    print(f"\nDetayli CSV raporlari kaydedildi: {LOGS_DIR}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scalper Backtest Karsilastirma Araci")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--timeframes", nargs="+", default=["15m", "1h", "4h"])
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--days-15m", type=int, default=180)
    args = parser.parse_args()

    run_benchmark(
        symbols=args.symbols,
        timeframes=args.timeframes,
        days_1h_4h=args.days,
        days_15m=args.days_15m,
    )
