"""Ana bot döngüsü: evren seç -> veri çek -> bekleyen emirler -> pozisyon yönetimi -> sinyal -> emir."""
import threading
import time
import traceback
from collections import deque
from datetime import datetime

from . import config as cfgmod
from .binance_client import BinanceFutures, BinanceError, INTERVAL_MS
from .live_engine import LiveExchange
from .paper_engine import PaperExchange
from .risk import AccountGuard, position_size
from .strategy import (compute_indicators, signal_at, update_trailing, min_bars, htf_interval,
                       htf_bias_series, htf_bias_at, breakeven_price, partial_tp_price)


def fp(x):
    """Fiyatı anlamlı basamakla yaz (düşük fiyatlı coinlerde 0.0009 yerine 0.0009405)."""
    if x is None:
        return "-"
    x = float(x)
    if x >= 100:
        return f"{x:.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.7g}"


class Trader:
    def __init__(self, storage):
        self.storage = storage
        self.cfg = cfgmod.load()
        self.logs = deque(maxlen=600)
        self.running = False
        self.thread = None
        self.thread_gen = 0
        self.lock = threading.RLock()
        self.prices = {}
        self.funding = {}
        self.last_candle = {}
        self.snapshot = {}
        self.htf_cache = {}          # symbol -> {"series": [...], "fetched_close": int}
        self.symbols = list(self.cfg["symbols"])
        self.blacklist = set()       # bu oturumda işlem açılamayan semboller (örn. -4140 işlem dışı)
        self.universe_refreshed = 0.0
        self.equity_history = deque(maxlen=1500)
        self.last_error = None
        self.started_at = None
        self.public = BinanceFutures()
        self.exchange = None
        self.client = None
        self.guard = AccountGuard(self.cfg["risk"])
        self._build_exchange()
        self.log(f"Bot hazır. Mod: {self.cfg['mode'].upper()}")

    # ---------- yardımcı ----------
    def log(self, msg):
        line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
        self.logs.append(line)
        print(line, flush=True)

    @property
    def mode(self):
        return self.cfg["mode"]

    def _build_exchange(self):
        mode = self.cfg["mode"]
        if mode == "demo":
            self.client = self.public
            self.exchange = PaperExchange(self.cfg)
        else:
            testnet = mode == "testnet"
            key = self.cfg["testnet_api_key"] if testnet else self.cfg["api_key"]
            sec = self.cfg["testnet_api_secret"] if testnet else self.cfg["api_secret"]
            self.client = BinanceFutures(key, sec, testnet=testnet)
            self.exchange = LiveExchange(self.client, self.cfg, mode, self.log)
        self.exchange.load_state(self.storage.get(f"state:{mode}"))
        self.guard = AccountGuard(self.cfg["risk"])
        g = self.storage.get(f"guard:{mode}")
        if g:
            self.guard.daily_realized = g.get("daily_realized", 0.0)
            self.guard.cooldown_until = g.get("cooldown_until", 0.0)
            self.guard.consecutive_losses = g.get("consecutive_losses", 0)
        self.equity_history = deque(self.storage.get(f"equity:{mode}", []), maxlen=1500)
        self.last_candle = {}
        self.htf_cache = {}
        self.universe_refreshed = 0.0
        self.symbols = list(self.cfg["symbols"])

    def reload_config(self):
        with self.lock:
            old_mode = self.cfg["mode"]
            self.cfg = cfgmod.load()
            if self.running:
                if self.cfg["mode"] != old_mode:
                    self.cfg["mode"] = old_mode
                    self.log("Mod değişikliği kaydedildi; bot durdurulup yeniden başlatılınca uygulanacak.")
                self.guard.rcfg = self.cfg["risk"]
                self.exchange.cfg = self.cfg
                self.universe_refreshed = 0.0
                self.last_candle = {}
                self.htf_cache = {}
            else:
                self._build_exchange()
            self.log("Ayarlar yüklendi.")

    # ---------- kontrol ----------
    def start(self):
        with self.lock:
            if self.running:
                return False, "zaten çalışıyor"
            # diskteki ayarları uygula (mod değişmiş olabilir)
            fresh = cfgmod.load()
            if fresh["mode"] != self.cfg["mode"] or self.exchange is None:
                self.cfg = fresh
                self._build_exchange()
                self.log(f"Mod değiştirildi: {self.cfg['mode'].upper()}")
            else:
                self.cfg = fresh
                self.exchange.cfg = self.cfg
                self.guard.rcfg = self.cfg["risk"]
            mode = self.cfg["mode"]
            if mode == "live":
                if not self.cfg.get("live_trading_confirmed"):
                    return False, "Gerçek mod için ayarlardan onay kutusunu işaretleyin."
                if not (self.cfg["api_key"] and self.cfg["api_secret"]):
                    return False, "Gerçek mod için API anahtarı ve secret gerekli."
            if mode == "testnet" and not (self.cfg["testnet_api_key"] and self.cfg["testnet_api_secret"]):
                return False, "Testnet için testnet API anahtarı gerekli."
        # Bağlantı kontrolleri kilit dışında ve sert süre sınırıyla (DNS/ağ takılmasında arayüz kilitlenmesin)
        result = {}

        def _check():
            try:
                self.client.sync_time()
                if mode != "demo":
                    if self.client.is_hedge_mode():
                        result["err"] = "Hesap Hedge modunda. Binance'te One-way (tek yön) moda alın."
                        return
                    self.client.balance_usdt()
                result["ok"] = True
            except Exception as e:  # noqa
                result["err"] = f"Bağlantı hatası: {e}"

        th = threading.Thread(target=_check, daemon=True)
        th.start()
        th.join(20.0)
        if th.is_alive():
            self.log("Başlatma: bağlantı kontrolü 20 sn içinde yanıt vermedi")
            return False, "Bağlantı 20 sn içinde yanıt vermedi; ağı kontrol edip tekrar deneyin."
        if "err" in result:
            self.log(f"Başlatma başarısız: {result['err']}")
            return False, result["err"]
        with self.lock:
            if self.running:
                return False, "zaten çalışıyor"
            self.running = True
            self.started_at = time.time()
            self.thread_gen += 1
            self.thread = threading.Thread(target=self._loop, args=(self.thread_gen,), daemon=True)
            self.thread.start()
            self.storage.set("autostart", True)
            self.log(f"Bot BAŞLATILDI ({mode.upper()}, {self.cfg['interval']}, giriş: {self.cfg['strategy'].get('entry_order', 'limit')})")
            return True, "başlatıldı"

    def stop(self):
        with self.lock:
            if not self.running:
                return False, "çalışmıyor"
            self.running = False
            self.thread_gen += 1
            self.storage.set("autostart", False)
        self.log("Bot durduruldu (açık pozisyonlar ve borsadaki koruma emirleri korunuyor).")
        return True, "durduruldu"

    def reset_demo(self):
        with self.lock:
            if self.mode != "demo":
                return False, "sadece demo modunda"
            if self.running:
                return False, "önce botu durdurun"
            self.exchange.reset()
            self.storage.clear_trades("demo")
            self.storage.set("state:demo", None)
            self.storage.set("guard:demo", None)
            self.storage.set("equity:demo", [])
            self.equity_history.clear()
            self.guard = AccountGuard(self.cfg["risk"])
            self.log("Demo bakiye sıfırlandı.")
            return True, "sıfırlandı"

    def close_position(self, symbol):
        with self.lock:
            if symbol in self.exchange.pending:
                self.exchange.cancel_pending(symbol)
                self.log(f"[{symbol}] bekleyen emir iptal edildi (manuel)")
                return True, "emir iptal edildi"
            if symbol not in self.exchange.positions:
                return False, "pozisyon yok"
            price = self.prices.get(symbol) or self.public.premium_index(symbol)["mark_price"]
            try:
                t = self.exchange.close(symbol, price, "MANUEL")
            except Exception as e:  # noqa
                return False, str(e)
            self._record(t)
            return True, "kapatıldı"

    # ---------- döngü ----------
    def _loop(self, gen):
        while self.running and gen == self.thread_gen:
            t0 = time.time()
            try:
                with self.lock:
                    if not (self.running and gen == self.thread_gen):
                        break
                    self._tick()
                self.last_error = None
            except Exception as e:  # noqa
                self.last_error = str(e)
                self.log(f"HATA: {e}")
                if "-1003" not in str(e):
                    traceback.print_exc()
            ban = max(self.client.banned_until, self.public.banned_until) - time.time()
            if ban > 0:
                wait_s = min(60.0, max(3.0, ban + 1))
                self.log(f"İstek limiti: {wait_s:.0f} sn bekleniyor (ağırlık: veri {self.public.used_weight}, hesap {self.client.used_weight})")
                for _ in range(int(wait_s * 2)):
                    if not (self.running and gen == self.thread_gen):
                        break
                    time.sleep(0.5)
            elapsed = time.time() - t0
            wait = max(1.0, self.cfg["poll_seconds"] - elapsed)
            while wait > 0 and self.running and gen == self.thread_gen:
                time.sleep(min(0.5, wait))
                wait -= 0.5
        if gen == self.thread_gen:
            self.log("Bot durdu.")

    def _record(self, trade):
        self.storage.add_trade(trade)
        self.guard.record_trade(trade.net_pnl)
        sign = "+" if trade.net_pnl >= 0 else ""
        self.log(f"[{trade.symbol}] {trade.side} KAPANDI ({trade.reason}) giriş {fp(trade.entry)} çıkış {fp(trade.exit)} "
                 f"net {sign}{trade.net_pnl:.2f} USDT (komisyon {trade.fees:.2f}, fonlama {trade.funding:.2f})")

    def _refresh_universe(self):
        u = self.cfg.get("universe", {})
        if not u.get("auto"):
            self.symbols = list(self.cfg["symbols"])
            return
        if time.time() - self.universe_refreshed < u.get("refresh_minutes", 60) * 60:
            return
        try:
            top = self.public.top_symbols(int(u.get("top_n", 20)), float(u.get("min_quote_volume_usdt", 1e8)),
                                          set(u.get("exclude", [])), int(u.get("min_listing_days", 90)),
                                          float(u.get("max_change_pct", 30)))
        except BinanceError as e:
            self.log(f"evren yenilenemedi: {e}")
            self.universe_refreshed = time.time() - u.get("refresh_minutes", 60) * 60 + 300
            return
        if self.mode != "demo":
            # emirlerin gittiği borsada (testnet/gerçek) işlem görmeyen sembolleri ele (örn. SETTLING)
            try:
                venue = {x["symbol"]: x.get("status") for x in self.client.exchange_info()["symbols"]}
                dropped = [x for x in top if venue.get(x) != "TRADING"]
                if dropped:
                    self.log(f"Evrenden elendi (borsada işlem dışı): {', '.join(dropped)}")
                top = [x for x in top if venue.get(x) == "TRADING"]
            except BinanceError as e:
                self.log(f"borsa sembol durumu alınamadı: {e}")
        held = [s for s in list(self.exchange.positions) + list(self.exchange.pending) if s not in top]
        new = top + held
        if new != self.symbols:
            self.log(f"Evren güncellendi ({len(new)}): {', '.join(new)}")
        self.symbols = new
        self.universe_refreshed = time.time()

    def _htf_bias(self, symbol, now_ms):
        scfg = self.cfg["strategy"]
        if not scfg.get("htf_filter"):
            return None
        iv = htf_interval(scfg, self.cfg["interval"])
        ms = INTERVAL_MS[iv]
        c = self.htf_cache.get(symbol)
        if c is None or now_ms > c["fetched_close"] + ms:
            candles = self.public.klines(symbol, iv, limit=260)
            closed = [x for x in candles if x.close_time <= now_ms]
            if len(closed) < 205:
                return 0
            c = {"series": htf_bias_series(closed), "fetched_close": closed[-1].close_time}
            self.htf_cache[symbol] = c
        return htf_bias_at(c["series"], now_ms)

    def _manage_position(self, symbol, price, scfg, fee_rt, now_ms):
        pos = self.exchange.positions.get(symbol)
        if not pos:
            return
        reason = self.exchange.check_exit(symbol, price)
        if reason:
            self._record(self.exchange.close(symbol, price, reason))
            return
        d = pos.direction
        # kısmi kâr alma
        ptp = partial_tp_price(pos, scfg)
        if ptp is not None and (price - ptp) * d >= 0:
            try:
                gross, fee = self.exchange.reduce(symbol, scfg.get("partial_fraction", 0.5), price)
                if gross or fee:
                    self.log(f"[{symbol}] kısmi kâr alındı: {gross - fee:+.2f} USDT, stop başabaşa çekiliyor")
                be = breakeven_price(pos, fee_rt)
                if (be - pos.stop) * d > 0:
                    self._set_stop(pos, be, price)
            except BinanceError as e:
                self.log(f"[{symbol}] kısmi kapanış hatası: {e}")
        # zaman stopu
        mb = int(scfg.get("max_bars_in_trade", 0) or 0)
        if mb:
            bars = int((now_ms / 1000 - pos.opened_at) / (INTERVAL_MS[self.cfg["interval"]] / 1000))
            if bars >= mb:
                self._record(self.exchange.close(symbol, price, "ZAMAN"))
                return
        # iz süren stop
        old_stop = pos.stop
        new_stop = update_trailing(pos, price, scfg, fee_rt)
        if new_stop is not None:
            pos.stop = old_stop
            self._set_stop(pos, new_stop, price)

    def _set_stop(self, pos, new_stop, price):
        if self.mode == "demo":
            pos.stop = new_stop
            self.log(f"[{pos.symbol}] stop -> {fp(new_stop)}")
            return
        if abs(new_stop - pos.stop) / price < 0.0005:
            return
        try:
            self.exchange.update_stop(pos, new_stop)
            self.log(f"[{pos.symbol}] borsa stop güncellendi -> {fp(pos.stop)}")
        except Exception as e:  # noqa
            self.log(f"[{pos.symbol}] stop güncellenemedi: {e}")

    def _tick(self):
        cfg = self.cfg
        scfg, rcfg = cfg["strategy"], cfg["risk"]
        entry_limit = scfg.get("entry_order", "limit") == "limit"
        fee_rt = cfgmod.round_trip_fee(cfg, taker_in=not entry_limit)
        now_ms = int(time.time() * 1000)
        step = INTERVAL_MS[cfg["interval"]]

        self._refresh_universe()
        for t in self.exchange.sync():
            self._record(t)

        try:
            allp = self.public.premium_index_all()
        except BinanceError as e:
            self.log(f"fiyatlar alınamadı: {e}")
            return

        for symbol in list(self.symbols):
            pi = allp.get(symbol)
            if not pi or (symbol in self.blacklist and symbol not in self.exchange.positions):
                continue
            price = pi["mark_price"]
            self.prices[symbol] = price
            self.funding[symbol] = pi["funding_rate"]
            paid = self.exchange.apply_funding(symbol, pi["funding_rate"], price, pi["next_funding_time"])
            if paid:
                self.log(f"[{symbol}] fonlama uygulandı: {-paid:+.4f} USDT")

            # bekleyen limit emri
            if symbol in self.exchange.pending:
                pos = self.exchange.poll_pending(symbol, price)
                if pos:
                    self.log(f"[{symbol}] {pos.side} limit emri DOLDU {pos.qty} @ {fp(pos.entry)} | stop {fp(pos.stop)} hedef {fp(pos.take_profit)}")
                elif symbol not in self.exchange.pending:
                    self.log(f"[{symbol}] limit emir dolmadı, iptal")

            # açık pozisyon
            self._manage_position(symbol, price, scfg, fee_rt, now_ms)

            # yeni mum var mı? (bir sonraki mum kapanmadan tekrar istek atma)
            last_close = self.last_candle.get(symbol, 0)
            if last_close and now_ms < last_close + step + 3000:
                continue
            limit = min(1000, min_bars(scfg) + 60)
            try:
                candles = self.public.klines(symbol, cfg["interval"], limit=limit)
            except BinanceError as e:
                self.log(f"[{symbol}] mum alınamadı: {e}")
                if "ağ hatası" in str(e):
                    break  # bağlantı sorunlu: bu tick'te diğer coinleri deneme
                continue
            closed = [c for c in candles if c.close_time <= now_ms]
            if len(closed) < min_bars(scfg):
                continue
            last = closed[-1]
            if last.close_time == last_close:
                continue
            first_run = last_close == 0
            self.last_candle[symbol] = last.close_time
            ind = compute_indicators(closed, scfg)
            i = len(closed) - 1
            try:
                bias = self._htf_bias(symbol, now_ms)
            except BinanceError as e:
                self.log(f"[{symbol}] HTF verisi alınamadı: {e}")
                if "ağ hatası" in str(e):
                    break
                bias = 0
            self.snapshot[symbol] = {
                "close": last.close, "rsi": ind["rsi"][i], "adx": ind["adx"][i], "atr": ind["atr"][i],
                "trend": "YUKARI" if (ind["ema_slow"][i] or 0) > (ind["ema_trend"][i] or 0) else "AŞAĞI",
                "htf": {1: "↑", -1: "↓"}.get(bias, "–") if bias is not None else "",
                "candle_time": last.close_time,
            }
            if first_run or symbol in self.exchange.positions or symbol in self.exchange.pending:
                continue
            sig = signal_at(closed, ind, i, scfg, fee_rt, htf_bias=bias)
            if not sig:
                continue
            # --- risk kapıları ---
            equity = self.exchange.equity(self.prices)
            open_n = len(self.exchange.positions) + len(self.exchange.pending)
            ok, why = self.guard.can_open(equity, open_n)
            if not ok:
                self.log(f"[{symbol}] sinyal {sig.side} atlandı: {why}")
                continue
            same_side = sum(1 for p in self.exchange.positions.values() if p.side == sig.side) + \
                sum(1 for o in self.exchange.pending.values() if o.side == sig.side)
            if same_side >= rcfg.get("max_positions_per_side", 99):
                self.log(f"[{symbol}] sinyal {sig.side} atlandı: aynı yönde maksimum pozisyon")
                continue
            if self.exchange.used_margin() >= equity * rcfg.get("max_total_margin_pct", 100) / 100:
                self.log(f"[{symbol}] sinyal {sig.side} atlandı: toplam marjin limiti")
                continue
            try:
                filters = self.client.symbol_filters(symbol) if self.mode != "demo" else self.public.symbol_filters(symbol)
            except BinanceError as e:
                self.log(f"[{symbol}] filtre alınamadı: {e}")
                continue
            lim = None
            if entry_limit:
                off = scfg.get("limit_offset_bps", 0) / 10_000
                lim = sig.price * (1 - off) if sig.side == "LONG" else sig.price * (1 + off)
            qty, why = position_size(equity, lim or price, sig.stop, rcfg, filters)
            if qty <= 0:
                self.log(f"[{symbol}] sinyal {sig.side} atlandı: {why}")
                continue
            expires = (last.close_time + int(scfg.get("limit_timeout_bars", 1)) * step) / 1000
            try:
                res = self.exchange.place_entry(symbol, sig.side, qty, price, sig.stop, sig.take_profit, sig.atr,
                                                rcfg["leverage"], limit_price=lim, expires_at=expires, reason=sig.reason)
            except Exception as e:  # noqa
                self.log(f"[{symbol}] emir hatası: {e}")
                if "-4140" in str(e) or "-4141" in str(e):
                    self.blacklist.add(symbol)
                    self.log(f"[{symbol}] bu oturumda devre dışı bırakıldı (sembol işlem dışı)")
                continue
            risk_usdt = abs((lim or price) - sig.stop) * qty
            if lim is not None:
                self.log(f"[{symbol}] {sig.side} LİMİT EMİR {qty} @ {fp(lim)} | stop {fp(sig.stop)} hedef {fp(sig.take_profit)} "
                         f"| risk {risk_usdt:.2f} USDT | {sig.reason}")
            else:
                self.log(f"[{symbol}] {sig.side} AÇILDI {res.qty} @ {fp(res.entry)} | stop {fp(res.stop)} hedef {fp(res.take_profit)} "
                         f"| risk {risk_usdt:.2f} USDT | {sig.reason}")

        equity = self.exchange.equity(self.prices)
        self.guard.roll_day(equity)
        self.equity_history.append([int(time.time()), round(equity, 2)])
        self.storage.set(f"state:{self.mode}", self.exchange.to_state())
        self.storage.set(f"guard:{self.mode}", self.guard.to_dict())
        self.storage.set(f"equity:{self.mode}", list(self.equity_history))

    # ---------- arayüz ----------
    _status_cache = None

    def status(self):
        if not self.lock.acquire(timeout=2.0):
            if self._status_cache:
                out = dict(self._status_cache)
                out["stale"] = True
                return out
            return {"mode": self.mode, "running": self.running, "stale": True, "positions": [], "pending": [],
                    "symbols": self.symbols, "prices": {}, "funding": {}, "snapshot": {}, "equity_history": [],
                    "stats": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "net_pnl": 0, "fees": 0, "funding": 0, "gross_pnl": 0},
                    "guard": self.guard.to_dict(), "balance": 0, "equity": 0, "unrealized": 0, "last_error": self.last_error,
                    "start_balance": self.cfg["demo"]["start_balance"], "leverage": self.cfg["risk"]["leverage"],
                    "interval": self.cfg["interval"], "started_at": self.started_at, "api_weight": 0, "fee_round_trip_pct": 0}
        try:
            self._status_cache = self._status_locked()
            return self._status_cache
        finally:
            self.lock.release()

    def _status_locked(self):
        if True:
            mode = self.mode
            try:
                if mode == "demo":
                    balance = self.exchange.balance
                    equity = self.exchange.equity(self.prices)
                else:
                    # arayüz için ağa çıkma: tick'in yenilediği önbelleği kullan
                    a = self.exchange._acct_cache or (self.exchange.refresh_account() if not self.running else None)
                    balance = a["wallet"] if a else 0.0
                    equity = (a["wallet"] + a["unrealized"]) if a else 0.0
            except Exception as e:  # noqa
                balance, equity = 0.0, 0.0
                self.last_error = str(e)
            trades = self.storage.trades(mode, limit=1000)
            wins = sum(1 for t in trades if t["net_pnl"] >= 0)
            net = sum(t["net_pnl"] for t in trades)
            positions = [p.to_dict(self.prices.get(s)) for s, p in self.exchange.positions.items()]
            for p in positions:
                p["funding_rate"] = self.funding.get(p["symbol"])
            pending = [o.to_dict() for o in self.exchange.pending.values()]
            for o in pending:
                o["mark_price"] = self.prices.get(o["symbol"])
            return {
                "mode": mode, "running": self.running, "started_at": self.started_at,
                "interval": self.cfg["interval"], "symbols": self.symbols,
                "balance": balance, "equity": equity,
                "unrealized": sum(p.get("unrealized_pnl", 0) for p in positions),
                "positions": positions, "pending": pending, "prices": self.prices, "funding": self.funding,
                "guard": self.guard.to_dict(),
                "stats": {"trades": len(trades), "wins": wins, "losses": len(trades) - wins,
                          "win_rate": round(wins / len(trades) * 100, 1) if trades else 0.0,
                          "net_pnl": net, "fees": sum(t["fees"] for t in trades), "funding": sum(t["funding"] for t in trades),
                          "gross_pnl": sum(t["gross_pnl"] for t in trades)},
                "snapshot": self.snapshot, "equity_history": list(self.equity_history),
                "last_error": self.last_error, "start_balance": self.cfg["demo"]["start_balance"],
                "api_weight": max(self.client.used_weight, self.public.used_weight),
                "leverage": self.cfg["risk"]["leverage"],
                "fee_round_trip_pct": cfgmod.round_trip_fee(self.cfg, taker_in=not entry_limit) * 100
                if (entry_limit := self.cfg["strategy"].get("entry_order", "limit") == "limit") or True else 0,
            }
