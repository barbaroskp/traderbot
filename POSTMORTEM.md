# TraderBot Postmortem — Neden Zarar Etti?

**Tarih:** 2026-09-05
**Yöntem:** Kod tabanının tamamı + git geçmişi + config dosyaları incelendi; kritik iddialar
sayısal olarak doğrulandı.

---

## TL;DR

Bot zarar etti çünkü **kanıtlanmış bir edge'i yoktu ve bu edge'siz durumda çok yüksek
frekansla, sanılandan 8 kat büyük pozisyonlarla işlem yaptı.**

Tek bir hata değil, birbirini besleyen bir zincir var. En ölümcül üçü:

1. **Sessiz config regresyonu** — `.env` dosyasındaki pozisyon limitleri kod tarafından
   yok sayılıyor. Sen "toplam 8 USDT risk" sandın, bot bakiyenin %80'ini margin olarak
   kullandı (10x kaldıraçla → bakiyenin 8 katı notional).
2. **Maliyet > edge** — gerçek sürtünme işlem başına ~20 bps, bot 8 bps varsayıyor.
   Günde ~80 işlemde bu **günlük bakiyenin %9-22'si** demek. Strateji mükemmel bir yazı-tura
   olsa bile 30 günde bakiye erir.
3. **Ters çalışan güven skoru** — az göstergeli (zayıf kanıtlı) sinyaller daha *yüksek*
   skor alıyor, dolayısıyla daha büyük pozisyon ve daha yüksek kaldıraç kazanıyor.

Sorunun **fee mi timing mi** olduğu sorusunun cevabı: **ikisi de, ama asıl sorun ikisinden
önce gelen "edge yokluğu".** Fee ve timing, sıfır edge'i negatife çeviren çarpanlar.

---

## 1. Sessiz config regresyonu — en büyük tek hasar

`00ee744` commit'i pozisyon boyutlandırmayı sabit USDT'den bakiye yüzdesine çevirdi:

| Eski alan (env'de hâlâ var) | Yeni alan (config.py'de) | Sonuç |
|---|---|---|
| `MAX_TOTAL_MARGIN_USDT=8` | `max_total_margin_pct = 0.80` | env yok sayıldı |
| `MAX_TRADE_MARGIN_USDT=3` | `max_trade_margin_pct = 0.20` | env yok sayıldı |
| `MAX_MARGIN_HIGH_CONVICTION_USDT=5` | `max_margin_high_conviction_pct = 0.30` | env yok sayıldı |

`config.py:28` → `extra="ignore"`. Pydantic tanımadığı env değişkenini **hata vermeden
atıyor**. `.env.example` ve `.env.optimized` dosyalarının ikisi de hâlâ eski isimleri
kullanıyor.

**Etkisi:** 100 USDT bakiye ile:
- Sandığın: tek işlem max 3 USDT margin, toplam 8 USDT.
- Gerçekleşen: tek işlem 20 USDT margin × 10x kaldıraç = **200 USDT notional**,
  toplam 80 USDT margin → **800 USDT'ye kadar notional**.

Bu tek başına küçük bir zararı büyük bir zarara çevirir.

**Düzeltme:** `.env` dosyalarındaki üç değişkeni `MAX_TOTAL_MARGIN_PCT` /
`MAX_TRADE_MARGIN_PCT` / `MAX_MARGIN_HIGH_CONVICTION_PCT` olarak yeniden adlandır,
**ve** `config.py`'de `extra="forbid"` yap ki bir daha sessizce yok sayılmasın.

---

## 2. Maliyet matematiği — asıl katil

### Gerçek sürtünme
| Kalem | Bot varsayımı | Gerçek |
|---|---|---|
| Fee (taker, çift yön) | 8 bps | **10 bps** (BingX taker 0.05%) |
| Slippage (giriş+çıkış) | 3 bps (paper) | **10-30 bps** (market emri, 30-40 bps spread limitli coinler) |
| Funding | **0 — hiç modellenmiyor** | 8 saatte ±1-5 bps |
| **Toplam** | **8 bps** | **~20 bps** (iyimser) |

Bot maliyeti **%150 eksik** hesaplıyor. Yani her backtest / paper sonucu bu kadar iyimser.

### Başabaş kazanma oranı
| Ayar | R:R | Brüt başabaş | **Net başabaş** |
|---|---|---|---|
| ATR tabanı (SL 50 / TP 100) | 2.0 | %33.3 | **%46.7** |
| `.env.optimized` (SL 40 / TP 120) | 3.0 | %25.0 | **%37.5** |
| `config.py` (SL 70 / TP 200) | 2.9 | %25.9 | **%33.3** |

Sürtünme, gereken kazanma oranını **7-13 puan** yukarı itiyor. Bu, gösterge tabanlı
bir scalp stratejisinin gerçekçi olarak kapatabileceğinden çok daha büyük bir açık.

### Günlük yanma hızı (edge = 0 varsayımıyla, 100 USDT bakiye)
| Senaryo | İşlem/gün | Notional/işlem | Günlük maliyet | 30 gün sonra |
|---|---|---|---|---|
| Sandığın ayar (%8 margin × 3x) | 48 | 24 USDT | %2.3 | %50 |
| Gerçekleşen default (%20 × 3x) | 80 | 60 USDT | **%9.6** | **%4.8** |
| Tier3 kaldıraç (%20 × 7x) | 80 | 140 USDT | **%22.4** | **%0.0** |

**Bu tablo, git geçmişindeki `-84% loss` commit'ini tek başına açıklıyor.**
Strateji hiç yanlış olmasaydı bile bu sonuç çıkardı.

---

## 3. Güven skoru ters çalışıyor (anti-Kelly)

`strategy.py:_normalize_weighted_score` skoru şöyle hesaplıyor:

```
skor = (kazanan taraftaki ağırlıkların toplamı) / (SADECE O GÖSTERGELERİN max'ı) × 100
```

Payda, **sadece oy veren göstergeleri** içeriyor. Sonuç:

| Senaryo | weighted_score |
|---|---|
| 2 gösterge, ikisi de tam ağırlıkta (zayıf kanıt) | **81.8** |
| 8 gösterge aynı yönde, 3'ü kısmi ağırlıkta (güçlü kanıt) | **77.7** |

Eşikler: `high_conviction=60` (margin ×1.5), `tier2=55` (5x), `tier3=70` (7-10x).

**2 göstergeli sinyal tier3'ü geçiyor, 8 göstergeli geçemiyor.** Bot en zayıf kanıtlı
işlemlere en büyük parayı ve en yüksek kaldıracı koyuyor. Bu, pozisyon boyutlandırmanın
tam tersi olması gereken şey.

**Düzeltme:** payda sabit olmalı — ya tüm göstergelerin toplam max ağırlığı, ya da skoru
`(kazanan ağırlık − kaybeden ağırlık) / toplam_max` şeklinde net oy farkı olarak hesapla.

---

## 4. Strateji kendi içinde çelişiyor (sorduğun soru: evet, çelişiyor)

`CLUSTER_MEMBERS["trend"] = ["ema_zscore", "trend", "adx"]`

Aynı kümenin içinde:
- `ema_zscore`: fiyat hızlı EMA'nın **altında** → **LONG** (ortalamaya dönüş / mean reversion)
- `trend`: fiyat EMA50'nin **üstünde** → **LONG** (trend takibi)
- `adx`: +DI > −DI → **LONG** (trend takibi)

İki farklı piyasa felsefesi aynı oy havuzunda. Mean reversion "aşırı düştü, geri döner" der;
trend takibi "yükseliyor, yükselmeye devam eder" der. Bunlar **birbirini iptal eden**
varsayımlar — aynı anda ikisine de inanamazsın.

Daha kötüsü: `min_cluster_confluence = 2` (6 kümeden 2'si). Yani **6 kümeden 4'ü zıt yönde
oy verirken bile işlem açılabiliyor.** Bu bir mutabakat filtresi değil, gürültü toplayıcı.

**Cevap:** Evet, tek taraflı (tek felsefeli) bir analize geçmeliydin. İki seçenek:
- **Sadece trend takibi:** yüksek ADX + EMA dizilimi + kırılım. Düşük kazanma oranı (%35-40),
  yüksek R:R (3-5x). Trendli piyasada çalışır.
- **Sadece mean reversion:** BB alt bandı + RSI aşırı satım + düşük ADX. Yüksek kazanma
  oranı (%60-65), düşük R:R (0.7-1x). Yatay piyasada çalışır.

Ama **asla ikisini aynı oy havuzunda karıştırma.** Karıştırmak istiyorsan rejim filtresi
şart (`use_regime_filter` zaten var ama **kapalı** — çünkü sinyal sayısını düşürüyordu).

---

## 5. Sinyal zamanlaması: göstergeler "repaint" ediyor

`fetch_klines` son mumu atmıyor. `compute_indicators` içinde `closes[-1]` ve `volumes[-1]`
**henüz kapanmamış, o an oluşmakta olan mum.**

Sonuçları:
- `volume_ratio = volumes[-1] / avg_vol` — mumun ilk dakikasında hacim doğal olarak düşük,
  son dakikasında yüksek. `volume_spike` bayrağı **sadece mum kapanışına yakın** ateşleniyor.
- RSI, MACD, BB%, z-score — hepsi 3 dakikada bir yeniden hesaplanıyor ve mum kapanana kadar
  **değerleri değişiyor**. Aynı mum içinde sinyal doğuyor, kayboluyor, tekrar doğuyor.
- Backtest'te bu sorun **yok** (tamamlanmış mumlarla çalışıyor). Yani backtest ve canlı
  farklı veri görüyor — backtest sonuçları canlıya transfer olmuyor.

**Düzeltme:** `klines[:-1]` — son mumu at. Tek satırlık düzeltme, en yüksek kaliteli
etkilerden biri.

---

## 6. Backtest ve paper mod sistematik olarak iyimser

| Konu | Backtest | Paper | Canlı |
|---|---|---|---|
| Giriş fiyatı | `mid_price` — **slippage yok** | `best_ask × (1+slip)` | MARKET emri, gerçek slippage |
| Spread | sabit 5 bps | gerçek | gerçek (40 bps'e kadar) |
| Derinlik | 10.000 USDT varsayım | gerçek | gerçek |
| SL tetikleme | mum high/low (doğru) | **3 dakikada bir poll** | borsa emri (anlık) |
| Funding | yok | yok | **gerçek, PnL'e yansımıyor** |
| Kısmi TP | yok | var | var |

**Paper modun en kritik kusuru:** SL kontrolü 2-3 dakikada bir yapılıyor. Fiyat iki poll
arasında SL'i delip geri dönerse **paper mod bunu hiç görmez ve pozisyon devam eder.**
Canlıda borsadaki stop emri tetiklenir. Yani paper mod, canlının kaybettiği yerlerde
kazanıyor gösteriyor. Paper'ın "iyi" görünüp canlının zarar etmesinin doğrudan sebebi bu.

---

## 7. Diğer bulgular

**Bug: anti-likidasyon paper modda ölü kod.** `execution.py:450` `exit_reason = "ANTI_LIQUIDATION"`
atıyor, `execution.py:473` 23 satır sonra koşulsuz `exit_reason = ""` ile siliyor. Paper modda
anti-likidasyon **hiç çalışmadı**.

**Kelly sıfıra inemiyor.** `_compute_kelly_fraction` negatif edge'de negatif değer üretir —
doğru davranış "bahsi kes"tir. Ama `kelly_min_fraction = 0.05` (env'de 0.02) ile tabanlanıyor.
Yani **bot kendi istatistikleri kaybettiğini kanıtladığında bile bahis oynamaya devam ediyor.**
Kelly'nin tüm amacı bu.

**Sahte çeşitlendirme.** 5 farklı altcoin pozisyonu, kripto perp'lerin BTC ile ~0.85-0.9
korelasyonu nedeniyle **1 kaldıraçlı BTC bahsi** demek. 1x riske 5x fee ödüyorsun.
`max_same_direction_positions = 3` ile 3 LONG + 2 SHORT olabiliyor — net pozisyon ≈ 0,
ama 5 işlemin fee'si tam.

**Fear & Greed sembol bazlı sinyal değil.** `alternative.me` endeksi **günlük ve piyasa
geneli** — 300 coinin hepsi için aynı değer. `composite_sentiment` içinde %15 ağırlıkla
kullanılıyor. Bu çeşitlendirme değil, tüm portföye aynı yönde sistematik bir yanlılık.

**Sahte taker verisi.** `taker_buy_ratio` gerçek taker verisi değil, "yeşil mum = alım"
proxy'si. Yani zaten fiyat yönünün türevi — MACD/momentum ile aynı bilgiyi tekrar sayıyor.

**Çoklu hipotez testi.** 300 sembol × 22 gösterge × günde 480 tarama = **günde ~3 milyon
istatistiksel test**. Bu ölçekte "sinyal" bulmak kaçınılmaz — bulduğun şey gürültü.

**Optimizer overfit ediyor.** `ParameterOptimizer` grid search'ü aynı dönemde çalıştırıp
aynı dönemde raporluyor. Walk-forward yok, out-of-sample yok. `_compute_score` <5 işlemde
−999 veriyor → yüksek frekanslı (yani yüksek maliyetli) parametre setlerine bias.

---

## 8. Meta-hata: yanlış şeyi optimize etmek

Git geçmişi ve `config.py` yorum satırları bir örüntü gösteriyor:

```
b7e47e1 fix: agresif filtre gevsetme - bot 7 gundur islem yapamiyordu
```
```python
entry_threshold_bps: float = 15.0   # dusuk esik: daha fazla coin sinyal uretsin (40 cok katiydi)
min_volume_24h_usdt: float = 1_000_000  # 1M$ yeterli (5M cok yuksekti, firsatlari disladik)
max_spread_bps: float = 30.0        # genis spread: daha fazla coin (15 cok katiydi)
use_regime_filter: bool = False     # KAPALI: sinyalleri cok engelliyor
require_momentum_confirmation: False
avoid_funding_window: False
use_funding_filter: False
risk_state_disabled: True           # HER ZAMAN NORMAL
soft_kill_switch_enabled: False     # bot ASLA durmasin
```

Bot işlem açmadığında **filtreler gevşetildi**. Ama bot işlem açmıyorduysa, bunun sebebi
filtrelerin katı olması değil, **o koşullarda gerçekten iyi bir fırsat olmamasıydı.**
Filtreler görevlerini yapıyordu.

Fee-expectancy guard'ın eşiği bile `1.5` iken `1.0`'e düşürülmüş — yorumda gerekçesi yazıyor:
*"1.5 cok katiydi, cok sinyal engelliyordu"*.

**İşlem sayısını değil, işlem başına beklenen değeri optimize etmek gerekiyordu.**
Sıfır işlem, negatif beklenen değerli 80 işlemden iyidir.

---

## 9. Ben olsam ne yapardım

### Önce: gerçeği ölç (kod yazmadan)
Sunucudaki `data/bingx_agent.db` dosyasını al ve şunu çalıştır:

```sql
SELECT exit_reason, COUNT(*) n, ROUND(AVG(realised_pnl),4) avg_pnl,
       ROUND(SUM(realised_pnl),2) total
FROM positions WHERE status='CLOSED' GROUP BY exit_reason ORDER BY total;

-- Gerçek kazanma oranı ve ortalama tutma süresi
SELECT COUNT(*) n,
       ROUND(100.0*SUM(realised_pnl>0)/COUNT(*),1) win_rate,
       ROUND(AVG((julianday(closed_at)-julianday(opened_at))*1440),1) avg_hold_min
FROM positions WHERE status='CLOSED';

-- Toplam ödenen fee (notional üzerinden tahmini)
SELECT ROUND(SUM(notional)*0.0010, 2) tahmini_fee FROM positions WHERE status='CLOSED';
```

Son sorgunun sonucunu toplam zararla karşılaştır. **Tahminim: zararın %60-90'ı fee+slippage.**
Bu doğrulanırsa strateji tartışması bile gereksiz — problem tamamen maliyet/frekans.

### Sonra: yeniden yazmak yerine sertçe kısıtla

Mevcut botu çöpe atmaya gerek yok, iskeleti iyi. Şu değişikliklerle:

| # | Değişiklik | Neden |
|---|---|---|
| 1 | `.env`'deki 3 margin değişkenini `_PCT` olarak düzelt; `extra="forbid"` yap | Sessiz regresyonun kökü |
| 2 | `FEE_RATE_BPS=5`, slippage varsayımını 8 bps'e çıkar | Gerçekçi maliyet |
| 3 | `klines[:-1]` — tamamlanmamış mumu at | Repaint'i bitirir |
| 4 | Sembol evrenini **300 → 5'e** indir (BTC, ETH, SOL, BNB, XRP) | Çoklu hipotez testini öldürür, spread/slippage düşer |
| 5 | `min_cluster_confluence: 2 → 4`, `min_weighted_score: 35 → 65` | Gerçek mutabakat |
| 6 | `MAX_OPEN_POSITIONS: 5 → 2`, `COOLDOWN_MINUTES: 10 → 60` | Frekansı ~10x düşürür = maliyeti ~10x düşürür |
| 7 | Kaldıracı **sabit 2x** yap, dinamik kaldıracı kapat | Ters çalışan skorun etkisini sıfırlar |
| 8 | `kelly_min_fraction: 0 `— negatif edge'de bahsi kesebilsin | Kelly'nin asıl işlevi |
| 9 | `_normalize_weighted_score` paydasını sabitle | Anti-Kelly hatasını düzeltir |
| 10 | Mean reversion **veya** trend seç, kümeleri buna göre kur | Felsefi çelişkiyi bitirir |
| 11 | `soft_kill_switch_enabled=true`, drawdown %15'te dursun | Kanamayı durdurur |
| 12 | Zaman aşımını 180dk → 45dk **ya da** TP'yi ATR×5'e çıkar | 3 saat tutup TP'ye ulaşmayan işlemler saf maliyet |
| 13 | Backtest'e slippage + funding ekle | Backtest'in canlıya transfer olması için şart |
| 14 | Walk-forward: 6 ay optimize → sonraki 2 ay test | Overfit'i yakalar |

**Kritik kural:** Bunları yaptıktan sonra **canlıya geçmeden önce** walk-forward testte
out-of-sample dönemde net pozitif çıkmalı. Çıkmıyorsa strateji gerçekten edge'siz demektir
ve hiçbir parametre ayarı bunu kurtaramaz.

---

## 10. "BIST hisse botu daha mı güvenli?" — dürüst cevap

### Önerdiğin stratejinin gizli kusuru
"Değerinde al, %3 artınca sat" — **stop-loss'suz** bir strateji. Kazanma oranı yüksek
görünür (%70-80) ama:
- Kazançlar %3'te kesilir (sınırlı)
- Kayıplar sınırsızdır (hisse %40 düşerse pozisyon açık kalır)

Bu bir martingale risk profili. 20 işlem kazanır, 21. işlem hepsini siler. Kripto botunun
tam tersi hata: o çok fazla stop yiyordu, bu hiç stop yemeyecek.

### Ama yapısal olarak BIST gerçekten daha uygun
| Kalem | BingX perp | BIST hisse |
|---|---|---|
| İşlem başı maliyet | ~20 bps + funding | ~10-20 bps (komisyon+BSMV), funding yok |
| Kaldıraç | 3-10x | 1x (VIOP hariç) |
| Likidasyon riski | var | **yok** |
| İşlem saati | 7/24 (bot sürekli işlem arar) | 10:00-18:00 (doğal frekans limiti) |
| Yapısal yön | yok (sıfır toplamlı, fee'li → negatif toplamlı) | **uzun vadede yukarı** (enflasyon + büyüme) |
| Gecelik risk | sürekli | gap riski var ama likidasyon yok |

**En önemli fark:** kripto perp piyasası negatif toplamlı bir oyundur (fee + funding). BIST'te
"al ve tut" bile pozitif beklenen değere sahiptir. Yani BIST'te **hiçbir şey yapmayan bir
bot bile** uzun vadede pozitif olur — bu çok daha affedici bir zemin.

### Pratik engeller (bilmen gerekenler)
- **API:** Algolab (Denizbank), İş Yatırım, Matriks, Gedik gibi kurumların API'leri var ama
  kripto borsaları kadar açık/dokümante değil. Çoğu için hesap + başvuru gerekir.
- **T+2 takas:** Sattığın parayla aynı gün tekrar alamazsın (nakit alım-satımda). Frekansı
  doğal olarak sınırlar — bu senin durumunda **avantaj**.
- **Açığa satış:** Bireysel yatırımcı için pratikte çok kısıtlı. Yani tek yönlü (sadece
  LONG) bir bot yazacaksın — bu da bir sadeleşme, felsefi çelişki kalmaz.
- **Küçük hisselerde manipülasyon** ve düşük likidite BIST'te de var. BIST-30/BIST-50
  dışına çıkma.

### Benim önerim
BIST'e geçmek **doğru yön**, ama "%3 artınca sat" stratejisiyle değil. Bunun yerine:

1. **Basit ve test edilebilir bir kural** seç. Örnek: BIST-30 hisseleri içinde,
   200 günlük ortalamanın üstünde olan ve son 20 günde en yüksek momentuma sahip 5 hisseyi
   tut, ayda bir dengele. Bu strateji akademik olarak da defalarca test edilmiş (cross-sectional
   momentum) ve kripto scalp'ten çok daha sağlam bir zemine oturuyor.
2. **Stop koy** ama geniş: %10-12. Sınırsız kayıp riskini kes ama gürültüde stop yeme.
3. **Ayda 5-10 işlem** hedefle, günde 80 değil. Maliyet problemi böyle çözülür.
4. **Önce 10 yıllık BIST verisiyle backtest et** (yfinance ile `.IS` sembolleri ücretsiz).
   Walk-forward yap. Pozitif çıkmazsa yazma.

---

## 11. Özetle: neden zarar ettin?

Sırayla, en büyük etkiden başlayarak:

1. **Config regresyonu** pozisyonları sandığından 8 kat büyük yaptı.
2. **Maliyet edge'den büyüktü** — günde %10-22 sürtünme, kazanılabilir edge ise sıfıra yakın.
3. **Skor mantığı ters** — en zayıf sinyallere en büyük kaldıraç.
4. **Strateji kendi içinde çelişkili** — mean reversion + trend takibi aynı havuzda,
   6 kümeden sadece 2'si yeterli.
5. **Göstergeler repaint ediyordu** — tamamlanmamış mum kullanılıyor.
6. **Paper ve backtest iyimserdi** — slippage yok, SL poll bazlı, funding yok.
   Bu yüzden test iyi, canlı kötü göründü.
7. **Tüm koruma mekanizmaları kapatılmıştı** — çünkü sinyal sayısını düşürüyorlardı.
8. **300 sembolde 22 gösterge taramak** istatistiksel olarak gürültü madenciliğidir.

**En önemli ders:** Bot teknik olarak iyi yazılmış. Sorun mühendislikte değil,
**"işlem açsın" hedefiyle "para kazansın" hedefinin karıştırılmasında.** Her filtre
gevşetildiğinde bot daha çok işlem açtı ve daha hızlı kaybetti.
