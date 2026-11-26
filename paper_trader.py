"""Hafif, VPS/terminal uyumlu Canli Sanal Borsa (Paper Trading) Botu.

Ozellikler:
- Sadece kapanmis mumlardan sinyal uretir (repaint / sahte sinyal yok).
- Acik pozisyonlar icin anlik fiyatla Stop-Loss / Take-Profit takibi yapar.
- Komisyon (%0.10/yon) ve kayma (%0.05/yon) gercekci olarak dusulur.
- Risk bazli pozisyon boyutlandirma, gunluk zarar kesici (kill-switch) ve
  maksimum drawdown korumasina sahiptir.
- Tum durum SQLite + CSV olarak saklanir; yeniden baslatmada sifir veri kaybi.
"""

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict

import numpy as np

from config import (
    DB_PATH,
    DEFAULT_SYMBOLS,
    EQUITY_CSV_PATH,
    ExchangeSimConfig,
    LOGS_DIR,
    PaperBotConfig,
    QUOTE_CURRENCY,
    RiskConfig,
    TRADES_CSV_PATH,
)
from data_loader import create_exchange, fetch_latest_closed_ohlcv
from indicators import add_all_indicators
from storage import PaperStorage
from strategies import STRATEGIES

_STOP_REQUESTED = False


def _handle_signal(signum, frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def setup_logger(log_file: Path) -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("paper_trader")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setFormatter(fmt)
    file_h = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_h.setFormatter(fmt)

    logger.addHandler(stream_h)
    logger.addHandler(file_h)
    return logger


class PaperTradingBot:
    def __init__(self, cfg: PaperBotConfig, storage: PaperStorage, logger: logging.Logger):
        self.cfg = cfg
        self.storage = storage
        self.logger = logger
        self.exchange = create_exchange()
        self.strategy_fn = STRATEGIES[cfg.strategy_name]

    def _close_position(
        self,
        pos: Dict,
        raw_exit_price: float,
        exit_reason: str,
        account: Dict,
    ) -> float:
        ex_cfg = self.cfg.exchange
        fill_exit = raw_exit_price * (1.0 - ex_cfg.slippage_rate)
        gross_proceeds = pos["qty"] * fill_exit
        exit_fee = gross_proceeds * ex_cfg.fee_rate
        net_proceeds = gross_proceeds - exit_fee

        new_cash = account["cash_usdt"] + net_proceeds
        pnl_usdt = net_proceeds - pos["notional_usdt"]
        pnl_pct = (pnl_usdt / pos["notional_usdt"]) * 100.0 if pos["notional_usdt"] > 0 else 0.0
        total_fees = pos["entry_fee_usdt"] + exit_fee

        now_iso = datetime.now(timezone.utc).isoformat()
        self.storage.record_closed_trade(
            {
                "symbol": pos["symbol"],
                "strategy": pos["strategy"],
                "timeframe": pos["timeframe"],
                "entry_time": pos["entry_time"],
                "exit_time": now_iso,
                "entry_price": pos["entry_price"],
                "exit_price": fill_exit,
                "qty": pos["qty"],
                "notional_usdt": pos["notional_usdt"],
                "pnl_usdt": round(pnl_usdt, 6),
                "pnl_pct": round(pnl_pct, 4),
                "fees_usdt": round(total_fees, 6),
                "exit_reason": exit_reason,
                "cash_after": round(new_cash, 6),
            }
        )
        self.storage.delete_open_position(pos["symbol"])

        if pnl_usdt < 0:
            st = self.storage.get_symbol_state(pos["symbol"])
            self.storage.set_symbol_state(
                pos["symbol"],
                st["last_candle_ts_ms"],
                self.cfg.risk.cooldown_candles_after_loss,
            )

        account["cash_usdt"] = new_cash
        tag = "KAR" if pnl_usdt >= 0 else "ZARAR"
        self.logger.info(
            "[CIKIS-%s] %s | Neden: %s | Giris: %.2f -> Cikis: %.2f | PnL: %+.2f %s (%+.2f%%) | Komisyon: %.2f %s | Kasa Nakit: %.2f %s",
            tag,
            pos["symbol"],
            exit_reason,
            pos["entry_price"],
            fill_exit,
            pnl_usdt,
            QUOTE_CURRENCY,
            pnl_pct,
            total_fees,
            QUOTE_CURRENCY,
            new_cash,
            QUOTE_CURRENCY,
        )
        return new_cash

    def _open_position(
        self,
        symbol: str,
        raw_entry_price: float,
        sig,
        total_equity: float,
        account: Dict,
    ) -> bool:
        ex_cfg = self.cfg.exchange
        risk_cfg = self.cfg.risk

        fill_entry = raw_entry_price * (1.0 + ex_cfg.slippage_rate)
        sl = sig.stop_loss
        if sl is not None and sl >= fill_entry:
            sl = fill_entry * 0.985

        per_slot_cap = total_equity * min(risk_cfg.max_position_pct, 1.0 / max(1, risk_cfg.max_open_positions))
        if sl is not None and sl < fill_entry:
            risk_per_unit = fill_entry - sl
            risk_usdt = total_equity * risk_cfg.risk_per_trade_pct
            target_notional = (risk_usdt / risk_per_unit) * fill_entry
            alloc = min(account["cash_usdt"], per_slot_cap, target_notional)
        else:
            alloc = min(account["cash_usdt"], per_slot_cap)

        if alloc < ex_cfg.min_notional:
            self.logger.info(
                "[ATLANDI] %s icin yeterli nakit yok (ayrilan: %.2f %s < %.0f %s)",
                symbol,
                alloc,
                QUOTE_CURRENCY,
                ex_cfg.min_notional,
                QUOTE_CURRENCY,
            )
            return False

        entry_fee = alloc * ex_cfg.fee_rate
        net_buy = alloc - entry_fee
        qty = net_buy / fill_entry
        account["cash_usdt"] -= alloc

        now_iso = datetime.now(timezone.utc).isoformat()
        pos = {
            "symbol": symbol,
            "strategy": self.cfg.strategy_name,
            "timeframe": self.cfg.timeframe,
            "entry_time": now_iso,
            "entry_price": fill_entry,
            "qty": qty,
            "notional_usdt": alloc,
            "entry_fee_usdt": entry_fee,
            "stop_loss": sl,
            "take_profit": sig.take_profit,
            "trailing_atr_mult": sig.trailing_atr_mult,
            "highest_price": fill_entry,
            "entry_reason": sig.reason,
        }
        self.storage.save_open_position(pos)
        self.logger.info(
            "[ALIM] %s | Fiyat: %.2f | Tutar: %.2f %s | SL: %s | TP: %s | Neden: %s",
            symbol,
            fill_entry,
            alloc,
            QUOTE_CURRENCY,
            f"{sl:.2f}" if sl else "-",
            f"{sig.take_profit:.2f}" if sig.take_profit else "Trailing/Signal",
            sig.reason,
        )
        return True

    def run_cycle(self) -> None:
        account = self.storage.ensure_account(self.cfg.exchange.initial_capital)
        positions = self.storage.get_open_positions()
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        live_prices: Dict[str, float] = {}
        closed_frames = {}

        # 1) Tum pariteler icin son kapanmis mumlar ve anlik fiyatlari cek
        for sym in self.cfg.symbols:
            try:
                closed_df, live_price = fetch_latest_closed_ohlcv(
                    sym, timeframe=self.cfg.timeframe, limit=260, exchange=self.exchange
                )
                live_prices[sym] = live_price
                closed_frames[sym] = closed_df
            except Exception as e:
                self.logger.warning("[VERI HATASI] %s verisi alinamadi: %s", sym, e)

        if not live_prices:
            self.logger.warning("Hicbir pariteden veri alinamadi, sonraki dongu bekleniyor.")
            return

        # Gun degistiyse gunluk zarar kesiciyi sifirla
        open_val_now = sum(pos["qty"] * live_prices.get(sym, pos["entry_price"]) for sym, pos in positions.items())
        total_equity = account["cash_usdt"] + open_val_now

        if account["day_utc"] != today_utc:
            account["day_utc"] = today_utc
            account["day_start_equity"] = total_equity
            account["daily_halted"] = 0
            self.logger.info("[YENI GUN UTC] %s | Gun basi kasa: %.2f %s", today_utc, total_equity, QUOTE_CURRENCY)

        # 2) Acik pozisyonlar icin ANLIK SL / TP kontrolu
        for sym in list(positions.keys()):
            if sym not in live_prices:
                continue
            pos = positions[sym]
            price = live_prices[sym]
            sl = pos["stop_loss"]
            tp = pos["take_profit"]

            if sl is not None and price <= sl:
                self._close_position(pos, price, "Stop-Loss (Anlik)", account)
                del positions[sym]
            elif tp is not None and price >= tp:
                self._close_position(pos, price, "Take-Profit (Anlik)", account)
                del positions[sym]

        # 3) Kapanmis yeni mum varsa indikator/sinyal ve trailing stop degerlendir
        for sym in self.cfg.symbols:
            if sym not in closed_frames:
                continue
            df_closed = closed_frames[sym]
            if len(df_closed) < 210:
                self.logger.warning("[YETERSIZ VERI] %s icin %d mum var (en az 210 gerekli)", sym, len(df_closed))
                continue

            last_closed_ts_ms = int(df_closed["timestamp_ms"].iloc[-1])
            sym_state = self.storage.get_symbol_state(sym)
            is_new_candle = last_closed_ts_ms > sym_state["last_candle_ts_ms"]
            cooldown = sym_state["cooldown_candles"]

            if not is_new_candle:
                continue

            # Ilk acilista gecmis kapanmis muma aninda giris yapma; sadece referans olarak kaydet
            if sym_state["last_candle_ts_ms"] == 0:
                self.storage.set_symbol_state(sym, last_closed_ts_ms, 0)
                self.logger.info(
                    "[SENKRON] %s | Son kapanmis %s mumu kaydedildi (%s) | Anlik: %.2f %s",
                    sym,
                    self.cfg.timeframe,
                    df_closed["timestamp"].iloc[-1].strftime("%Y-%m-%d %H:%M UTC"),
                    live_prices[sym],
                    QUOTE_CURRENCY,
                )
                continue

            df_ind = add_all_indicators(df_closed)
            row = df_ind.iloc[-1]
            prev = df_ind.iloc[-2]
            in_pos = sym in positions

            # Acik pozisyon varsa trailing stop guncelle
            if in_pos:
                pos = positions[sym]
                if float(row["high"]) > pos["highest_price"]:
                    pos["highest_price"] = float(row["high"])
                mult = pos["trailing_atr_mult"]
                atr_val = float(row["atr_14"])
                if mult is not None and not np.isnan(atr_val):
                    new_sl = pos["highest_price"] - mult * atr_val
                    if pos["stop_loss"] is None or new_sl > pos["stop_loss"]:
                        pos["stop_loss"] = new_sl
                        self.logger.info("[TRAILING SL] %s yeni stop: %.2f %s", sym, new_sl, QUOTE_CURRENCY)
                self.storage.save_open_position(pos)

            sig = self.strategy_fn(row, prev, in_pos)

            if in_pos and sig.action == "SELL":
                self._close_position(positions[sym], live_prices[sym], sig.reason or "Signal Exit", account)
                del positions[sym]
            elif (not in_pos) and sig.action == "BUY":
                blocked_reasons = []
                if account["daily_halted"]:
                    blocked_reasons.append("gunluk zarar limiti")
                if account["dd_halted"]:
                    blocked_reasons.append("max drawdown korumasi")
                if cooldown > 0:
                    blocked_reasons.append(f"cooldown ({cooldown} mum)")
                if len(positions) >= self.cfg.risk.max_open_positions:
                    blocked_reasons.append(f"max acik pozisyon ({self.cfg.risk.max_open_positions})")

                if blocked_reasons:
                    self.logger.info(
                        "[SINYAL ENGELLENDI] %s ALIM (%s) -> Neden: %s",
                        sym,
                        sig.reason,
                        ", ".join(blocked_reasons),
                    )
                else:
                    open_val_cur = sum(
                        p["qty"] * live_prices.get(s, p["entry_price"]) for s, p in positions.items()
                    )
                    eq_cur = account["cash_usdt"] + open_val_cur
                    if self._open_position(sym, live_prices[sym], sig, eq_cur, account):
                        positions = self.storage.get_open_positions()

            if cooldown > 0 and sym not in positions:
                cooldown -= 1

            self.storage.set_symbol_state(sym, last_closed_ts_ms, cooldown)

        # 4) Kasa, Peak Equity ve Kill-Switch guncelleme
        positions_val = sum(
            pos["qty"] * live_prices.get(sym, pos["entry_price"]) for sym, pos in positions.items()
        )
        total_equity = account["cash_usdt"] + positions_val
        peak_equity = max(account["peak_equity"], total_equity)

        daily_loss_pct = (
            (account["day_start_equity"] - total_equity) / account["day_start_equity"]
            if account["day_start_equity"] > 0
            else 0.0
        )
        dd_pct = (peak_equity - total_equity) / peak_equity if peak_equity > 0 else 0.0

        daily_halted = bool(account["daily_halted"]) or (daily_loss_pct >= self.cfg.risk.daily_max_loss_pct)
        dd_halted = bool(account["dd_halted"]) or (dd_pct >= self.cfg.risk.max_drawdown_pause_pct)

        if daily_halted and not account["daily_halted"]:
            self.logger.warning(
                "[KILL-SWITCH] Gunluk kayip %%%.2f esigini asti! Yeni alimlar gun sonuna kadar durduruldu.",
                daily_loss_pct * 100.0,
            )
        if dd_halted and not account["dd_halted"]:
            self.logger.warning(
                "[MAX DD KORUMASI] Zirveden dusus %%%.2f! Bot yeni alimlari durdurdu.",
                dd_pct * 100.0,
            )

        self.storage.update_account(
            cash_usdt=account["cash_usdt"],
            peak_equity=peak_equity,
            day_utc=account["day_utc"],
            day_start_equity=account["day_start_equity"],
            daily_halted=daily_halted,
            dd_halted=dd_halted,
        )
        self.storage.record_equity_snapshot(account["cash_usdt"], positions_val, len(positions))

        net_pnl_pct = ((total_equity - account["initial_capital"]) / account["initial_capital"]) * 100.0
        self.logger.info(
            "[DURUM] Kasa: %.2f %s (%+.2f%%) | Nakit: %.2f %s | Acik Poz: %d (%.2f %s) | DD: %.2f%%",
            total_equity,
            QUOTE_CURRENCY,
            net_pnl_pct,
            account["cash_usdt"],
            QUOTE_CURRENCY,
            len(positions),
            positions_val,
            QUOTE_CURRENCY,
            dd_pct * 100.0,
        )

    def run_forever(self) -> None:
        self.logger.info("=" * 75)
        self.logger.info(
            "SANAL BORSA (PAPER TRADING) BOTU BASLATILDI | Strateji: %s | TF: %s",
            self.cfg.strategy_name,
            self.cfg.timeframe,
        )
        self.logger.info(
            "Pariteler: %s | Kontrol Araligi: %dsn | Risk/Islem: %%%.1f | Gunluk Max Zarar: %%%.1f",
            ", ".join(self.cfg.symbols),
            self.cfg.poll_interval_seconds,
            self.cfg.risk.risk_per_trade_pct * 100.0,
            self.cfg.risk.daily_max_loss_pct * 100.0,
        )
        self.logger.info("=" * 75)

        while not _STOP_REQUESTED:
            try:
                self.run_cycle()
            except Exception as e:
                self.logger.exception("Dongu sirasinda beklenmeyen hata: %s", e)

            for _ in range(self.cfg.poll_interval_seconds):
                if _STOP_REQUESTED:
                    break
                time.sleep(1)

        self.logger.info("Bot guvenli sekilde durduruldu. Tum veriler SQLite veritabaninda korundu.")


def main():
    parser = argparse.ArgumentParser(description="Canli Sanal Borsa (Paper Trading) Botu")
    parser.add_argument("--strategy", choices=list(STRATEGIES.keys()), default="supertrend_ema")
    parser.add_argument("--timeframe", choices=["15m", "1h", "4h"], default="4h")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--capital", type=float, default=5000.0, help=f"Baslangic sanal kasa ({QUOTE_CURRENCY})")
    parser.add_argument("--interval", type=int, default=60, help="Kontrol araligi (saniye)")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="SQLite veritabani yolu")
    parser.add_argument("--once", action="store_true", help="Tek kontrol dongusu calistir ve cik")
    parser.add_argument("--reset", action="store_true", help="Sanal kasayi ve gecmisi sifirla")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger = setup_logger(LOGS_DIR / "bot.log")
    storage = PaperStorage(db_path=Path(args.db), trades_csv=TRADES_CSV_PATH, equity_csv=EQUITY_CSV_PATH)

    if args.reset:
        storage.reset_account(args.capital)
        logger.info("Sanal hesap %.2f %s ile sifirlandi.", args.capital, QUOTE_CURRENCY)

    cfg = PaperBotConfig(
        strategy_name=args.strategy,
        symbols=args.symbols,
        timeframe=args.timeframe,
        poll_interval_seconds=args.interval,
        exchange=ExchangeSimConfig(initial_capital=args.capital),
        risk=RiskConfig(),
    )

    bot = PaperTradingBot(cfg=cfg, storage=storage, logger=logger)
    if args.once:
        bot.run_cycle()
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
