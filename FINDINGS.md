# Deney sonuçları — stratejinin edge'i var mı?

**Tarih:** 2026-09-05
**Veri:** 10 sembol × 40 gün × 5m mum = **115.200 bar** (BingX public API, `research/data/`)
**Yöntem:** `research/forward_returns.py` — her barda kapalı mumlardan sinyal üretilir,
sonraki N barın getirisi sinyal yönüne göre işaretlenir, piyasa sürüklenmesi ayıklanır.

---

## Kısa cevap

**Hayır. Ölçülebilir bir edge yok — ve asıl sorun sinyal kalitesi değil, işlem ufku.**

En iyi istatistiksel olarak anlamlı sinyal **~1 bps** değer üretiyor.
Round-trip maliyet **26 bps**. Arada **~23 kat** fark var.

Daha da önemlisi: botun işlem yaptığı ufukta (30 dk – 3 saat) maliyet, ortalama
fiyat hareketinin **%39–90'ını** yiyor. Bu, hiçbir gösterge kalitesinin
kapatamayacağı **yapısal** bir açık.

---

## 1. Ölçüm aleti doğrulandı

"Edge yok" sonucu, düzeneğin bozuk olmasından da kaynaklanabilirdi. Kontrol:

| Test sinyali | alpha (bps) | isabet | t |
|---|---|---|---|
| Kâhin (yönü tam bilir) | **+34.02** | %99.7 | +48.71 |
| Gürültülü (%52 doğru) | **+1.43** | %52.1 | +1.68 |
| Yazı-tura (%50) | +0.13 | %49.9 | +0.15 |

Düzenek çalışıyor: gerçek bir edge olsaydı görürdük. Ve kritik kalibrasyon —
**%52 isabetli bir sinyal ~1.4 bps üretiyor.** Botun en iyi kümesi de ~1.1 bps
üretiyor. Yani mevcut sistem, fiilen **%52'lik bir yazı-turaya** denk.

---

## 2. Hiçbir konfigürasyon maliyeti aşmıyor

60 dakika ufku, piyasa sürüklenmesi ayıklanmış (`alpha`):

| Konfigürasyon | n | tilt | alpha bps | isabet | t | alpha − maliyet |
|---|---|---|---|---|---|---|
| thesis:mean_revert | 41 | +0.17 | +29.28 | %61.0 | +0.73 | +3.28 |
| vote (legacy 4/6) | 102 | −0.02 | +7.33 | %47.1 | +0.27 | −18.67 |
| vote (eski 2/6) | 299 | −0.06 | +6.24 | %50.2 | +0.39 | −19.76 |
| thesis:trend | 98 | +0.02 | +4.07 | %46.9 | +0.16 | −21.93 |
| cluster:mean_revert | 93.174 | −0.05 | +1.52 | %53.1 | +1.76 | −24.48 |
| cluster:oscillator | 92.371 | −0.05 | +1.41 | %52.5 | +1.63 | −24.59 |
| cluster:flow | 99.037 | −0.04 | −0.13 | %48.1 | −0.38 | −26.13 |
| **cluster:trend** | 100.526 | +0.02 | **−1.03** | %46.5 | −1.34 | −27.03 |

Okunuşu:

- **Tek pozitif satır (`thesis:mean_revert`) istatistiksel olarak yok hükmünde:**
  n=41, t=0.73. 41 gözlemde bu büyüklük tesadüfen sık görülür.
- **İstatistiksel olarak anlamlı tek sonuçlar büyük örneklemli kümeler**
  (`mean_revert` t=+2.54, `oscillator` t=+2.21 — 30 dk ufkunda). İkisi de
  **gerçek ama ekonomik olarak değersiz**: ~1 bps.
- **`cluster:trend` kısa ufuklarda NEGATİF.** Yani varsayılan olarak seçtiğim
  `primary_thesis="trend"` veriye göre **yanlış tercih**. Veri `mean_revert`
  diyor — tezi yapılandırılabilir yapmamın sebebi tam da buydu.
- **tilt değerleri ~0**: sinyaller long/short dengeli, sonuçlar sürüklenme
  artefaktı değil.
- **İsabet oranları %46–53**: yazı-tura.

---

## 3. Asıl bulgu: ufuk yanlış

Maliyeti (26 bps) ortalama mutlak fiyat hareketiyle karşılaştırınca:

| Ufuk | ort. \|hareket\| | maliyet / hareket | **gereken isabet** | |
|---|---|---|---|---|
| 30 dk | 29 bps | %90 | **%94.9** | imkânsız |
| 1 saat | 40 bps | %66 | **%82.9** | imkânsız |
| 2 saat | 55 bps | %47 | **%73.5** | imkânsız |
| **3 saat** ← botun ufku | 67 bps | %39 | **%69.4** | imkânsız |
| 6 saat | 97 bps | %27 | %63.4 | çok zor |
| 12 saat | 139 bps | %19 | %59.4 | zor |
| 1 gün | 209 bps | %12 | %56.2 | mümkün |
| 3 gün | 410 bps | %6 | %53.2 | makul |
| 7 gün | 795 bps | %3 | %51.6 | rahat |

`E[kâr] = ort.hareket × (2p − 1) − maliyet`

**Bot 3 saatlik ufukta çalışıyordu ve orada başabaş için %69 isabet gerekiyor.**
Ölçülen isabet %52. Bu fark hiçbir parametre ayarıyla kapanmaz.

Ve tersinden: **ölçülen %52 isabetle minimum uygulanabilir ufuk 3–7 gün.**

Kâhin testi aynı şeyi söylüyordu: yönü *tam* bilen bir sinyal bile 60 dakikada
net sadece 8 bps kazanıyor (34 − 26).

---

## 4. Ne yapmalı

### Yapılmaması gereken
Bu stratejiyi bu ufukta canlıya almak. Parametre ayarı, gösterge ekleme, eşik
oynatma — hiçbiri %39–90'lık maliyet/hareket oranını değiştirmez. Geçen sefer
kaybedilen para bu yüzden kaybedildi; kod hataları o kaybı **hızlandırdı**,
sebebi değildi.

### Üç gerçek seçenek

**(A) Ufku büyüt — matematiğin işaret ettiği yön.**
5m scalp'ten çok-günlük swing'e geç. 1 günde gereken isabet %56, 3 günde %53.
Mevcut 23 göstergenin çoğu zaten daha uzun ufukta daha anlamlı. Bu, mevcut
kod tabanının **konfigürasyonuyla** denenebilir (`swing_*` ayarları) ama
gösterge periyotları ve TP/SL geometrisi yeniden kurulmalı.

**(B) Maliyeti düşür — tek başına yetmez.**
Maker emirle + VIP kademeyle maliyet ~10 bps'e inse bile 3 saatte gereken
isabet %57.5. Hâlâ ulaşılamaz. Ufuk baskın kısıt; maliyet ikincil.

**(C) BIST / hisse tarafına geç.**
Daha önce konuştuğumuz seçenek ve bu tablo onu destekliyor: doğal olarak
çok-günlük ufuk, funding yok, likidasyon yok, komisyon daha düşük ve
yapısal yukarı eğilim var.

### Her durumda önce
Yeni fikri **canlıya almadan** bu düzenekten geçir: `research/forward_returns.py`
sinyali alır, ileri getiriyi ölçer, maliyetle karşılaştırır. Bir strateji burada
`alpha − maliyet > 0` ve `|t| > 2` üretmiyorsa canlıda da üretmez.

---

## 5. Bu çalışmanın sınırları

Dürüst olmak gerekirse:

- **40 gün, tek rejim.** BingX 5m geçmişini ~40 günle sınırlıyor. Farklı bir
  piyasa rejiminde (yüksek volatilite, güçlü trend) sonuçlar değişebilir.
- **10 sembol.** Likit majörler. Küçük altlarda davranış farklı olabilir
  (ama orada spread/slippage daha kötü, yani muhtemelen daha kötü).
- **Orderbook geçmişi yok.** `orderflow` kümesi canlıda daha çok oy verir;
  geçmişte sadece taker-proxy üzerinden oy verebildi.
- **Tek yön ölçüldü, TP/SL simülasyonu değil.** Bu kasıtlı: sinyal kalitesini
  çıkış geometrisinden ayırmak için. TP/SL bir sinyali iyileştiremez, sadece
  nasıl hasat edildiğini değiştirir.

Bu sınırlar sonucu yumuşatmıyor: 23 kat fark, örneklem gürültüsüyle kapanacak
bir fark değil.

---
---

# BÖLÜM 2 — Uzun vade, şok tepkisi ve tahsis motoru

**Veri:** 12 sembol × **730 gün** × 1h mum = 210.240 bar (1h/4h geçmişi 2 yıl geriye gidiyor;
5m sadece 40 gün).

## 6. Uzun ufukta da edge yok

1h veride 1 gün / 3 gün / 7 gün / 14 gün ufukları, sürüklenme ayıklanmış:
hiçbir konfigürasyonda **`|t| > 2` yok.** En büyüğü 0.78.

14 günde bazı satırlar pozitif görünüyor (`vote(old 2/6)` +95 bps) ama t = 0.12 —
14 günlük örtüşen pencerelerde etkin örneklem ~6 gözlem.

Kümelerin uzun ufuktaki işaretleri ekonomik olarak anlamlı: `mean_revert` ve
`oscillator` uzun vadede **negatife dönüyor**, `trend`/`flow` pozitife. Yani kısa
vadede ortalamaya dönüş, uzun vadede trend — ders kitabına uygun, ama hiçbiri
istatistiksel olarak anlamlı değil.

## 7. Şok dönüşü: 1. yıl var, 2. yıl yok

"Ani hareketten sonra ne olur?" hipotezi. 1 günlük hareketin en uç %1'i,
eşik **sadece geçmiş 90 günden** (look-ahead yok):

| %1 eşik, 1 gün tut | ortalama | isabet | t |
|---|---|---|---|
| Tüm örneklem | +95.8 bps | %55.7 | +2.00 |
| **1. yıl** | **+296.9 bps** | %66.5 | **+3.66** |
| **2. yıl** | **−39.2 bps** | %48.5 | −0.71 |

Etki ikinci yılda tamamen kayboluyor. Ayrıca 4193 "gözlem" yalnızca **236 takvim
gününe** düşüyor — kripto çöküşleri tüm coinlerde eşzamanlı, bağımsız gözlem sayısı
göründüğünün onda biri. **Dayanıksız, kullanılamaz.**

## 8. Trend filtreleri al-tut'u yenemiyor

12 coin, 2 yıl, geçiş maliyetleri düşülmüş:

| | ortalama | pozitif | al-tut'u yendi |
|---|---|---|---|
| al-tut | +22.4% | 7/12 | — |
| MA50-gün | +10.8% | 7/12 | 6/12 |
| MA20-gün | −13.8% | 3/12 | 4/12 |
| MA100-gün | −34.9% | **0/12** | 2/12 |

Saatlik barda aynı filtre 2 yılda ~700 kez pozisyon değiştiriyor → sermayenin
%180'i geçiş maliyeti → **12/12 sembolde felaket.**

> İlk denememde MA filtresi %2.473.980 getiri gösterdi. Look-ahead bug'ıydı:
> pozisyon kararını `c[i]` ile verip `c[i]/c[i-1]` getirisini topluyordum.
> Düzeltince tablo yukarıdaki hale geldi.

## 9. Ayakta kalan tek şey: tutmak — ve sepet seçimi

| 2 yıl | getiri |
|---|---|
| 6 majör sepeti (BTC/ETH/BNB/XRP/LTC/LINK) | **+43.5%** |
| 12 coin sepeti | +15.1% |

**Hangi coinleri tuttuğun, her türlü zamanlama kuralından daha belirleyici.**
12 coinin 5'i 2 yılda para kaybetti (DOT −76%, AVAX −63%).

## 10. Drawdown freni: cazip görünen bir bug'dı

İlk sonuç: aylık dengeleme + %20 fren = **+96.6%** (al-tut +43.5%), kötü yıl
−38% yerine −8%. Çok iyi görünüyordu.

**Bug:** geri giriş şartını portföy equity'sine bağlamıştım. Nakitteyken equity
sabit olduğu için toparlanma hiç gerçekleşemiyor — fren tek yönlü kapı. Motor
2025 Şubat'ta çıkıp **kalan sürenin %79'unda nakitte oturdu.** "+96.6%", tepeye
yakın bir kez satıp dönmemenin şansıydı.

Geri giriş piyasa endeksine bağlanınca (doğrusu bu):

| | tam dönem | maxDD | işlem | ücret |
|---|---|---|---|---|
| al-tut | +43.4% | −64.1% | 6 | 0.1% |
| aylık dengeleme | +55.5% | −63.7% | 92 | 0.5% |
| %25 fren | +13.5% | −67.1% | 162 | 2.8% |
| %20 fren | +7.2% | −68.6% | 213 | 4.1% |

Fren artık **zarar veriyor** ve maksimum drawdown'ı bile düşürmüyor — düşüşte
satıp %10 yukarıdan geri alıyor. 12 hücrelik (fren × geri giriş) tarama +7.2%
ile +100% arasında savruldu, komşu hücreler birbirinden çok farklı: **gürültü
imzası.** Tutarlı olan tek şey, frenin kötü dönemde kaybı azaltıp iyi dönemde
getiriyi yemesi — yani risk aracı, getiri aracı değil. **Varsayılan: kapalı.**

## 11. Drift tetikleyicisi de getiri artırmıyor

| drift eşiği | tam dönem |
|---|---|
| kapalı | +55.5% |
| %75 | +54.3% |
| %50 | +52.2% |
| %40 | +50.0% |
| %30 | +47.7% |

Monoton: ne kadar sıkıysa o kadar kötü. Trendde kazananı erken kesiyor.
%75'te bırakıldı — **yoğunlaşma emniyet supabı** olarak, getiri aracı olarak değil.

## 12. Sevk edilen yapılandırma

`src/allocator.py`, 2 yıllık veride kendi kodu oynatılarak:

| | 1. yarı | 2. yarı | tam | maxDD | işlem | ücret |
|---|---|---|---|---|---|---|
| al-tut | +145.3% | −38.1% | +43.4% | −64.1% | 6 | 0.1% |
| **sevk edilen** | **+150.7%** | **−37.8%** | **+54.3%** | −63.5% | 102 | 0.6% |

Her iki yarıda da al-tut'u geçiyor, 2 yılda 102 işlem, toplam ücret sermayenin
%0.6'sı. Mütevazı ama **ölçülmüş ve dayanıklı**.

**Dürüst uyarı:** maksimum drawdown −63.5%. Bu strateji kaybı önlemiyor, sadece
tutmayı biraz iyileştiriyor. Kriptonun uzun vadede yükseleceği tezine bağlısın —
bot o tezi disiplinle uyguluyor, doğrulamıyor.
