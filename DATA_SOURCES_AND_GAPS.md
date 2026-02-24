# Veri Kaynakları ve Eksikler – Bot İyileştirme Önerileri

**Amaç:** Agresiflik değiştirilmeden, piyasada yaygın kullanılan ama botta eksik/eksik kullanılan kritik verileri ve nereden ücretsiz çekilebileceğini özetlemek.

---

## 1. Şu An BingX’ten Çektiğimiz Veriler

| Veri | BingX endpoint | Botta kullanım |
|------|-----------------|-----------------|
| Sözleşme listesi | `/quote/contracts` | Universe, step_size, tick_size |
| 24h ticker | `/quote/ticker` | Son fiyat (fallback) |
| Order book (depth) | `/quote/depth` | Spread, bid/ask, whale duvarı, imbalance |
| Klines (OHLCV) | `/quote/klines` | EMA, RSI, MACD, BB, ATR, OBV, VWAP, StochRSI, ADX, taker proxy, vb. |
| Mark price | `/quote/premiumIndex` | Mark fiyat, çıkış kontrolü |
| **Funding rate** | `/quote/premiumIndex` (lastFundingRate) | Funding filtresi, contrarian bonus, sentiment |
| **Open interest** | `/quote/openInterest` | OI oyu, liquidation cascade tespiti |

Bunlar doğru ve yerinde kullanılıyor; agresiflik ayarı değiştirilmeden aynen kalabilir.

---

## 2. Piyasada Yaygın Ama Botta Eksik / Zayıf Olan Veriler

### 2.1 Taker buy/sell oranı (gerçek)

- **Ne:** Taker alım hacmi / toplam hacim (veya taker alım / taker satım). Kısa vadeli alım/satım baskısını doğrudan gösterir.
- **Botta durum:** Sadece **proxy** var: mum yönü × hacim (yeşil = alım, kırmızı = satım). Gerçek taker verisi BingX’ten çekilmiyor.
- **BingX:** Swap v2 dokümantasyonunda tek sembol için “recent trades” veya “taker volume” endpoint’i net değil; varsa eklenebilir.
- **Ücretsiz dış kaynak:** Binance Futures **public** (API key yok):
  - `GET https://fapi.binance.com/futures/data/takerlongshortRatio`
  - Symbol (örn. BTCUSDT), period (5m, 15m, 1h…), limit.
  - BTC/ETH gibi ana çiftler için piyasa baskısı proxy’si olarak kullanılabilir (BingX fiyatı Binance’e yakın).

### 2.2 Long/short hesap oranı (top trader veya global)

- **Ne:** Hesapların veya büyük hesapların net long/short dağılımı. Aşırı tek taraflı pozisyon = tersine dönüş sinyali olarak kullanılır.
- **Botta durum:** Yok.
- **BingX:** Resmi dokümanda standart long/short ratio endpoint’i görünmüyor.
- **Ücretsiz dış kaynak:** Binance Futures **public**:
  - Global: `GET /futures/data/globalLongShortAccountRatio` (symbol, period, limit)
  - Top trader: `GET /futures/data/topLongShortAccountRatio`
  - API key gerekmez; rate limit cömert (örn. 1000/5dk).

### 2.3 Funding rate geçmişi

- **Ne:** Son birkaç funding döneminin oranları (trend: yükseliyor mu, aşırı pozitif/negatif mi).
- **Botta durum:** Sadece **anlık** funding (`premiumIndex.lastFundingRate`). Geçmiş seri yok.
- **BingX:** Dokümanda “funding rate history” endpoint’i net değil; varsa eklenmeli.
- **Ücretsiz alternatif:** Binance: `GET /fapi/v1/fundingRate` (symbol, limit, startTime, endTime) – public.

### 2.4 Açık pozisyon likidasyonları (liquidations)

- **Ne:** Son X dakikada/saatte gerçekleşen long/short likidasyon hacimleri veya sayıları. Kaskad riski ve piyasa temizliği için kullanılır.
- **Botta durum:** Sadece “liquidation cascade” **tahmini**: OI düşüşü + fiyat hareketi. Gerçek liquidation feed’i yok.
- **BingX:** Swap v2’de public liquidation endpoint’i belgelenmiş değil.
- **Ücretsiz dış kaynak:** CoinGlass, Coinalyze vb. API’ler liquidation verisi sunar; çoğu ücretsiz kotada sınırlı. Binance doğrudan liquidation history vermiyor (sadece aggregate’ler).

### 2.5 Open interest geçmişi (OI zaman serisi)

- **Ne:** OI’nin son saat/gün içindeki değişim eğilimi (sadece “bir önceki snapshot” değil, 3–5 nokta).
- **Botta durum:** Sadece **anlık OI** ve bir önceki değere göre yüzde değişim. OI trendi (yükselen/düşen) sınırlı.
- **BingX:** `openInterest` tek nokta; history endpoint’i dokümanda net değil.
- **Ücretsiz:** Binance `GET /fapi/v1/openInterest` (anlık); history için `openInterestHist` (public).

---

## 3. BingX’ten Çekebileceğimiz Ek Veriler (doküman kontrolü gerek)

Aşağıdakiler BingX swap v2 dokümanında var mı diye tek tek kontrol edilmeli; varsa client’a eklenebilir:

- **Funding rate history** (örn. son 10–20 dönem)
- **Open interest history** (zaman serisi)
- **Recent trades / aggTrades** (gerçek taker buy/sell oranı hesaplamak için)
- **Liquidation orders / forceOrders** (varsa)

Bunlar eklenirse strateji ağırlıkları değiştirilmeden sadece **yeni sinyal/ filtre** olarak kullanılabilir (agresiflik korunur).

---

## 4. Ücretsiz Dış Kaynak Özeti (BingX’te yoksa veya sınırlıysa)

| Veri | Kaynak | Endpoint / not | Auth |
|------|--------|----------------|------|
| Taker long/short ratio | Binance Futures | `GET /futures/data/takerlongshortRatio` (symbol, period, limit) | Yok (public) |
| Global long/short (hesap) | Binance Futures | `GET /futures/data/globalLongShortAccountRatio` | Yok |
| Top trader long/short | Binance Futures | `GET /futures/data/topLongShortAccountRatio` | Yok |
| Funding rate history | Binance Futures | `GET /fapi/v1/fundingRate` | Yok |
| OI history | Binance Futures | `GET /futures/data/openInterestHist` | Yok |
| Fear & Greed | alternative.me | Zaten kullanılıyor | Yok |
| Liquidations (aggregate) | CoinGlass / Coinalyze | Ücretsiz kotada sınırlı; API key gerekebilir | Kayıt |

Symbol mapping: BingX `BTC-USDT` ↔ Binance `BTCUSDT` (tire kaldırılır).

---

## 5. Agresifliği Bozmadan Eklenebilecek İyileştirmeler

1. **Gerçek taker oranı (Binance proxy)**  
   BTC-USDT / ETH-USDT için Binance `takerlongshortRatio` çekilip, isteğe bağlı bir “gerçek taker” göstergesi olarak kullanılabilir; mevcut proxy ile birlikte veya yerine (config ile seçilebilir) kullanılır. Ağırlık ve eşikler aynı kalabilir.

2. **Long/short ratio (Binance, public)**  
   `globalLongShortAccountRatio` veya `topLongShortAccountRatio` ile aşırı long/short birikiminde ek filtre veya hafif contrarian bonus (mevcut funding bonusuna benzer mantık). Varsayılan ağırlık 0 ile açılıp sonra hafifçe artırılabilir.

3. **Funding rate geçmişi (Binance)**  
   Son 5–10 funding’i çekip “funding trend” (yükseliyor/düşüyor) veya “aşırı yüksek/düşük” filtre eklenebilir; mevcut tek nokta funding filtresi genişletilir, agresiflik değişmez.

4. **OI history (Binance)**  
   Son birkaç saat OI verisi ile “OI trend” (artıyor/azalıyor) eklenebilir; mevcut OI değişim yüzdesi ile tutarlı, sadece daha stabil bir sinyal olur.

5. **BingX doküman taraması**  
   Yukarıdaki (funding history, OI history, trades, liquidations) BingX’te varsa önce BingX’ten çekmek tercih edilir; yoksa Binance public ile tamamlanır.

---

## 6. Özet

- **BingX’ten çektiğimiz ana veriler** (depth, klines, mark, funding, OI) doğru kullanılıyor; eksik olanlar daha çok **taker oranı gerçek verisi**, **long/short ratio**, **funding/OI geçmişi** ve **liquidasyon** tarafı.
- **Agresiflik aynı kalacak şekilde** bu veriler:
  - Yeni/opsiyonel göstergeler,
  - Mevcut filtrelerin genişletilmesi (trend, aşırı değer),
  - veya proxy’nin gerçek veriyle desteklenmesi  
  şeklinde eklenebilir.
- **Ücretsiz ve pratik kaynak:** Binance Futures public API (API key yok); BTC/ETH ve diğer USDT çiftleri için BingX ile paralel kullanılabilir.

İstersen bir sonraki adımda Binance public client’ı ve “gerçek taker oranı + long/short ratio” entegrasyonu için somut endpoint ve kod yapısı önerebilirim.
