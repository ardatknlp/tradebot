"""Konfigürasyon yükleme/kaydetme. config.json proje kökünde tutulur (git'e girmez)."""
import copy
import json
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("TRADEBOT_DATA_DIR") or os.path.join(ROOT, "data")
CONFIG_PATH = os.environ.get("TRADEBOT_CONFIG") or os.path.join(ROOT, "config.json")
_lock = threading.Lock()

# Ortam değişkeni -> ayar yolu. Docker/Coolify'da anahtarlar dosyaya değil buraya girilir.
ENV_MAP = {
    "TRADEBOT_MODE": ("mode",), "BINANCE_API_KEY": ("api_key",), "BINANCE_API_SECRET": ("api_secret",),
    "BINANCE_TESTNET_API_KEY": ("testnet_api_key",), "BINANCE_TESTNET_API_SECRET": ("testnet_api_secret",),
    "LIVE_TRADING_CONFIRMED": ("live_trading_confirmed",), "WEB_PASSWORD": ("web", "password"),
    "WEB_HOST": ("web", "host"), "PORT": ("web", "port"), "WEB_PORT": ("web", "port"),
}


def _set_path(d, path, value):
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = value


def _env_overrides():
    out = {}
    for env, path in ENV_MAP.items():
        v = os.environ.get(env)
        if v is None or v == "":
            continue
        if path[-1] == "live_trading_confirmed":
            v = v.strip().lower() in ("1", "true", "yes", "evet")
        elif path[-1] == "port":
            try:
                v = int(v)
            except ValueError:
                continue
        _set_path(out, path, v)
    return out


def env_overridden_paths():
    return [path for env, path in ENV_MAP.items() if os.environ.get(env)]

DEFAULTS = {
    # demo  : bot içi kağıt-para simülasyonu (API anahtarı gerekmez, canlı fiyat kullanır)
    # testnet: Binance Futures testnet (sahte bakiye, gerçek emir akışı) -> testnet anahtarı gerekir
    # live  : GERÇEK PARA
    "mode": "demo",
    "api_key": "",
    "api_secret": "",
    "testnet_api_key": "",
    "testnet_api_secret": "",
    "live_trading_confirmed": False,

    "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],   # universe.auto kapalıysa kullanılır
    "universe": {
        "auto": True,                 # hacme göre en likit N perpetual'ı otomatik seç
        "top_n": 20,
        "min_quote_volume_usdt": 100_000_000,   # 24 saatlik USDT hacmi alt sınırı
        "min_listing_days": 90,       # en az bu kadar gündür listede olsun
        "max_change_pct": 30.0,       # 24 saatlik değişimi bundan büyük (pompalanan) coinleri alma
        "exclude": ["USDCUSDT", "FDUSDUSDT", "BUSDUSDT", "TUSDUSDT", "USDPUSDT", "EURUSDT", "USD1USDT", "DAIUSDT"],
        "refresh_minutes": 60,
    },
    "interval": "1h",
    "poll_seconds": 15,

    "demo": {
        "start_balance": 1000.0,
        "slippage_bps": 2.0,          # simüle edilen kayma (1 bps = %0.01)
        "apply_funding": True,        # fonlama ücretini demo'da da uygula
    },

    "fees": {
        "maker": 0.0002,              # %0.02
        "taker": 0.0005,              # %0.05 (Binance USDⓈ-M normal kullanıcı)
        "bnb_discount": False,        # BNB ile ödemede %10 indirim
    },

    "strategy": {
        "entry_mode": "breakout",     # cross | pullback | breakout | any (hepsi)
        "htf_filter": True,           # üst zaman dilimi (örn. 15m için 4h) trend onayı
        "htf_interval": "",           # boş = otomatik eşleme
        "volume_filter": True,        # tetik mumunda hacim > volume_mult x 20 mum ortalaması
        "volume_mult": 1.2,
        "atr_pct_min": 0.2,           # ATR / fiyat yüzdesi alt sınır (çok sakin piyasada işlem yok)
        "atr_pct_max": 4.0,           # üst sınır (aşırı volatil piyasada işlem yok)
        "max_bars_in_trade": 48,      # zaman stopu: bu kadar mum sonra hâlâ açıksa kapat (0 = kapalı)
        "partial_tp": True,           # 1R'de pozisyonun yarısını kapat, stop'u başabaşa çek
        "partial_r": 1.0,
        "partial_fraction": 0.5,
        "entry_order": "limit",       # limit (maker, ucuz) | market (taker, garanti dolum)
        "limit_offset_bps": 0.0,      # limit fiyatı sinyal kapanışından bu kadar daha iyi konur
        "limit_timeout_bars": 1,      # dolmazsa bu kadar mum sonra iptal
        "breakout_bars": 20,
        "ema_fast": 9,
        "ema_slow": 21,
        "ema_trend": 200,
        "adx_period": 14,
        "adx_min": 25.0,
        "rsi_period": 14,
        "rsi_max_long": 70.0,
        "rsi_min_short": 30.0,
        "atr_period": 14,
        "sl_atr": 2.0,                # stop mesafesi = 2 x ATR
        "tp_atr": 4.0,                # hedef = 4 x ATR  (R:R = 1:2)
        "trail_activate_atr": 2.0,    # 2 ATR kâra geçince iz süren stop devreye girer
        "trail_atr": 3.0,             # iz süren stop mesafesi
        "min_tp_fee_multiple": 4.0,   # hedef kâr, gidiş-dönüş komisyonun en az 4 katı olmalı
        "allow_short": True,
        "allow_long": True,
    },

    "risk": {
        "leverage": 3,
        "margin_type": "ISOLATED",
        "risk_per_trade_pct": 1.0,    # işlem başına maksimum kayıp (stop'ta) = equity'nin %1'i
        "max_position_pct": 30.0,     # tek pozisyona ayrılan marjin en fazla equity'nin %30'u
        "max_open_positions": 6,
        "max_positions_per_side": 3,  # aynı yönde (kripto korelasyonu yüksek) en fazla N pozisyon
        "max_total_margin_pct": 60.0, # tüm pozisyonların toplam marjini equity'nin en fazla %60'ı
        "max_daily_loss_pct": 3.0,    # günlük zarar bu eşiği aşarsa gün sonuna kadar dur
        "max_consecutive_losses": 3,
        "cooldown_minutes": 120,
    },

    "web": {"host": "127.0.0.1", "port": 8080, "password": ""},   # host 0.0.0.0 yapılacaksa password ZORUNLU
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    with _lock:
        data = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = {}
        return _merge(_merge(DEFAULTS, data), _env_overrides())


def save(cfg):
    """Ortam değişkeninden gelen değerler (gizli anahtarlar) dosyaya yazılmaz."""
    cfg = copy.deepcopy(cfg)
    for path in env_overridden_paths():
        d = cfg
        for k in path[:-1]:
            d = d.get(k, {})
        d.pop(path[-1], None)
    with _lock:
        os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)


def round_trip_fee(cfg, taker_in=True, taker_out=True):
    """Gidiş + dönüş komisyon oranı (notional'a göre)."""
    fees = cfg["fees"]
    f_in = fees["taker"] if taker_in else fees["maker"]
    f_out = fees["taker"] if taker_out else fees["maker"]
    total = f_in + f_out
    if fees.get("bnb_discount"):
        total *= 0.9
    return total
