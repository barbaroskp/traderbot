# Bu bot ne yapıyor?

> Hiç bilmeyen biri için özet yukarıda, teknik detay aşağıda.

---

## 1. Tek paragrafta

Bu depo bir **kripto alım-satım botu** ve onun etrafında yapılmış **ölçüm
çalışmalarını** içeriyor. Bot, BingX borsasındaki coinleri sürekli tarıyor,
24 farklı teknik göstergeyi hesaplıyor, yeterince gösterge aynı yönü
işaret ederse pozisyon açıyor, kâr hedefine veya zarar durdurucusuna
gelince kapatıyor.

Depodaki araştırma kısmı ise şu soruyu cevaplıyor: **bu gerçekten para
kazandırıyor mu?** Cevap ölçüldü ve bu belgede saklanmıyor.

---

## 2. Bot nasıl çalışıyor — adım adım

Bot her 5 dakikada bir şu döngüyü çalıştırıyor:

**Adım 1 — Tara.** Borsadaki yüzlerce coinden, yeterince likit olanları
seçiyor. Likit demek: alım-satım farkı (makas) dar ve günlük hacmi yüksek.
Likit olmayan coinde işlem yapmak pahalıdır.

**Adım 2 — Ölç.** Seçilen her coin için son 100 mumu alıp 24 gösterge
hesaplıyor: RSI, MACD, Bollinger bantları, hareketli ortalamalar, hacim
patlaması, emir defteri dengesizliği ve diğerleri.

**Adım 3 — Oyla.** Göstergeler 7 kümeye ayrılmış durumda:

| küme | ne bakıyor | üyeleri |
|---|---|---|
| osilatör | aşırı alım/satım | RSI, Stoch RSI, Williams %R, RSI uyumsuzluğu |
| ortalamaya dönüş | fiyat adil seviyeden ne kadar uzak | z-skor, Bollinger, VWAP, hacim profili |
| trend | yön ve gücü | trend, ADX, MACD, momentum |
| oynaklık | sıkışma ve kırılım | squeeze, breakout, hacim patlaması, hız |
| emir akışı | alıcı/satıcı baskısı | emir defteri, taker oranı, balina emirleri |
| para akışı | pozisyon değişimi | OBV, açık pozisyon, likidasyon zinciri |
| karşıt | duygu göstergesi | sentiment |

Her küme kendi içinde hemfikirse tek bir oy veriyor. Yeterli sayıda küme
aynı yönü gösterirse sinyal doğuyor.

**Adım 4 — Süz.** Sinyal birçok kapıdan geçiyor: aynı coinde yakın zamanda
işlem yapıldı mı, açık pozisyon sınırı doldu mu, beklenen kâr komisyonu
karşılıyor mu, risk durumu normal mi.

**Adım 5 — Aç.** Geçerse pozisyon açılıyor. Büyüklük, o anki bakiyeye göre
hesaplanıyor — yani para büyüdükçe pozisyonlar da büyüyor. Kâr hedefi ve
zarar durdurucu, coinin oynaklığına göre belirleniyor.

**Adım 6 — Yönet.** Her turda açık pozisyonlar kontrol ediliyor: hedefe
ulaştı mı, stopa değdi mi, süre doldu mu. Biri olursa kapatılıyor.

---

## 3. Bu bot para kazanıyor mu?

Hayır. Ve bunu tahmin ederek değil, **botun kendi kodunu geçmiş veride
çalıştırarak** biliyoruz.

BingX'te 89 coin, 41 gün, saatlik ve 5 dakikalık gerçek fiyatlarla:

| yapılandırma | 10.000 TL şuna dönüştü |
|---|---|
| bugünkü ayarlar | **8.956 TL** (−%10,4) |
| ilk kurulan ayarlar | **34 TL** (−%99,7) |

İkinci satır önemli: bot ilk yazıldığında 3x kaldıraç, bakiyenin %80'i
marj ve 5 eşzamanlı pozisyon kullanıyordu. 41 günde 4.122 işlem yaptı,
**işlemlerin %29'unu kazandı** ve parayı sıfırladı.

Neden? Çünkü işlemlerin kendisi komisyondan *önce* zarardaydı (−5.798 TL),
komisyon üstüne 2.778 TL daha ekledi.

### Denenen her şey

Bu deponun araştırma kısmı, botun kazanmasını sağlayacak bir ayar aradı:

- **24 göstergenin her biri** ayrı ayrı ölçüldü, 93.193 oy üzerinde.
  Hiçbiri işlem maliyetini aşmadı.
- **16 parametre kombinasyonu** (bekleme süresi, konfluans eşiği, kaldıraç,
  kâr/zarar oranı) örneklemin ilk yarısında ayarlanıp ikinci yarısında test
  edildi. Hiçbiri iki pencerede de kazanmadı.
- **1.008 tamamen bağımsız kural** tarandı. İlk yarıda kazananların ikinci
  yarıdaki performansıyla korelasyonu **−0,037** — yani sıfır. Kazananı
  önceden seçmenin yolu yok.
- **7 zaman dilimi** (1 dakikadan günlüğe) karşılaştırıldı. Tek istatistiksel
  olarak sağlam sinyal günlük MACD çıktı, ama o da sadece işlem maliyetinin
  yüksek olduğu likit olmayan coinlerde çalışıyordu.

### Neden kaybediyor — tek cümlelik açıklama

Her alım-satım sabit bir gişe parası ödetiyor (BingX'te yaklaşık %0,16).
Fiyatın 5 dakikada oynadığı mesafe bu paradan küçük, bir günde oynadığı
mesafe büyük. Gösterge iyi ya da kötü olması bunu değiştirmiyor.

```
 1 saat tutarsan   → işlem başına -%0,131
 3 saat tutarsan   → işlem başına -%0,073
 8 saat tutarsan   → işlem başına +%0,060
24 saat tutarsan   → işlem başına +%0,595
```

Bu tabloda **hiçbir gösterge kullanılmıyor** — rastgele giriliyor. Tek
değişen, ne kadar beklendiği.

---

## 4. Peki ne çalışıyor?

Ölçümden sağ çıkan tek şey **varlık tutmak**:

| strateji | yıllık getiri | en kötü düşüş |
|---|---|---|
| BTC %70 / ETH %30, kaldıraçsız, eşik dengelemeli | **+%39,3** | −%83 |
| aynısı 2x kaldıraçla | +%10,6 | −%99 |
| günlük al-sat botu | −%53 | — |

Kaldıracın her artışı hem getiriyi düşürüyor hem düşüşü derinleştiriyor.
Matematiksel sebebi var: `g(L) = r + L(μ−r) − L²σ²/2`. Bu sepet için
büyüme 1,05x'te maksimum, 2,10x'te sıfır, üstünde negatif.

`src/allocator.py` bu işi yapan motor: tahmin yapmıyor, sadece hedef
oranları koruyor.

---

## 5. Depoda ne var

```
src/
  strategy.py          24 gösterge, küme oylaması, tüm kapılar
  marketdata.py        gösterge hesabı, mum verisi, emir defteri
  execution.py         emir gönderme, TP/SL, pozisyon kapatma
  risk.py              pozisyon boyutu (Kelly), risk durumları
  scheduler.py         5 dakikalık ana döngü
  bingx_client.py      borsa API'si, hız sınırlama, imzalama
  allocator.py         sepet tutma motoru (ölçümden geçen tek şey)
  costs.py             aracı kurum maliyet profilleri
  bist/                BIST için adli muhasebe taraması
  portfolio/           çok varlıklı portföy motoru

research/
  replay/              botu geçmiş veride birebir çalıştıran motor
  crypto/              kripto evreninde sinyal çalışmaları
  bist/                BIST çalışmaları

tests/                 331 test
```

### Nasıl çalıştırılır

```bash
pip install -r requirements.txt
cp .env.example .env          # BingX anahtarlarını gir
python -m src.cli --help

python -m pytest              # testler

# botu geçmiş veride çalıştır
python -m research.replay.fetch --days 40 --symbols 100
python -m research.replay.run --days 40 --capital 10000

# hangi göstergenin ne yaptığını ölç
python -m research.replay.attribution

# aracı kurum maliyetine göre karar tablosu
python -m src.costs 0.10
```

---

## 6. Bu depoyu okuyan biri için uyarılar

**Backtest sonuçlarına dikkat.** Bu projede dört ayrı "bulgu" güçlü
göründü ve incelenince çöktü: şok-dönüşü (birinci yıl t=+3,66, ikinci yıl
−0,71), hareketli ortalama filtreleri (geleceğe bakma hatası), düşüş
freni (yanlış yeniden giriş mantığı), ve tier-C trend takibi (evren
geçmişe bakarak seçilince t=+3,48'den +0,96'ya düştü).

**Deneme bütçesi diye bir şey var.** 2 yıllık veriyle yaklaşık 7 bağımsız
kural denenebilir; ötesinde örneklem içinde kazanıp dışında hiçbir şey
yapmayan bir "bulgu" garanti hale gelir. Bu depoda 50'den fazla kural
denendi, yani buradaki hiçbir sayı ölçüm değil, ileriye doğru
doğrulanması gereken hipotez.

**Yeni faktör eşiği t > 3,0'dır, 2,0 değil** (Harvey, Liu & Zhu).
`research/bist/referee.py` bu yüzden 3,0 kullanıyor.

**Kendi hatalarım da kayıtlı.** Bu oturumda replay motorunda TP ve SL'yi
ters bağladığım için bot −%99,9 raporladı; düzeltince −%99,7 oldu. Hata
gerçekti ama sonucu değiştirmedi. `RESEARCH-SYNTHESIS.md` içinde dört
hatanın hepsi yazılı.

---

## 7. Detaylı belgeler

| dosya | içerik |
|---|---|
| `RESEARCH-SYNTHESIS.md` | tüm ölçümlerin sentezi, kripto + BIST |
| `POSTMORTEM.md` | botun ilk halinin neden para kaybettiği |
| `CHANGELOG-FIXES.md` | düzeltilen 6 hata, tek tek |
| `FINDINGS.md` | ileri getiri çalışmaları |
| `research/bist/FINDINGS-BIST.md` | BIST ölçümleri |

---

## 8. Son söz

Bu depo bir para makinesi değil. Bir **ölçüm aracı**.

Asıl değeri, bir stratejinin işe yaramayacağını para riske atmadan,
saatler içinde söyleyebilmesinde. Bu oturumda beş ayrı fikir bu şekilde
elendi — her biri mantıklı görünüyordu, hiçbiri ölçümden geçmedi.

Kazandıran şeyin ne olduğu da ölçüldü: **işlem yapmak değil, sahip olmak.**
