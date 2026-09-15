# BingX Agent — Çalışma Mantığı ve Ölçüm Sonuçları

## Özet

Bu depo bir kripto alım-satım botu ve onu değerlendirmek için kurulmuş ölçüm
altyapısını içerir.

Bot, BingX borsasındaki coinleri beş dakikada bir tarar, her biri için 24
teknik gösterge hesaplar, yeterli sayıda gösterge aynı yönü işaret ettiğinde
pozisyon açar ve kâr hedefine ya da zarar durdurucusuna ulaştığında kapatır.

Ölçüm altyapısı ise botun kendi karar motorunu geçmiş piyasa verisi üzerinde
yeniden çalıştırır ve sonucu raporlar. Bu belge, botun nasıl çalıştığını ve
ölçümlerin ne gösterdiğini anlatır.

Sonuç önden verilmek gerekirse: bot, 41 günlük gerçek BingX verisinde 10.000
TL'yi 8.956 TL'ye düşürmektedir. İlk yapılandırmasıyla bu rakam 34 TL'dir.
Nedenleri ve pozitif sonuç veren tek yaklaşım aşağıda ele alınmıştır.

---

## 1. Çalışma döngüsü

Bot her beş dakikada bir aşağıdaki adımları yürütür.

**Tarama.** Borsadaki yüzlerce coinden yeterince likit olanları seçer. Likidite
burada iki şey demektir: alış-satış farkının (makas) dar olması ve günlük işlem
hacminin yüksek olması. Likit olmayan bir coinde işlem yapmanın maliyeti,
beklenen kazancı aşar.

**Ölçüm.** Seçilen her coin için son 100 mum alınır ve 24 gösterge hesaplanır:
RSI, MACD, Bollinger bantları, hareketli ortalamalar, hacim patlaması, emir
defteri dengesizliği ve diğerleri.

**Oylama.** Göstergeler yedi kümeye ayrılmıştır:

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

**Filtreleme.** Doğan sinyal bir dizi kapıdan geçer: aynı coinde yakın zamanda
işlem yapılmış mı, açık pozisyon sınırı dolmuş mu, beklenen kâr işlem
maliyetini karşılıyor mu, risk durumu normal mi.

**Pozisyon açma.** Filtreleri geçen sinyal için pozisyon açılır. Büyüklük o
anki bakiyeye göre hesaplanır, dolayısıyla sermaye büyüdükçe pozisyonlar da
büyür. Kâr hedefi ve zarar durdurucu, coinin oynaklığına (ATR) göre belirlenir.

**Pozisyon yönetimi.** Her turda açık pozisyonlar kontrol edilir: hedefe
ulaşıldı mı, stop seviyesine değildi mi, azami tutma süresi doldu mu.

---

## 2. Ölçüm sonuçları

Bot para kazanmıyor. Bu, tahmin değil ölçüm sonucudur: botun kendi kodu geçmiş
veri üzerinde yeniden çalıştırılarak elde edilmiştir.

BingX'te 89 coin, 41 gün, saatlik ve 5 dakikalık gerçek fiyatlarla:

| yapılandırma | 10.000 TL şuna dönüştü |
|---|---|
| bugünkü ayarlar | **8.956 TL** (−%10,4) |
| ilk kurulan ayarlar | **34 TL** (−%99,7) |

İkinci satır dikkat çekicidir. Bot ilk yazıldığında 3x kaldıraç, bakiyenin
%80'i marj ve beş eşzamanlı pozisyon kullanıyordu; 41 günde 4.122 işlem yaptı,
bunların %29'unu kazandı ve sermayeyi tüketti.

Kaybın kaynağı yalnızca komisyon değildir: işlemler komisyon düşülmeden önce
de zarardaydı (−5.798 TL). Komisyon bunun üzerine 2.778 TL ekledi.

### Denenen yapılandırmalar

Deponun araştırma bölümünde, botu kâra geçirecek bir yapılandırma arandı:

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

### Veri ve yöntem

Buradaki sayıların tamamı, bu depoda indirilip çalıştırılan veriden üretilmiştir.

**İndirilen piyasa verisi**

| piyasa | genişlik | derinlik | gözlem |
|---|---|---|---|
| BingX perpetual | 1.216 kontrat tarandı, 967'sinde canlı kitap | 372 sembol × 999 günlük bar (2,7 yıl) | 371.628 |
| BingX perpetual | 196 sembol | her biri 3.996 saatlik bar (166 gün) | 783.216 |
| BingX perpetual | 89 sembol | her biri 11.988 beş-dakikalık bar (41 gün) | 1.066.932 |
| BingX perpetual | 150 sembol | 1dk / 5dk / 15dk / 30dk / 4sa panelleri | ~2.100.000 |
| Borsa İstanbul | 136 hisse | 2.543 günlük bar (10 yıl) | 345.848 |
| Borsa İstanbul | 136 hisse | saatlik, 2 yıl | 68.257 |
| çok varlıklı, TL bazında | BTC ETH XU100 ALTIN SP500 GÜMÜŞ USDTRY | 10 yıl günlük | 22.603 |

Toplam yaklaşık **4,7 milyon mum.**

**Üzerinde yapılan çalışmalar**

| çalışma | test edilen | örneklem |
|---|---|---|
| indikatör atfı | botun 24 indikatörünün hepsi | 93.193 yönlü oy |
| gün içi sinyal haritası | 9 sinyal × 4 ufuk | 772.150 saatlik gözlem |
| konfluans testi | oy sayısı → ileri getiri | 767.446 gözlem |
| çok zaman dilimli harita | 14 sinyal × 7 zaman dilimi | 1 dakikadan günlüğe |
| kural taraması | 1.008 bağımsız kural | fit/test ayrımıyla |
| parametre taraması | 16 konfigürasyon | fit/test ayrımıyla |
| açılış boşluğu çalışması | 30 kural + artifakt denetimi | 68.104 hisse-gün |
| BIST teknik kuralları | 12 kural + literatürden 5 etki | 326.040 hisse-gün |
| kesitsel seçim | 9 sinyal × 5 devir ayarı | 372 sembol |
| tam bot replay'i | botun kendi kodu, bar bar | 11.885 tur |

**Uygulanan disiplin**

- her ileri getiri **kesitsel olarak ortalamadan arındırıldı** — kripto da BIST
  de kendi pencerelerinde yükseldi, bu yapılmazsa her long sinyali yetenekli
  görünür
- eşik **|t| ≥ 3,0**, 2,0 değil (Harvey, Liu & Zhu) — çok sayıda kural
  denendiği için
- **ilk yarıda ayarla, ikinci yarıda ölç**, ikisi de birlikte raporlanır
- ayakta kalanlara ayrıca **genişlik** (en iyi 10 sembolü at) ve **istikrar**
  (ay ay) testi uygulanır
- maliyetler canlı BingX kitabından ve BIST kademe tablosundan alındı,
  varsayılmadı

Sonucun negatif olması bu ölçütlerin uygulanmasından kaynaklanmaktadır. Daha
gevşek bir ilk turda dört ayrı bulgu ortaya çıkmış, dördü de yakın incelemede
geçersiz kalmıştır: şok dönüşü (birinci yıl t=+3,66, ikinci yıl −0,71),
hareketli ortalama filtreleri (geleceğe bakma hatası), düşüş freni (özkaynak
yerine fiyata bakması gereken yeniden giriş mantığı) ve tier-C trend takibi
(evren nokta-zaman seçilince t=+3,48'den +0,96'ya düşmüştür).

Analiz kodundaki iki hata da sessizce düzeltilmek yerine kayda geçirilmiştir:
TP ve SL değerlerinin ters bağlanması (replay'in −%99,9 raporlamasına yol
açmıştı) ve likidite sıralamasının TL enflasyonundan etkilenmesi (bir sonucu
tamamen tersine çevirmişti).

### Kaybın yapısal nedeni

Her alım-satım işlemi sabit bir maliyet doğurur (BingX'te yaklaşık %0,16).
Fiyatın beş dakikada kat ettiği mesafe bu maliyetin altında, bir günde kat
ettiği mesafe ise üzerindedir. Göstergenin niteliği bu ilişkiyi değiştirmez.

```
 1 saat tutarsan   → işlem başına -%0,131
 3 saat tutarsan   → işlem başına -%0,073
 8 saat tutarsan   → işlem başına +%0,060
24 saat tutarsan   → işlem başına +%0,595
```

Bu tabloda hiçbir gösterge kullanılmamaktadır; giriş noktaları rastgeledir.
Değişen tek parametre tutma süresidir.

---

## 3. Ölçümden geçen tek yaklaşım

Test edilenler arasında pozitif sonuç veren tek yöntem varlık tutmak oldu:

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

## 4. Depo yapısı

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

### Kurulum ve çalıştırma

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

## 5. Uyarılar

**Deneme bütçesi sınırlıdır.** Bailey ve López de Prado'nun asgari backtest
uzunluğu sonucuna göre, iki yıllık veriyle yaklaşık yedi bağımsız yapılandırma
denenebilir; bu sayının ötesinde, örneklem içinde kazanıp dışında hiçbir değer
üretmeyen bir sonuç elde etmek kaçınılmaz hale gelir. Bu depoda elliden fazla
kural denenmiştir. Dolayısıyla buradaki hiçbir rakam kesinleşmiş bir ölçüm
değil, ileriye doğru doğrulanması gereken bir hipotezdir.

**Anlamlılık eşiği t > 3,0'dır, 2,0 değil** (Harvey, Liu & Zhu). Aynı veri
üzerinde çok sayıda kural denendiğinde beklenen en yüksek t değeri, hiçbir
yetenek olmasa dahi yükselir. `research/bist/referee.py` bu nedenle 3,0
kullanmaktadır.

**Backtest sonuçları tek başına delil sayılmamalıdır.** Bu projede güçlü
görünüp incelemede geçersiz kalan dört bulgunun ayrıntısı bölüm 2'dedir;
analiz kodundaki iki hata da aynı yerde kayıtlıdır.

---

## 6. İlgili belgeler

| dosya | içerik |
|---|---|
| `RESEARCH-SYNTHESIS.md` | tüm ölçümlerin sentezi, kripto + BIST |
| `POSTMORTEM.md` | botun ilk halinin neden para kaybettiği |
| `CHANGELOG-FIXES.md` | düzeltilen 6 hata, tek tek |
| `FINDINGS.md` | ileri getiri çalışmaları |
| `research/bist/FINDINGS-BIST.md` | BIST ölçümleri |

---

## 7. Değerlendirme

Bu depo bir kazanç aracı değil, bir ölçüm aracıdır.

Pratik değeri, bir stratejinin işe yaramayacağını sermaye riske atmadan ve
saatler içinde ortaya koyabilmesinde. Çalışma sürecinde beş ayrı yaklaşım bu
şekilde elendi; her biri makul görünüyordu, hiçbiri ölçüm eşiğini geçemedi.

Pozitif sonuç veren tek yaklaşımın ne olduğu da ölçüldü: sık işlem yapmak
değil, pozisyonda kalmak.
