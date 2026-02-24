# TraderBot İnceleme Raporu

**Tarih:** 25 Şubat 2025  
**Kapsam:** Kod yapısı, fee hesaplamaları, risk, strateji, iyileştirme önerileri

---

## Genel Puan: **7.2 / 10**

Bot profesyonel bir yapıda, çok sayıda gösterge ve risk katmanı içeriyor. Fee tarafında tespit edilen hatalar düzeltildiğinde ve önerilen iyileştirmeler yapıldığında **8.5+** seviyesine çıkabilir.

---

## 1. Güçlü Yönler

### Mimari ve Kod Kalitesi
- **Tek config kaynağı:** Pydantic `Settings` ile `.env`, tüm parametreler merkezi.
- **Paper / Live ayrımı:** Aynı execution interface, scheduler moddan bağımsız.
- **Backtest gerçek stratejiyi kullanıyor:** `Strategy.generate_signals()` ve `RiskManager` backtest’te de kullanılıyor; sonuçlar canlıya daha yakın.
- **SQLite ile kalıcılık:** Pozisyonlar, emirler, fill’ler, risk state log’u tutuluyor.
- **Testler:** `test_backtest.py`, `test_risk.py`, `test_execution.py` vb. ile kritik akışlar test edilmiş.

### Strateji
- **22+ gösterge ile confluence:** EMA z-score, RSI, MACD, Bollinger, ADX, orderbook, VWAP, StochRSI, OBV, Williams %R, TTM Squeeze, OI, whale, liquidation cascade, volume profile, sentiment vb.
- **Mod bilinçli:** Mean reversion / trend / breakout; scalp + swing ayrı parametreler.
- **Funding penceresi, korelasyon filtresi, momentum çıkışı, zamanla sıkılaşan SL** gibi ek filtreler mevcut.

### Risk Yönetimi
- **NORMAL / TIGHT / ULTRA_TIGHT:** “Ses düğmesi” mantığı; tamamen kapatma yok.
- **Zaman tabanlı toparlanma:** Uzun süre TIGHT/ULTRA’da kalınca otomatik gevşeme.
- **Kelly + volatilite boyutlandırma:** İsteğe bağlı Kelly ve ATR ile pozisyon büyüklüğü.
- **Anti-likidasyon:** Likidasyon fiyatına belirli yüzde yaklaşınca pozisyon kapatma.

### Trade Yönetimi
- **Dinamik TP/SL (ATR), trailing stop, breakeven stop, kısmi TP** destekleniyor.
- **Momentum çıkışı:** MACD sıfırı ters yönde geçince erken çıkış.

---

## 2. Fee Hesaplamaları – Tespit Edilen Sorunlar ve Düzeltmeler

### Backtest
- **Doğru:** Girişte `capital -= entry_fee`, çıkışta `pnl -= exit_fee`. Round-trip fee’ler doğru yansıyor.

### Paper Mod (Düzeltildi)
- **Sorun:** Sadece **çıkış fee’si** PnL’den düşülüyordu; **giriş fee’si** sadece `fills` tablosuna yazılıyordu, `realised_pnl`’e etkisi yoktu. Bu da kârı **her işlemde bir giriş fee’si kadar** şişiriyordu.
- **Düzeltme:** Hem tam kapanışta hem kısmi TP’de:
  - Giriş fee (kapanan miktar için): `entry_price * qty * (fee_rate_bps / 10_000)`
  - Çıkış fee: `exit_price * qty * (fee_rate_bps / 10_000)`
  - `realised_pnl = brüt PnL - giriş fee - çıkış fee`

### Live Mod (Düzeltildi)
- **Sorun:** Hiçbir kapanış yolunda (exchange_close, anti_liquidation, momentum_exit, timeout) fee düşülmüyordu. `realised_pnl` tamamen **brüt** (fee’siz) hesaplanıyordu.
- **Düzeltme:** Tüm kapanış path’lerinde:
  - `entry_fee = entry * qty * (fee_rate_bps / 10_000)`
  - `exit_fee = exit_price * qty * (fee_rate_bps / 10_000)`
  - `realised_pnl = brüt PnL - entry_fee - exit_fee`

### Fee Oranı
- **Varsayılan 4 bps (0.04%):** BingX perpetual’da taker genelde ~0.05%, maker ~0.02%. Market emirleri taker sayıldığı için 4 bps makul (biraz iyimser). İstersen `.env` ile `FEE_RATE_BPS=5` yapabilirsin.

---

## 3. Eksikler / Dikkat Edilmesi Gerekenler

1. **Live’da fill kaydı:** Gerçek piyasa emri doldurulduğunda `fills` tablosuna satır eklenmiyor; sadece paper’da var. İleride komisyon raporu veya exchange ile mutabakat için live fill’leri de yazmak faydalı olur.
2. **Maker / Taker ayrımı:** Şu an tek oran var. TP/SL limit ile kapatılıyorsa maker oranı kullanılabilir; şimdilik tek oranla devam edilebilir.
3. **Slippage (backtest):** Backtest’te spread sabit 5 bps, gerçek piyasa daha oynak olabilir. Özellikle düşük likidite sembollerde backtest biraz iyimser kalabilir.

---

## 4. Kazanç İçin İyileştirme Önerileri

### Strateji / Sinyal
- **Confluence eşiğini veriyle ayarla:** Backtest optimizer ile `min_confluence_score`, `min_weighted_score_no_ema` ve TP/SL bps değerlerini dönem dönem optimize et.
- **Sembol seçimi:** Yüksek spread’li veya düşük hacimli coin’leri selector’da daha sert elersen gereksiz kayıplar azalır.
- **Swing ağırlığı:** Uzun vadede swing pozisyonları daha az işlem maliyeti ve daha geniş TP ile kâr getirebilir; `swing_max_positions` ve swing sinyal eşiklerini test et.

### Risk ve Boyutlandırma
- **Kelly ve volatilite sizing:** Zaten açık; `kelly_min_trades` dolana kadar yarı-Kelly ile devam, sonra Kelly çıktısına göre boyut ayarı iyi bir denge.
- **Drawdown limitleri:** `risk_drawdown_pct_tight` / `ultra` değerlerini sermayene göre sıkılaştırabilirsin; küçük hesaplarda %8–18 makul, büyük hesaplarda daha konservatif olabilir.

### Teknik
- **Backtest’te partial TP:** Şu an backtest’te kısmi TP simülasyonu yok; eklenirse sonuçlar live’a daha yakın olur.
- **Funding penceresi:** `avoid_funding_window` ve `funding_window_minutes` ile funding saatlerinden kaçınma zaten var; volatiliteyi azaltır.

---

## 5. Özet Tablo

| Kriter              | Puan (10) | Not |
|----------------------|-----------|-----|
| Mimari / kod kalitesi| 8.0       | Modüler, config merkezi, testler var |
| Strateji zenginliği  | 8.5       | 22+ gösterge, confluence, mod bilinci |
| Risk yönetimi        | 7.5       | 3 seviye, Kelly, anti-likidasyon |
| Fee doğruluğu        | 6.0 → 8.5*| *Paper/Live düzeltmeleri sonrası |
| Backtest / uyum      | 7.5       | Aynı strateji/risk, fee doğru |
| Dokümantasyon / ops  | 6.5       | README, .env.example var; detay artırılabilir |

**Ortalama (fee düzeltmesi sonrası): ~7.8–8.0 / 10.**

---

## 6. Sonuç

Bot production’a yakın, fee hataları düzeltildikten sonra hem paper hem live PnL’i gerçeğe daha yakın olacak. Strateji ve risk katmanları güçlü; parametre optimizasyonu ve backtest’e kısmi TP eklenmesi ile daha yüksek ve tutarlı kazanç hedeflenebilir.
