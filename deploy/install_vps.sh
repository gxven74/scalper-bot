#!/usr/bin/env bash
# Ubuntu / Debian (orn. Oracle Cloud Always Free - Frankfurt/Amsterdam) icin
# 7/24 otomatik baslayan systemd servisi kurulum scripti.
# Kullanim: bash deploy/install_vps.sh

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(whoami)"

echo "[1/4] Python venv ve bagimliliklar kuruluyor..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "[2/4] systemd servis dosyasi olusturuluyor..."
SERVICE_FILE="/etc/systemd/system/scalper-paper-bot.service"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Scalper Paper Trading Bot (24/7)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/paper_trader.py --strategy supertrend_ema --timeframe 4h --capital 100 --interval 60
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

echo "[3/4] Servis etkinlestiriliyor ve baslatiliyor..."
sudo systemctl daemon-reload
sudo systemctl enable scalper-paper-bot
sudo systemctl restart scalper-paper-bot

echo "[4/4] Tamamlandi! Canli loglari gormek icin:"
echo "  journalctl -u scalper-paper-bot -f"
echo "Durum raporu icin:"
echo "  ${APP_DIR}/.venv/bin/python ${APP_DIR}/report.py"
