# TradeBot – Binance USDⓈ-M Vadeli İşlem Botu

Trend takip stratejisiyle çalışan, komisyon/fonlama maliyetlerini hesaba katan, risk yönetimli bir vadeli işlem botu.
Üç çalışma modu vardır:

| Mod | Ne yapar | Gerekli |
|-----|----------|---------|
| **demo** | Canlı Binance fiyatıyla kağıt-para simülasyonu (komisyon, kayma, fonlama dahil) | Hiçbir şey (API anahtarı gerekmez) |
| **testnet** | Binance Futures Testnet'te sahte bakiyeyle gerçek emir akışı | testnet.binancefuture.com anahtarı |
| **live** | Gerçek hesapta gerçek para | Binance API anahtarı (Futures yetkili) + arayüzde onay kutusu |

## Kurulum ve çalıştırma

```bash
pip3 install -r requirements.txt
python3 run.py
```

Ardından tarayıcıda `http://127.0.0.1:8080` adresini açın. Arayüz sadece bu bilgisayardan erişilebilir.
İlk açılışta bot **demo** modundadır; "Başlat" ile hemen kağıt para ile test edebilirsiniz.

API anahtarlarını arayüzdeki **Ayarlar > Mod & API** bölümünden girin. Anahtarlar proje kökündeki `config.json`
dosyasına yazılır (bu dosya `.gitignore`'dadır) ve arayüze asla geri gönderilmez.

Komut satırından backtest:

```bash
python3 backtest.py --symbols BTCUSDT,ETHUSDT --interval 1h --days 365
```

## Evren (hangi coinler)

- `universe.auto` açıkken 24 saatlik USDT hacmine göre en likit **20** perpetual seçilir ve saatte bir yenilenir.
- Elenenler: stablecoin çiftleri, 90 günden yeni listelenenler, o gün %30'dan fazla hareket edenler (pompa/çöküş).
- Açık pozisyonu olan coin evrenden düşse bile bot onu izlemeye devam eder.
- `universe.auto` kapalıysa `symbols` listesi kullanılır.

## Strateji v2 (varsayılan)

Sinyal yalnızca **kapanmış** mumda üretilir; geriye bakma (lookahead) yoktur. Canlı bot ve backtest aynı kodu kullanır.

**Trend filtreleri (hepsi sağlanmalı)**
- EMA21 > EMA200 ve fiyat EMA200 üstünde (long) / tersi (short), +DI > -DI, ADX ≥ eşik
- Üst zaman dilimi onayı (`htf_filter`): 15m için 4h, 1h için 4h, 5m için 1h grafikte EMA50 > EMA200 ve fiyat EMA200 üstünde
- RSI aşırı bölgede giriş yok, ATR/fiyat yüzdesi `atr_pct_min`–`atr_pct_max` bandında olmalı
- Hacim onayı: tetik mumunun hacmi 20 mum ortalamasının 1,2 katından fazla

**Giriş tetikleyicileri (`entry_mode`)**: `cross` EMA9/21 kesişimi, `pullback` EMA21'e geri çekilip toparlama,
`breakout` 20 mum yüksek/düşük kırılımı, `any` üçünden herhangi biri.

**Giriş emri**: `limit` sinyal kapanış fiyatından maker limit emir (komisyon %0,02), `limit_timeout_bars` mum içinde
dolmazsa iptal. `market` anında taker (%0,05).

**Çıkışlar**
- Stop: `sl_atr` × ATR, hedef: `tp_atr` × ATR
- Kısmi kâr: 1R'de pozisyonun yarısı kapatılır, stop başabaş + komisyona çekilir
- İz süren stop: `trail_activate_atr` kâra geçince `trail_atr` mesafeyle izler
- Zaman stopu: `max_bars_in_trade` mum sonra hâlâ açıksa kapatılır
- Komisyon kapısı: hedef kâr, gidiş-dönüş komisyonun `min_tp_fee_multiple` katından azsa işlem yok

## Risk yönetimi

- Pozisyon büyüklüğü stop mesafesine göre: stop olursa kayıp = equity'nin **%1'i**
- Tek pozisyon marjini ≤ equity'nin %30'u, tüm pozisyonların toplam marjini ≤ %60, kaldıraç **3x**, izole marjin
- En fazla 6 açık pozisyon, **aynı yönde en fazla 3** (kripto korelasyonu yüksek; 6 long = tek büyük bahis)
- Günlük gerçekleşen zarar %3'ü aşarsa gün sonuna kadar yeni işlem yok; 3 ardışık kayıpta 120 dk soğuma
- Canlı/testnet modda stop ve hedef emirleri **borsaya yerleştirilir** (Binance Algo Order API üzerinden
  STOP_MARKET / TAKE_PROFIT_MARKET, mark price tetikli; eski `/fapi/v1/order` ucu koşullu emirleri artık kabul etmiyor);
  bot kapansa bile pozisyon korumasız kalmaz. Koruma emri konamazsa pozisyon anında kapatılır.

## Komisyon ve maliyetler

- Varsayılan: taker %0,05, maker %0,02 (Binance USDⓈ-M normal kullanıcı). BNB indirimi seçilebilir.
- Limit girişte maker, tüm çıkışlarda (stop/hedef/kısmi/zaman) taker komisyon uygulanır.
- Canlı modda gerçek komisyon `userTrades`, fonlama `income` uçlarından okunur.
- Demo modda komisyon, kayma (2 bps) ve 8 saatlik fonlama gerçek orana göre simüle edilir.
- Backtest: komisyon, kayma, tahmini fonlama (%0,01 / 8 saat) dahildir. Limit emir yalnızca fiyat limite
  gelirse dolmuş sayılır; aynı mumda hem stop hem hedef değmişse **stop** varsayılır (kötümser).
- Mum verisi `data/cache/` altında önbelleklenir; tekrar eden backtestler hızlıdır.

## Backtest sonuçları

20 coin (otomatik evren), 1 saatlik grafik, 1000 USDT, 3x, komisyon + kayma + fonlama dahil.
İlk 6 ay ayar dönemi (in-sample), son 6 ay doğrulama (out-of-sample); 192 kombinasyon tarandı.

| Ayar | İlk 6 ay | Son 6 ay | Tam yıl | İşlem/yıl | Maks. düşüş | Komisyon |
|------|----------|----------|---------|-----------|-------------|----------|
| **Varsayılan:** kırılım + HTF onayı + limit giriş + kısmi kâr, SL 2×ATR / TP 4×ATR, ADX ≥ 25 | +%8,6 | +%11,9 | +%20,6 | 421 | %15,7 | 134 USDT |
| Aynısı, kısmi kâr kapalı | +%11,0 | +%14,9 | +%23,9 | 414 | %17,9 | 138 USDT |
| Aynısı, market giriş | +%13,5 | +%7,0 | +%18,5 | 416 | %18,8 | 193 USDT |
| Aynısı, HTF onayı kapalı | +%0,3 | +%8,2 | +%24,8 | 539 | %20,9 | 184 USDT |
| EMA kesişim + HTF + limit + kısmi, SL 2/TP 4 | −%0,4 | +%11,3 | +%10,8 | 88 | %9,4 | 24 USDT |
| "Hepsi" modu (geri çekilme dahil) | +%18,4 | **−%42,5** | −%31,5 | 1144 | %62 | 476 USDT |
| 15 dakikalık grafik, her varyant | negatif | −%23 … −%84 | negatif | 1000+ | %43+ | 300+ USDT |

Çıkarımlar:
- Üst zaman dilimi onayı, ilk 6 aydaki (trendsiz dönem) kayıpları ortadan kaldıran tek filtre.
- Limit giriş, market girişe göre komisyonu üçte bir azaltıyor ve dolmayan emirler bunu geri almıyor.
- Geri çekilme tetikleyicisi ("any" modu) ilk yarıda çok iyi görünüp ikinci yarıda çöküyor: klasik ezberleme örneği, bu yüzden varsayılan değil.
- 15 dakikalık grafik bu strateji ailesinde her ayarda zarar ediyor; komisyon işlem sayısıyla büyüyor.
- Daha az işlem, daha düşük düşüş isteyen için EMA kesişim modu (yılda ~90 işlem, %9 düşüş) uygun alternatif.

Bu rakamlar geçmiş veriye dayanır ve gelecekteki performansı garanti etmez. Gerçek paraya geçmeden önce
en az birkaç hafta demo/testnet'te çalıştırın.

## Dosyalar

```
run.py               web arayüzü + bot
backtest.py          komut satırı backtest
bot/config.py        varsayılan ayarlar, config.json okuma/yazma
bot/binance_client.py Binance Futures REST istemcisi (live/testnet)
bot/indicators.py    EMA, RSI, ATR, ADX (saf Python)
bot/strategy.py      sinyal üretimi ve iz süren stop (canlı ve backtest ortak)
bot/risk.py          pozisyon boyutu, günlük zarar limiti, soğuma
bot/paper_engine.py  demo borsa simülasyonu
bot/live_engine.py   gerçek/testnet emir yönetimi
bot/trader.py        ana döngü
bot/backtester.py    geçmiş veri simülasyonu (walk-forward için end_ms parametresi)
bot/web.py           Flask API
templates/, static/  arayüz
data/tradebot.db     işlem geçmişi ve bot durumu (SQLite)
```

## Coolify / Docker ile çalıştırma

Depoda `Dockerfile` ve `docker-compose.yml` var. Coolify'da:

1. **New Resource → Public/Private Repository**, depo `ardatknlp/tradebot`, dal `main`, Build Pack **Dockerfile**.
2. **Port:** 8080 (Dockerfile `PORT=8080` ile başlar; Coolify'ın verdiği `PORT` değişkeni de otomatik kullanılır).
3. **Persistent Storage:** konteyner yolu `/app/data` (SQLite işlem geçmişi, mum önbelleği ve arayüzden kaydedilen `config.json` burada durur; volume olmazsa her deploy'da sıfırlanır).
4. **Environment Variables** (anahtarları buraya girin, dosyaya değil; ortam değişkeni dosyadaki değeri ezer ve dosyaya asla yazılmaz):

| Değişken | Açıklama |
|---|---|
| `WEB_PASSWORD` | **Zorunlu.** Arayüz parolası (HTTP Basic Auth, kullanıcı adı boş). Boşsa konteyner başlamaz. |
| `TRADEBOT_MODE` | `demo`, `testnet` veya `live` |
| `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` | testnet için |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | gerçek hesap için |
| `LIVE_TRADING_CONFIRMED` | gerçek modda `true` olmalı |
| `TRADEBOT_AUTOSTART` | `true` ise konteyner açılınca bot hemen başlar. Ayrıca bot çalışırken konteyner yeniden başlarsa otomatik devam eder. |

5. Domain verip Coolify'ın HTTPS'ini açın; parola HTTP üzerinden düz metin gittiği için HTTPS şart.
6. Sağlık kontrolü: `GET /api/health` (parola istemez). Loglar Coolify'ın log ekranında (stdout).

Yerelde deneme: `WEB_PASSWORD=gizli docker compose up --build`

## Sunucuda çalıştırma (Docker'sız)

```bash
git clone https://github.com/ardatknlp/tradebot.git && cd tradebot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # anahtarları ve web.password değerini düzenleyin
python3 run.py
```

- `config.json` içinde `web.host` değerini `0.0.0.0` yaparsanız `web.password` **zorunludur**; arayüz HTTP Basic Auth ister
  (kullanıcı adı boş bırakılabilir). Parola yoksa program başlamaz.
- Daha güvenlisi: host'u `127.0.0.1` bırakıp SSH tüneliyle bağlanın: `ssh -L 8080:127.0.0.1:8080 kullanici@sunucu`
- Sürekli çalışması için systemd örneği (`/etc/systemd/system/tradebot.service`):

```ini
[Unit]
Description=TradeBot
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/tradebot
ExecStart=/home/ubuntu/tradebot/.venv/bin/python run.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now tradebot && journalctl -u tradebot -f
```

- Bot yeniden başladığında durumu (`data/tradebot.db`) ve borsadaki pozisyonları otomatik eşitler; koruma emirleri
  borsada olduğu için yeniden başlatma sırasında pozisyonlar korumasız kalmaz.
- Binance API anahtarına IP kısıtlaması koyun ve sunucunun IP'sini ekleyin.

## Notlar

- Hesap Binance'te **One-way (tek yön)** pozisyon modunda olmalı; hedge modunda bot başlamaz.
- API anahtarında sadece "Futures" yetkisi verin, para çekme yetkisi vermeyin, IP kısıtlaması koyun.
- Demo bakiyeyi "Demo bakiyeyi sıfırla" ile sıfırlayabilirsiniz (demo işlem geçmişi de silinir).
