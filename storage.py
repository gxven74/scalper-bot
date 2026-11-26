"""Sanal borsa (Paper Trading) icin SQLite + CSV kalici veri katmani.

Bot kapansa veya VPS yeniden baslasa bile kasa, acik pozisyonlar,
gunluk zarar limiti durumu ve islem gecmisi eksiksiz geri yuklenir.
"""

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import DB_PATH, EQUITY_CSV_PATH, LOGS_DIR, TRADES_CSV_PATH


class PaperStorage:
    def __init__(
        self,
        db_path: Path = DB_PATH,
        trades_csv: Path = TRADES_CSV_PATH,
        equity_csv: Path = EQUITY_CSV_PATH,
    ):
        self.db_path = Path(db_path)
        self.trades_csv = Path(trades_csv)
        self.equity_csv = Path(equity_csv)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS account_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initial_capital REAL NOT NULL,
                    cash_usdt REAL NOT NULL,
                    peak_equity REAL NOT NULL,
                    day_utc TEXT NOT NULL,
                    day_start_equity REAL NOT NULL,
                    daily_halted INTEGER NOT NULL DEFAULT 0,
                    dd_halted INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS open_positions (
                    symbol TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    qty REAL NOT NULL,
                    notional_usdt REAL NOT NULL,
                    entry_fee_usdt REAL NOT NULL,
                    stop_loss REAL,
                    take_profit REAL,
                    trailing_atr_mult REAL,
                    highest_price REAL NOT NULL,
                    entry_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS symbol_state (
                    symbol TEXT PRIMARY KEY,
                    last_candle_ts_ms INTEGER NOT NULL DEFAULT 0,
                    cooldown_candles INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS closed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    qty REAL NOT NULL,
                    notional_usdt REAL NOT NULL,
                    pnl_usdt REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    fees_usdt REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    cash_after REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    cash_usdt REAL NOT NULL,
                    positions_value_usdt REAL NOT NULL,
                    total_equity_usdt REAL NOT NULL,
                    open_positions_count INTEGER NOT NULL
                );
                """
            )

    def ensure_account(self, initial_capital: float) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM account_state WHERE id = 1").fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO account_state
                    (id, initial_capital, cash_usdt, peak_equity, day_utc, day_start_equity, daily_halted, dd_halted, updated_at)
                    VALUES (1, ?, ?, ?, ?, ?, 0, 0, ?)
                    """,
                    (initial_capital, initial_capital, initial_capital, today, initial_capital, now.isoformat()),
                )
                row = conn.execute("SELECT * FROM account_state WHERE id = 1").fetchone()
            return dict(row)

    def get_account(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM account_state WHERE id = 1").fetchone()
            return dict(row) if row else None

    def update_account(
        self,
        cash_usdt: float,
        peak_equity: float,
        day_utc: str,
        day_start_equity: float,
        daily_halted: bool,
        dd_halted: bool,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE account_state
                SET cash_usdt = ?, peak_equity = ?, day_utc = ?, day_start_equity = ?,
                    daily_halted = ?, dd_halted = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    cash_usdt,
                    peak_equity,
                    day_utc,
                    day_start_equity,
                    1 if daily_halted else 0,
                    1 if dd_halted else 0,
                    now_iso,
                ),
            )

    def get_open_positions(self) -> Dict[str, Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM open_positions").fetchall()
            return {r["symbol"]: dict(r) for r in rows}

    def save_open_position(self, pos: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO open_positions
                (symbol, strategy, timeframe, entry_time, entry_price, qty, notional_usdt,
                 entry_fee_usdt, stop_loss, take_profit, trailing_atr_mult, highest_price, entry_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos["symbol"],
                    pos["strategy"],
                    pos["timeframe"],
                    pos["entry_time"],
                    pos["entry_price"],
                    pos["qty"],
                    pos["notional_usdt"],
                    pos["entry_fee_usdt"],
                    pos["stop_loss"],
                    pos["take_profit"],
                    pos["trailing_atr_mult"],
                    pos["highest_price"],
                    pos.get("entry_reason", ""),
                ),
            )

    def delete_open_position(self, symbol: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM open_positions WHERE symbol = ?", (symbol,))

    def get_symbol_state(self, symbol: str) -> Dict[str, int]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM symbol_state WHERE symbol = ?", (symbol,)).fetchone()
            if row is None:
                return {"last_candle_ts_ms": 0, "cooldown_candles": 0}
            return {"last_candle_ts_ms": row["last_candle_ts_ms"], "cooldown_candles": row["cooldown_candles"]}

    def set_symbol_state(self, symbol: str, last_candle_ts_ms: int, cooldown_candles: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO symbol_state (symbol, last_candle_ts_ms, cooldown_candles)
                VALUES (?, ?, ?)
                """,
                (symbol, last_candle_ts_ms, cooldown_candles),
            )

    def record_closed_trade(self, trade: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO closed_trades
                (symbol, strategy, timeframe, entry_time, exit_time, entry_price, exit_price,
                 qty, notional_usdt, pnl_usdt, pnl_pct, fees_usdt, exit_reason, cash_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade["symbol"],
                    trade["strategy"],
                    trade["timeframe"],
                    trade["entry_time"],
                    trade["exit_time"],
                    trade["entry_price"],
                    trade["exit_price"],
                    trade["qty"],
                    trade["notional_usdt"],
                    trade["pnl_usdt"],
                    trade["pnl_pct"],
                    trade["fees_usdt"],
                    trade["exit_reason"],
                    trade["cash_after"],
                ),
            )

        # CSV log dosyasina da ekle (Excel / VPS inceleme kolayligi)
        file_exists = self.trades_csv.exists()
        fieldnames = [
            "symbol", "strategy", "timeframe", "entry_time", "exit_time",
            "entry_price", "exit_price", "qty", "notional_usdt",
            "pnl_usdt", "pnl_pct", "fees_usdt", "exit_reason", "cash_after",
        ]
        with self.trades_csv.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({k: trade[k] for k in fieldnames})

    def record_equity_snapshot(
        self,
        cash_usdt: float,
        positions_value_usdt: float,
        open_positions_count: int,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        total = cash_usdt + positions_value_usdt
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO equity_snapshots
                (timestamp, cash_usdt, positions_value_usdt, total_equity_usdt, open_positions_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (now_iso, cash_usdt, positions_value_usdt, total, open_positions_count),
            )

        file_exists = self.equity_csv.exists()
        with self.equity_csv.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "cash_usdt", "positions_value_usdt", "total_equity_usdt", "open_positions_count"])
            writer.writerow([now_iso, round(cash_usdt, 4), round(positions_value_usdt, 4), round(total, 4), open_positions_count])

    def get_closed_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM closed_trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def reset_account(self, initial_capital: float) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                DELETE FROM account_state;
                DELETE FROM open_positions;
                DELETE FROM symbol_state;
                DELETE FROM closed_trades;
                DELETE FROM equity_snapshots;
                """
            )
        if self.trades_csv.exists():
            self.trades_csv.unlink()
        if self.equity_csv.exists():
            self.equity_csv.unlink()
        self.ensure_account(initial_capital)
