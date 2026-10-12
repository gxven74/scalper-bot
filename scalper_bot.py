import time
import ccxt
import pandas as pd

# Borsa bağlantısı
borsa = ccxt.binance({'enableRateLimit': True})

PARITE = 'SOL/USDT'
ZAMAN_DILIMI = '15m'

# Strateji Parametreleri
RSI_ALIM_ESIGI = 30
KAR_HEDEFI = 0.020      # %2.0 Kâr Al
STOP_LOSS = 0.015       # %1.5 Zararı Kes

# Sanal Kasa
kasa_tl = 500.0
pozisyonda_mi = False
alis_fiyati = 0.0

print("==========================================")
print(f"🤖 Mikro-Scalp Botu Başlatıldı!")
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
    
    if fiyat is not None and rsi is not None:
        zaman_damgasi = time.strftime('%H:%M:%S')
        
        if not pozisyonda_mi:
            print(f"[{zaman_damgasi}] {PARITE}: {fiyat:.2f}$ | RSI: {rsi:.1f} (Beklemede...)")
            
            if rsi <= RSI_ALIM_ESIGI:
                alis_fiyati = fiyat
                pozisyonda_mi = True
                print(f"\n🚨 ALIM SİNYALİ! RSI: {rsi:.1f}")
                print(f"👉 Alış Fiyatı: {alis_fiyati:.2f}$ | Kasa: {kasa_tl:.2f} TL\n")
        
        else:
            kar_orani = (fiyat - alis_fiyati) / alis_fiyati
            print(f"[{zaman_damgasi}] Pozisyon Açık | Giriş: {alis_fiyati:.2f}$ | Anlık: {fiyat:.2f}$ | Durum: %{kar_orani * 100:+.2f}")
            
            if kar_orani >= KAR_HEDEFI:
                kazanc = kasa_tl * kar_orani
                kasa_tl += kazanc
                pozisyonda_mi = False
                print(f"\n✅ HEDEF GÖRÜLDÜ (+%{kar_orani*100:.2f})!")
                print(f"💰 Kazanılan: +{kazanc:.2f} TL | Yeni Kasa: {kasa_tl:.2f} TL\n")
            
            elif kar_orani <= -STOP_LOSS:
                kayip = kasa_tl * abs(kar_orani)
                kasa_tl -= kayip
                pozisyonda_mi = False
                print(f"\n🛑 ZARAR KES ÇALIŞTI (%{kar_orani*100:.2f})!")
                print(f"⚠️ Kayıp: -{kayip:.2f} TL | Kalan Kasa: {kasa_tl:.2f} TL\n")

    time.sleep(30)