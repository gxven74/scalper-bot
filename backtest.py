"""Gercekci (sifir lookahead, komisyon + slippage + risk yonetimi) backtest motoru."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import ExchangeSimConfig, RiskConfig
from indicators import add_all_indicators
from strategies import STRATEGIES, Signal


@dataclass
class TradeRecord:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    notional_usdt: float
    pnl_usdt: float
    pnl_pct: float
    fees_usdt: float
    exit_reason: str


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    timeframe: str
    initial_capital: float
    final_equity: float
    total_return_pct: float
    buy_and_hold_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    sharpe_ratio: float
    num_trades: int
    total_fees_usdt: float
    avg_trade_pct: float
    trades: List[TradeRecord] = field(default_factory=list)
    equity_curve: Optional[pd.Series] = None


def _compute_metrics(
    strategy: str,
    symbol: str,
    timeframe: str,
    initial_capital: float,
    equity_series: pd.Series,
    trades: List[TradeRecord],
    buy_and_hold_pct: float,
    periods_per_year: float,
) -> BacktestResult:
    final_equity = float(equity_series.iloc[-1]) if len(equity_series) else initial_capital
    total_return_pct = ((final_equity - initial_capital) / initial_capital) * 100.0

    running_max = equity_series.cummax()
    dd = (equity_series - running_max) / running_max.replace(0, np.nan)
    max_dd_pct = float(abs(dd.min()) * 100.0) if len(dd) else 0.0

    num_trades = len(trades)
    if num_trades > 0:
        wins = [t for t in trades if t.pnl_usdt > 0]
        losses = [t for t in trades if t.pnl_usdt <= 0]
        win_rate = (len(wins) / num_trades) * 100.0
        gross_profit = sum(t.pnl_usdt for t in wins)
        gross_loss = abs(sum(t.pnl_usdt for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
        total_fees = sum(t.fees_usdt for t in trades)
        avg_trade_pct = float(np.mean([t.pnl_pct for t in trades]))
    else:
        win_rate = 0.0
        profit_factor = 0.0
        total_fees = 0.0
        avg_trade_pct = 0.0

    returns = equity_series.pct_change().dropna()
    if len(returns) > 2 and returns.std() > 0:
        sharpe = float((returns.mean() / returns.std()) * np.sqrt(periods_per_year))
    else:
        sharpe = 0.0

    return BacktestResult(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        initial_capital=initial_capital,
        final_equity=final_equity,
        total_return_pct=total_return_pct,
        buy_and_hold_pct=buy_and_hold_pct,
        max_drawdown_pct=max_dd_pct,
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        sharpe_ratio=sharpe,
        num_trades=num_trades,
        total_fees_usdt=total_fees,
        avg_trade_pct=avg_trade_pct,
        trades=trades,
        equity_curve=equity_series,
    )


PERIODS_PER_YEAR = {
    "15m": 365 * 24 * 4,
    "1h": 365 * 24,
    "4h": 365 * 6,
    "1d": 365,
}


def run_single_backtest(
    df_raw: pd.DataFrame,
    strategy_name: str,
    symbol: str,
    timeframe: str,
    ex_cfg: Optional[ExchangeSimConfig] = None,
    risk_cfg: Optional[RiskConfig] = None,
    use_risk_sizing: bool = True,
) -> BacktestResult:
    """Tek parite uzerinde sifir lookahead bias ile bar-by-bar backtest calistirir."""
    ex_cfg = ex_cfg or ExchangeSimConfig()
    risk_cfg = risk_cfg or RiskConfig()
    strat_fn = STRATEGIES[strategy_name]

    df = add_all_indicators(df_raw)
    n = len(df)
    if n < 220:
        raise ValueError(f"Yetersiz mum sayisi ({n}) - en az 220 mum gerekli.")

    # Hizli dongu icin satir sozluklerine cevir (pandas iloc/loc yavasligini onler)
    records = df.to_dict("records")

    # Ilk 205 mum isinma (EMA200 vb.), test 205. mumdan baslar
    start_idx = 205
    buy_hold_pct = ((records[-1]["close"] - records[start_idx]["open"]) / records[start_idx]["open"]) * 100.0

    cash = ex_cfg.initial_capital_usdt
    qty = 0.0
    in_pos = False
    entry_price = 0.0
    entry_time = None
    entry_notional = 0.0
    entry_fee = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_atr_mult: Optional[float] = None
    highest_since_entry = 0.0

    cooldown_remaining = 0
    pending_entry: Optional[Signal] = None
    pending_exit_reason: Optional[str] = None

    trades: List[TradeRecord] = []
    equity_vals = []
    equity_idx = []

    current_day = None
    day_start_equity = cash
    daily_halted = False

    for i in range(start_idx, n):
        row = records[i]
        prev = records[i - 1]
        ts = row["timestamp"]
        day = ts.date()

        if current_day != day:
            current_day = day
            day_start_equity = cash + (qty * row["open"] if in_pos else 0.0)
            daily_halted = False

        # 1) Onceki mumun kapanisinda uretilen CIKIS emri bu mumun acilisinda gerceklesir
        if in_pos and pending_exit_reason is not None:
            fill_exit = row["open"] * (1.0 - ex_cfg.slippage_rate)
            gross_proceeds = qty * fill_exit
            exit_fee = gross_proceeds * ex_cfg.fee_rate
            net_proceeds = gross_proceeds - exit_fee
            cash += net_proceeds

            pnl_usdt = net_proceeds - entry_notional
            pnl_pct = (pnl_usdt / entry_notional) * 100.0 if entry_notional > 0 else 0.0
            trades.append(
                TradeRecord(
                    symbol=symbol,
                    entry_time=entry_time,
                    exit_time=ts,
                    entry_price=entry_price,
                    exit_price=fill_exit,
                    qty=qty,
                    notional_usdt=entry_notional,
                    pnl_usdt=pnl_usdt,
                    pnl_pct=pnl_pct,
                    fees_usdt=entry_fee + exit_fee,
                    exit_reason=pending_exit_reason,
                )
            )
            if pnl_usdt < 0:
                cooldown_remaining = risk_cfg.cooldown_candles_after_loss
            in_pos = False
            qty = 0.0
            pending_exit_reason = None

        # 2) Onceki mumun kapanisinda uretilen GIRIS emri bu mumun acilisinda gerceklesir
        if (not in_pos) and pending_entry is not None and not daily_halted and cooldown_remaining == 0:
            fill_entry = row["open"] * (1.0 + ex_cfg.slippage_rate)
            # Stop-loss'u giris fiyatina gore guncelle (gap durumunda emniyet)
            sl = pending_entry.stop_loss
            tp = pending_entry.take_profit
            if sl is not None and sl >= fill_entry:
                sl = fill_entry * 0.985

            # Pozisyon boyutlandirma (risk bazli)
            if use_risk_sizing and sl is not None and sl < fill_entry:
                risk_per_unit = fill_entry - sl
                risk_usdt = cash * risk_cfg.risk_per_trade_pct
                target_notional = (risk_usdt / risk_per_unit) * fill_entry
                alloc_usdt = min(cash * risk_cfg.max_position_pct, target_notional)
            else:
                alloc_usdt = cash * risk_cfg.max_position_pct

            if alloc_usdt >= 5.0:  # Minimum 5 USDT Binance spot emir esigi
                entry_fee = alloc_usdt * ex_cfg.fee_rate
                net_buy_usdt = alloc_usdt - entry_fee
                qty = net_buy_usdt / fill_entry
                cash -= alloc_usdt
                entry_price = fill_entry
                entry_time = ts
                entry_notional = alloc_usdt
                stop_loss = sl
                take_profit = tp
                trailing_atr_mult = pending_entry.trailing_atr_mult
                highest_since_entry = fill_entry
                in_pos = True

        pending_entry = None

        # 3) Acik pozisyon varsa mum ici SL / TP kontrolu (muhafazakar: once SL kontrol edilir!)
        if in_pos:
            exited_intrabar = False
            exit_fill = None
            exit_reason = ""

            if stop_loss is not None and row["low"] <= stop_loss:
                # Gap acilisi stop altindaysa open'dan, degilse stop fiyatindan slippage ile cik
                trigger_p = min(row["open"], stop_loss)
                exit_fill = trigger_p * (1.0 - ex_cfg.slippage_rate)
                exit_reason = "Stop-Loss"
                exited_intrabar = True
            elif take_profit is not None and row["high"] >= take_profit:
                trigger_p = max(row["open"], take_profit)
                exit_fill = trigger_p * (1.0 - ex_cfg.slippage_rate)
                exit_reason = "Take-Profit"
                exited_intrabar = True

            if exited_intrabar and exit_fill is not None:
                gross_proceeds = qty * exit_fill
                exit_fee = gross_proceeds * ex_cfg.fee_rate
                net_proceeds = gross_proceeds - exit_fee
                cash += net_proceeds

                pnl_usdt = net_proceeds - entry_notional
                pnl_pct = (pnl_usdt / entry_notional) * 100.0 if entry_notional > 0 else 0.0
                trades.append(
                    TradeRecord(
                        symbol=symbol,
                        entry_time=entry_time,
                        exit_time=ts,
                        entry_price=entry_price,
                        exit_price=exit_fill,
                        qty=qty,
                        notional_usdt=entry_notional,
                        pnl_usdt=pnl_usdt,
                        pnl_pct=pnl_pct,
                        fees_usdt=entry_fee + exit_fee,
                        exit_reason=exit_reason,
                    )
                )
                if pnl_usdt < 0:
                    cooldown_remaining = risk_cfg.cooldown_candles_after_loss
                in_pos = False
                qty = 0.0
            else:
                # Mum kapandiktan sonra iz suren stop guncellemesi (sonraki mum icin)
                if row["high"] > highest_since_entry:
                    highest_since_entry = row["high"]
                if trailing_atr_mult is not None and not np.isnan(row["atr_14"]):
                    new_sl = highest_since_entry - trailing_atr_mult * row["atr_14"]
                    if stop_loss is None or new_sl > stop_loss:
                        stop_loss = new_sl

        # Gunluk zarar limiti kontrolu
        mtm_equity = cash + (qty * row["close"] if in_pos else 0.0)
        equity_vals.append(mtm_equity)
        equity_idx.append(ts)

        if day_start_equity > 0 and (day_start_equity - mtm_equity) / day_start_equity >= risk_cfg.daily_max_loss_pct:
            daily_halted = True

        # 4) Kapanan mumun verisiyle bir sonraki mum icin sinyal uret
        if i < n - 1:
            sig = strat_fn(row, prev, in_pos)
            if in_pos and sig.action == "SELL":
                pending_exit_reason = sig.reason or "Signal Exit"
            elif (not in_pos) and sig.action == "BUY" and not daily_halted and cooldown_remaining == 0:
                pending_entry = sig

        if cooldown_remaining > 0 and not in_pos:
            cooldown_remaining -= 1

    # Test sonunda acik pozisyon varsa son kapanistan kapat (raporlama netligi icin)
    if in_pos:
        last_row = records[-1]
        exit_fill = last_row["close"] * (1.0 - ex_cfg.slippage_rate)
        gross_proceeds = qty * exit_fill
        exit_fee = gross_proceeds * ex_cfg.fee_rate
        net_proceeds = gross_proceeds - exit_fee
        cash += net_proceeds
        pnl_usdt = net_proceeds - entry_notional
        pnl_pct = (pnl_usdt / entry_notional) * 100.0 if entry_notional > 0 else 0.0
        trades.append(
            TradeRecord(
                symbol=symbol,
                entry_time=entry_time,
                exit_time=last_row["timestamp"],
                entry_price=entry_price,
                exit_price=exit_fill,
                qty=qty,
                notional_usdt=entry_notional,
                pnl_usdt=pnl_usdt,
                pnl_pct=pnl_pct,
                fees_usdt=entry_fee + exit_fee,
                exit_reason="End-of-Test",
            )
        )
        equity_vals[-1] = cash

    eq_series = pd.Series(equity_vals, index=pd.DatetimeIndex(equity_idx))
    return _compute_metrics(
        strategy=strategy_name,
        symbol=symbol,
        timeframe=timeframe,
        initial_capital=ex_cfg.initial_capital_usdt,
        equity_series=eq_series,
        trades=trades,
        buy_and_hold_pct=buy_hold_pct,
        periods_per_year=PERIODS_PER_YEAR.get(timeframe, 365 * 24),
    )


def run_portfolio_backtest(
    data_by_symbol: Dict[str, pd.DataFrame],
    strategy_name: str,
    timeframe: str,
    ex_cfg: Optional[ExchangeSimConfig] = None,
    risk_cfg: Optional[RiskConfig] = None,
) -> BacktestResult:
    """
    Coklu paritede ortak USDT kasasiyla (kartopu / bilesik getiri) portfoy backtesti.
    Maksimum acik pozisyon, islem basi risk ve gunluk zarar kesici dahildir.
    """
    ex_cfg = ex_cfg or ExchangeSimConfig()
    risk_cfg = risk_cfg or RiskConfig()
    strat_fn = STRATEGIES[strategy_name]

    prepared: Dict[str, pd.DataFrame] = {}
    for sym, raw_df in data_by_symbol.items():
        d = add_all_indicators(raw_df).set_index("timestamp")
        prepared[sym] = d

    # Ortak zaman ekseni (ilk 205 mum isinma)
    common_index = None
    for d in prepared.values():
        idx = d.index[205:]
        common_index = idx if common_index is None else common_index.intersection(idx)
    common_index = common_index.sort_values()

    if len(common_index) < 50:
        raise ValueError("Ortak zaman araliginda yeterli mum yok.")

    # Hizli erisim icin her sembolu liste + ts->index haritasina cevir
    records_by_sym: Dict[str, List[dict]] = {}
    pos_maps: Dict[str, Dict[pd.Timestamp, int]] = {}
    for sym, d in prepared.items():
        recs = d.reset_index().to_dict("records")
        records_by_sym[sym] = recs
        pos_maps[sym] = {r["timestamp"]: idx for idx, r in enumerate(recs)}

    # Esit agirlikli Buy & Hold karsilastirmasi
    bh_returns = []
    for sym in prepared:
        r0 = records_by_sym[sym][pos_maps[sym][common_index[0]]]
        r1 = records_by_sym[sym][pos_maps[sym][common_index[-1]]]
        bh_returns.append(((r1["close"] - r0["open"]) / r0["open"]) * 100.0)
    avg_bh_pct = float(np.mean(bh_returns))

    cash = ex_cfg.initial_capital_usdt
    positions: Dict[str, dict] = {}
    pending_entries: Dict[str, Signal] = {}
    pending_exits: Dict[str, str] = {}
    cooldowns: Dict[str, int] = {s: 0 for s in prepared}

    trades: List[TradeRecord] = []
    equity_vals = []

    current_day = None
    day_start_equity = cash
    daily_halted = False

    for step_idx, ts in enumerate(common_index):
        day = ts.date()
        cur_rows = {sym: records_by_sym[sym][pos_maps[sym][ts]] for sym in prepared}

        # Gun basi kasa hesabi
        if current_day != day:
            current_day = day
            open_val = sum(pos["qty"] * cur_rows[sym]["open"] for sym, pos in positions.items())
            day_start_equity = cash + open_val
            daily_halted = False

        # 1) Bekleyen cikislar (mum acilisi)
        for sym in list(pending_exits.keys()):
            if sym in positions:
                pos = positions.pop(sym)
                open_p = cur_rows[sym]["open"]
                exit_fill = open_p * (1.0 - ex_cfg.slippage_rate)
                gross = pos["qty"] * exit_fill
                exit_fee = gross * ex_cfg.fee_rate
                net = gross - exit_fee
                cash += net
                pnl_usdt = net - pos["notional"]
                pnl_pct = (pnl_usdt / pos["notional"]) * 100.0
                trades.append(
                    TradeRecord(
                        symbol=sym,
                        entry_time=pos["entry_time"],
                        exit_time=ts,
                        entry_price=pos["entry_price"],
                        exit_price=exit_fill,
                        qty=pos["qty"],
                        notional_usdt=pos["notional"],
                        pnl_usdt=pnl_usdt,
                        pnl_pct=pnl_pct,
                        fees_usdt=pos["entry_fee"] + exit_fee,
                        exit_reason=pending_exits[sym],
                    )
                )
                if pnl_usdt < 0:
                    cooldowns[sym] = risk_cfg.cooldown_candles_after_loss
        pending_exits.clear()

        # 2) Bekleyen girisler (mum acilisi)
        total_equity_open = cash + sum(
            pos["qty"] * cur_rows[sym]["open"] for sym, pos in positions.items()
        )
        per_slot_cap = total_equity_open * min(risk_cfg.max_position_pct, 1.0 / max(1, risk_cfg.max_open_positions))

        for sym, sig in list(pending_entries.items()):
            if daily_halted or sym in positions or cooldowns[sym] > 0:
                continue
            if len(positions) >= risk_cfg.max_open_positions:
                break

            open_p = cur_rows[sym]["open"]
            fill_entry = open_p * (1.0 + ex_cfg.slippage_rate)
            sl = sig.stop_loss
            if sl is not None and sl >= fill_entry:
                sl = fill_entry * 0.985

            if sl is not None and sl < fill_entry:
                risk_per_unit = fill_entry - sl
                risk_usdt = total_equity_open * risk_cfg.risk_per_trade_pct
                target_notional = (risk_usdt / risk_per_unit) * fill_entry
                alloc = min(cash, per_slot_cap, target_notional)
            else:
                alloc = min(cash, per_slot_cap)

            if alloc >= 5.0:
                entry_fee = alloc * ex_cfg.fee_rate
                net_buy = alloc - entry_fee
                qty = net_buy / fill_entry
                cash -= alloc
                positions[sym] = {
                    "qty": qty,
                    "entry_price": fill_entry,
                    "entry_time": ts,
                    "notional": alloc,
                    "entry_fee": entry_fee,
                    "stop_loss": sl,
                    "take_profit": sig.take_profit,
                    "trailing_atr_mult": sig.trailing_atr_mult,
                    "highest": fill_entry,
                }
        pending_entries.clear()

        # 3) Mum ici SL / TP ve kapanis sonrasi trailing stop guncelleme
        for sym in list(positions.keys()):
            pos = positions[sym]
            row = cur_rows[sym]
            sl = pos["stop_loss"]
            tp = pos["take_profit"]

            exit_fill = None
            exit_reason = ""
            if sl is not None and row["low"] <= sl:
                trigger_p = min(row["open"], sl)
                exit_fill = trigger_p * (1.0 - ex_cfg.slippage_rate)
                exit_reason = "Stop-Loss"
            elif tp is not None and row["high"] >= tp:
                trigger_p = max(row["open"], tp)
                exit_fill = trigger_p * (1.0 - ex_cfg.slippage_rate)
                exit_reason = "Take-Profit"

            if exit_fill is not None:
                gross = pos["qty"] * exit_fill
                exit_fee = gross * ex_cfg.fee_rate
                net = gross - exit_fee
                cash += net
                pnl_usdt = net - pos["notional"]
                pnl_pct = (pnl_usdt / pos["notional"]) * 100.0
                trades.append(
                    TradeRecord(
                        symbol=sym,
                        entry_time=pos["entry_time"],
                        exit_time=ts,
                        entry_price=pos["entry_price"],
                        exit_price=exit_fill,
                        qty=pos["qty"],
                        notional_usdt=pos["notional"],
                        pnl_usdt=pnl_usdt,
                        pnl_pct=pnl_pct,
                        fees_usdt=pos["entry_fee"] + exit_fee,
                        exit_reason=exit_reason,
                    )
                )
                if pnl_usdt < 0:
                    cooldowns[sym] = risk_cfg.cooldown_candles_after_loss
                del positions[sym]
            else:
                if row["high"] > pos["highest"]:
                    pos["highest"] = row["high"]
                mult = pos["trailing_atr_mult"]
                atr_val = row["atr_14"]
                if mult is not None and not np.isnan(atr_val):
                    new_sl = pos["highest"] - mult * atr_val
                    if pos["stop_loss"] is None or new_sl > pos["stop_loss"]:
                        pos["stop_loss"] = new_sl

        # Kapanis MTM kasa
        mtm_equity = cash + sum(
            pos["qty"] * cur_rows[sym]["close"] for sym, pos in positions.items()
        )
        equity_vals.append(mtm_equity)

        if day_start_equity > 0 and (day_start_equity - mtm_equity) / day_start_equity >= risk_cfg.daily_max_loss_pct:
            daily_halted = True

        # 4) Sonraki mum icin sinyal uretimi
        if step_idx < len(common_index) - 1:
            for sym in prepared:
                i_loc = pos_maps[sym][ts]
                row = records_by_sym[sym][i_loc]
                prev = records_by_sym[sym][i_loc - 1]
                in_pos = sym in positions
                sig = strat_fn(row, prev, in_pos)
                if in_pos and sig.action == "SELL":
                    pending_exits[sym] = sig.reason or "Signal Exit"
                elif (not in_pos) and sig.action == "BUY" and not daily_halted and cooldowns[sym] == 0:
                    pending_entries[sym] = sig

        for sym in cooldowns:
            if cooldowns[sym] > 0 and sym not in positions:
                cooldowns[sym] -= 1

    # Kapanista acik kalanlari kapat
    last_ts = common_index[-1]
    for sym in list(positions.keys()):
        pos = positions.pop(sym)
        close_p = records_by_sym[sym][pos_maps[sym][last_ts]]["close"]
        exit_fill = close_p * (1.0 - ex_cfg.slippage_rate)
        gross = pos["qty"] * exit_fill
        exit_fee = gross * ex_cfg.fee_rate
        net = gross - exit_fee
        cash += net
        pnl_usdt = net - pos["notional"]
        pnl_pct = (pnl_usdt / pos["notional"]) * 100.0
        trades.append(
            TradeRecord(
                symbol=sym,
                entry_time=pos["entry_time"],
                exit_time=last_ts,
                entry_price=pos["entry_price"],
                exit_price=exit_fill,
                qty=pos["qty"],
                notional_usdt=pos["notional"],
                pnl_usdt=pnl_usdt,
                pnl_pct=pnl_pct,
                fees_usdt=pos["entry_fee"] + exit_fee,
                exit_reason="End-of-Test",
            )
        )
    equity_vals[-1] = cash

    eq_series = pd.Series(equity_vals, index=common_index)
    return _compute_metrics(
        strategy=strategy_name,
        symbol="+".join(s.split("/")[0] for s in prepared.keys()),
        timeframe=timeframe,
        initial_capital=ex_cfg.initial_capital_usdt,
        equity_series=eq_series,
        trades=trades,
        buy_and_hold_pct=avg_bh_pct,
        periods_per_year=PERIODS_PER_YEAR.get(timeframe, 365 * 24),
    )
