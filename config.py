"""Merkezi yapilandirma dosyasi."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "paper_trading.db"
TRADES_CSV_PATH = LOGS_DIR / "paper_trades.csv"
EQUITY_CSV_PATH = LOGS_DIR / "equity_curve.csv"

# Varsayilan test pariteleri (yuksek likidite, dusuk spread)
DEFAULT_SYMBOLS: List[str] = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
]

# Test edilecek zaman dilimleri
DEFAULT_TIMEFRAMES: List[str] = ["15m", "1h", "4h"]


@dataclass
class ExchangeSimConfig:
    """Gercekci borsa maliyet ve emir simulasyon parametreleri."""
    initial_capital_usdt: float = 100.0     # Baslangic sanal kasa (USDT)
    fee_rate: float = 0.001                 # Binance Spot standart %0.10 (tek yon)
    slippage_rate: float = 0.0005           # %0.05 tahmini kayma (tek yon)


@dataclass
class RiskConfig:
    """Dusuk zarar marjli risk yonetimi ayarlari."""
    risk_per_trade_pct: float = 0.015       # Islem basi maksimum kasa riski (%1.5)
    max_position_pct: float = 0.95          # Tek isleme ayrilabilecek maksimum kasa orani (tek parite)
    max_open_positions: int = 3             # Coklu paritede ayni anda max acik pozisyon
    daily_max_loss_pct: float = 0.04        # Gunluk %4 kayipta yeni islem durdurulur (kill-switch)
    max_drawdown_pause_pct: float = 0.12    # Zirveden %12 dususte bot korumaya gecer
    cooldown_candles_after_loss: int = 2    # Stop-loss sonrasi beklenecek mum sayisi


@dataclass
class PaperBotConfig:
    """Canli sanal (paper trading) bot ayarlari."""
    strategy_name: str = "supertrend_ema"
    symbols: List[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    timeframe: str = "4h"
    poll_interval_seconds: int = 60
    exchange: ExchangeSimConfig = field(default_factory=ExchangeSimConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
