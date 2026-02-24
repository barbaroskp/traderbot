# Analiz ve Hesaplama İyileştirmeleri

**Amaç:** Agresifliği (pozisyon büyüklüğü, risk seviyeleri, TP/SL değerleri) değiştirmeden, **hesaplama tutarlılığını** ve **analiz kalitesini** artırmak.

---

## 1. Şu Anki Bot – Analiz Açısından Özet

### Doğru Olanlar
- **realised_pnl** artık fee’li (giriş + çıkış) hesaplanıyor (paper + live + backtest).
- **Balance:** Paper’da `initial + sum(realised_pnl)`; live’da exchange’den sync.
- **Risk manager:** Kelly ve drawdown, fee’li PnL üzerinden çalışıyor.
- **Özet (get_summary):** Açık pozisyonlar ve kapanan işlemler moda göre (paper/live) filtreleniyor.
- **Backtest raporu:** Equity curve, drawdown, Sharpe, profit factor, exit reason dağılımı mevcut.

### Eksik / Tutarsız Kalan Noktalar (düzeltildi)
1. **Reconcile (scheduler):** Exchange’te pozisyon yok ama DB’de “açık” kalmış satır kapatılırken PnL **fee’siz** hesaplanıyordu → **düzeltildi:** aynı formül (entry_fee + exit_fee) uygulanıyor.
2. **Günlük PnL (record_daily_pnl):** “Bugünkü işlemler” sorgusu hep `is_paper=1` kullanıyordu → Live modda günlük istatistik yanlış (sadece paper sayılıyordu). Ayrıca açık pozisyonlar için `get_open_positions()` moda göre çağrılmıyordu → **düzeltildi:** Hem bugünkü kapanan işlemler hem unrealised hesabı için `cfg.paper_mode` kullanılıyor.

---

## 2. Yapılan Hesaplama Düzeltmeleri (agresiflik aynı)

| Yer | Sorun | Düzeltme |
|-----|--------|----------|
| **scheduler (reconcile)** | DB’de açık, borsada kapalı pozisyon kapatılırken `pnl = brüt` (fee yok) | `pnl -= entry_fee + exit_fee` (config’teki `fee_rate_bps` ile) |
| **portfolio.record_daily_pnl** | Bugünkü işlemler hep `is_paper=1`; açık pozisyonlar default paper | Bugünkü kapananlar ve açık pozisyonlar için `is_paper = cfg.paper_mode` kullanılıyor |

Bunlar **sadece hesaplama ve raporlama**; strateji, risk limitleri, kaldıraç, TP/SL değerleri değişmedi.

---

## 3. İleride Yapılabilecek Analiz İyileştirmeleri (opsiyonel)

- **Kapalı pozisyona ek alanlar:** `exit_reason`, `exit_price`, isteğe bağlı `entry_fee`, `exit_fee` — böylece “TP/SL/timeout’a göre kâr/zarar” veya “toplam fee” analizi DB’den yapılabilir.
- **Backtest’te kısmi TP:** Şu an backtest’te tek seferde tam kapanış var; kısmi TP simüle edilirse backtest sonuçları live’a daha yakın olur.
- **Equity curve (paper/live):** Her cycle’da `balance + total_unrealised` bir tabloya yazılırsa, drawdown ve equity grafiği sonradan çizilebilir.
- **Live fill kaydı:** Gerçek emir doldurulunca `fills` tablosuna da yazmak; borsa mutabakatı ve fee raporu için faydalı olur.

Bu maddeler şu anki değişikliklere dahil değil; istersen sonra eklenebilir.

---

## 4. Özet

- **Agresiflik:** Aynı (strateji parametreleri, risk state eşikleri, pozisyon büyüklüğü, TP/SL).
- **Hesaplama:** Reconcile ve günlük PnL artık fee’li ve moda uyumlu; sayılar daha tutarlı ve analiz için daha doğru.
