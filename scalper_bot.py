import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import time
import ccxt
import pandas as pd

# 1. Render'ın servis açık kalsın diye beklediği sahte web sunucusu
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Mikro-Scalp Botu Aktif ve Calisiyor!")

def start_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# Web sunucusunu arka planda ayrı bir kanalda (thread) başlat
threading.Thread(target=start_server, daemon=True).start()

# 2. Borsa ve Bot Kodları
borsa = ccxt.kraken({'enableRateLimit': True})

PARITE = 'SOL/USDT'
ZAMAN_DILIMI = '15m'

RSI_ALIM_ESIGI = 30
KAR_HEDEFI = 0.020      # %2.0 Kâr
STOP_LOSS = 0.015       # %1.5 Zarar Kes

kasa_tl = 500.0
pozisyonda_mi = False
alis_fiyati = 0.0

print("==========================================")
print(f"🤖 Mikro-Scalp Botu Bulutta Başlatıldı!")
print(f"Takip: {PARITE} | Başlangıç Kasası: {kasa_tl:.2f} TL")
print("==========================================\n")

def rsi_hesapla(seri, periyot=14):
    fark = seri.diff()
    kazanc = fark.clip(lower=0)
    kayip = -1 * fark.clip(upper=0)
    ortalama_kazanc = kazanc.ewm(com=periyot - 1, min_periods=periyot).mean()
    ortalama_kayip = kayip.ewm(com=periyot - 1, min_periods=periyot).mean()
    rs = ortalama_kazanc / ortalama_kayip
    return 100 - (100 / (1 + rs))

def veri_ve_rsi_getir():
    try:
        ohlcv = borsa.fetch_ohlcv(PARITE, timeframe=ZAMAN_DILIMI, limit=100)
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['rsi'] = rsi_hesapla(df['close'], 14)
        guncel_fiyat = float(df['close'].iloc[-1])
        guncel_rsi = float(df['rsi'].iloc[-1])
        return guncel_fiyat, guncel_rsi
    except Exception as e:
        print(f"Veri çekme hatası: {e}")
        return None, None

while True:
    fiyat, rsi = veri_ve_rsi_getir()
    zaman = time.strftime('%H:%M:%S')

    if fiyat is not None and rsi is not None:
        if not pozisyonda_mi:
            if rsi <= RSI_ALIM_ESIGI:
                alis_fiyati = fiyat
                pozisyonda_mi = True
                print(f"\n🚨 ALIM SİNYALİ! RSI: {rsi:.1f}")
                print(f"👉 Alış Fiyatı: {alis_fiyati:.2f}$ | Kasa: {kasa_tl:.2f} TL\n")
            else:
                print(f"[{zaman}] {PARITE}: {fiyat:.2f}$ | RSI: {rsi:.1f} (Beklemede...)")
        else:
            degisim_orani = (fiyat - alis_fiyati) / alis_fiyati

            if degisim_orani >= KAR_HEDEFI:
                kazanc = kasa_tl * degisim_orani
                kasa_tl += kazanc
                pozisyonda_mi = False
                print(f"\n✅ HEDEF GÖRÜLDÜ (+%{degisim_orani*100:.2f})!")
                print(f"💰 Kazanılan: +{kazanc:.2f} TL | Yeni Kasa: {kasa_tl:.2f} TL\n")
            elif degisim_orani <= -STOP_LOSS:
                kayip = kasa_tl * abs(degisim_orani)
                kasa_tl -= kayip
                pozisyonda_mi = False
                print(f"\n🛑 ZARAR KES (-%{abs(degisim_orani)*100:.2f})!")
                print(f"📉 Kayıp: -{kayip:.2f} TL | Kalan Kasa: {kasa_tl:.2f} TL\n")
            else:
                durum_str = f"+%{degisim_orani*100:.2f}" if degisim_orani >= 0 else f"-%{abs(degisim_orani)*100:.2f}"
                print(f"[{zaman}] Pozisyon Açık | Giriş: {alis_fiyati:.2f}$ | Anlık: {fiyat:.2f}$ | Durum: {durum_str}")

    time.sleep(30)
