"""Gerçek / testnet borsa adaptörü. Stop ve hedef emirleri borsaya yerleştirilir,
böylece bot kapansa bile pozisyon korunur. Limit giriş, kısmi kapanış desteklenir."""
import time

from .binance_client import BinanceError, round_price, round_step
from .models import Position, PendingOrder
from .paper_engine import make_trade


class LiveExchange:
    def __init__(self, client, cfg, mode, log):
        self.client = client
        self.cfg = cfg
        self.mode = mode  # live | testnet
        self.log = log
        self.positions = {}
        self.pending = {}
        self.total_fees = 0.0
        self.total_funding = 0.0
        self.taker = float(cfg["fees"]["taker"])
        self.maker = float(cfg["fees"]["maker"])
        self._configured = set()
        self._warned = set()

    # ---- durum (önbellekli: hesap uçlarına tick başına en fazla 1 kez gidilir) ----
    _acct_cache = None
    _acct_ts = 0.0
    ACCT_TTL = 30.0          # bakiye/pozisyon önbelleği (sn); testnet sayaç gürültüsüne karşı seyrek

    def refresh_account(self, force=False):
        if not force and self._acct_cache and time.time() - self._acct_ts < self.ACCT_TTL:
            return self._acct_cache
        b = self.client.balance_usdt()
        risk = self.client.position_risk()
        self._acct_cache = {"wallet": b["wallet"], "available": b["available"],
                            "unrealized": sum(p["unrealized"] for p in risk), "risk": risk}
        self._acct_ts = time.time()
        return self._acct_cache

    def balance_info(self):
        return self.refresh_account()

    def equity(self, prices=None):
        a = self.refresh_account()
        return a["wallet"] + a["unrealized"]

    @property
    def balance(self):
        return self.refresh_account()["wallet"]

    def used_margin(self):
        return sum(p.qty * p.entry / p.leverage for p in self.positions.values())

    def to_state(self):
        return {"positions": {s: p.to_dict() for s, p in self.positions.items()},
                "pending": {s: o.to_dict() for s, o in self.pending.items()},
                "total_fees": self.total_fees, "total_funding": self.total_funding}

    def load_state(self, st):
        if not st:
            return
        self.total_fees = float(st.get("total_fees", 0))
        self.total_funding = float(st.get("total_funding", 0))
        for s, d in st.get("positions", {}).items():
            d = {k: v for k, v in d.items() if k in Position.__dataclass_fields__}
            self.positions[s] = Position(**d)
        for s, d in st.get("pending", {}).items():
            d = {k: v for k, v in d.items() if k in PendingOrder.__dataclass_fields__}
            self.pending[s] = PendingOrder(**d)

    def reset(self):
        pass

    # ---- hazırlık ----
    def prepare_symbol(self, symbol):
        if symbol in self._configured:
            return
        r = self.cfg["risk"]
        self.client.set_margin_type(symbol, r.get("margin_type", "ISOLATED"))
        self.client.set_leverage(symbol, int(r["leverage"]))
        try:
            self.maker, self.taker = self.client.commission_rate(symbol)
        except BinanceError:
            pass
        self._configured.add(symbol)

    def _actual_fee(self, symbol, order_id, fallback):
        try:
            fee, found = 0.0, False
            for t in self.client.user_trades(symbol, limit=50):
                if int(t["orderId"]) == int(order_id):
                    found = True
                    if t["commissionAsset"] == "USDT":
                        fee += float(t["commission"])
                    else:
                        return fallback
            return fee if found else fallback
        except BinanceError:
            return fallback

    def _protect(self, pos):
        f = self.client.symbol_filters(pos.symbol)
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        pos.stop = round_price(pos.stop, f["tick_size"])
        pos.take_profit = round_price(pos.take_profit, f["tick_size"])
        try:
            so = self.client.stop_market_close(pos.symbol, close_side, pos.stop)
            pos.stop_order_id = so["orderId"]
            to = self.client.take_profit_market_close(pos.symbol, close_side, pos.take_profit)
            pos.tp_order_id = to["orderId"]
        except BinanceError as e:
            self.log(f"[{pos.symbol}] koruma emri başarısız ({e}); pozisyon kapatılıyor")
            self.client.cancel_all(pos.symbol)
            self.client.market_order(pos.symbol, close_side, pos.qty, reduce_only=True)
            raise

    def _new_position(self, symbol, side, qty, fill, stop, tp, atr, leverage, fee, entry_type):
        pos = Position(symbol=symbol, side=side, qty=qty, entry=fill, stop=stop, take_profit=tp, atr=atr,
                       leverage=leverage, entry_fee=fee, best_price=fill, initial_stop=stop, entry_type=entry_type)
        self.total_fees += fee
        self._protect(pos)
        self.positions[symbol] = pos
        return pos

    # ---- giriş ----
    def place_entry(self, symbol, side, qty, price, stop, tp, atr, leverage, limit_price=None, expires_at=None, reason=""):
        self.prepare_symbol(symbol)
        f = self.client.symbol_filters(symbol)
        order_side = "BUY" if side == "LONG" else "SELL"
        if limit_price is None:
            res = self.client.market_order(symbol, order_side, qty)
            filled_qty = float(res.get("executedQty") or qty)
            fill = float(res.get("avgPrice") or 0) or price
            fee = self._actual_fee(symbol, res["orderId"], filled_qty * fill * self.taker)
            return self._new_position(symbol, side, filled_qty, fill, stop, tp, atr, leverage, fee, "market")
        lp = round_price(limit_price, f["tick_size"])
        res = self.client.limit_order(symbol, order_side, qty, lp)
        o = PendingOrder(symbol=symbol, side=side, qty=qty, limit_price=lp, stop=stop, take_profit=tp, atr=atr,
                         leverage=leverage, created_at=time.time(), expires_at=expires_at or time.time() + 3600,
                         order_id=res["orderId"], reason=reason)
        self.pending[symbol] = o
        return o

    def poll_pending(self, symbol, price, now=None):
        o = self.pending.get(symbol)
        if not o:
            return None
        now = now or time.time()
        try:
            od = self.client.get_order(symbol, o.order_id)
        except BinanceError as e:
            self.log(f"[{symbol}] emir sorgulanamadı: {e}")
            return None
        status = od.get("status")
        executed = float(od.get("executedQty") or 0)
        avg = float(od.get("avgPrice") or 0) or o.limit_price
        if status == "FILLED":
            del self.pending[symbol]
            fee = self._actual_fee(symbol, o.order_id, executed * avg * self.maker)
            return self._new_position(symbol, o.side, executed, avg, o.stop, o.take_profit, o.atr, o.leverage, fee, "limit")
        if status in ("CANCELED", "EXPIRED", "REJECTED") or now >= o.expires_at:
            if status not in ("CANCELED", "EXPIRED", "REJECTED"):
                self.client.cancel_order(symbol, o.order_id)
                od = self.client.get_order(symbol, o.order_id)
                executed = float(od.get("executedQty") or 0)
                avg = float(od.get("avgPrice") or 0) or o.limit_price
            del self.pending[symbol]
            if executed > 0:
                f = self.client.symbol_filters(symbol)
                if executed * avg >= f["min_notional"]:
                    fee = self._actual_fee(symbol, o.order_id, executed * avg * self.maker)
                    self.log(f"[{symbol}] limit emir kısmen doldu ({executed}); kalan iptal")
                    return self._new_position(symbol, o.side, executed, avg, o.stop, o.take_profit, o.atr, o.leverage, fee, "limit")
                # çok küçük parça: kapat
                self.client.market_order(symbol, "SELL" if o.side == "LONG" else "BUY", executed, reduce_only=True)
            self.log(f"[{symbol}] limit emir dolmadı, iptal edildi")
        return None

    def cancel_pending(self, symbol):
        o = self.pending.pop(symbol, None)
        if o and o.order_id:
            try:
                self.client.cancel_order(symbol, o.order_id)
            except BinanceError:
                pass
        return o

    # ---- yönetim ----
    def update_stop(self, pos, new_stop):
        """Binance bir yönde tek closePosition stop'a izin verir (-4130): önce eski iptal, sonra yeni.
        Yeni stop konamazsa eski seviye hemen geri konur; pozisyon korumasız bırakılmaz."""
        f = self.client.symbol_filters(pos.symbol)
        new_stop = round_price(new_stop, f["tick_size"])
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        old_stop, old_id = pos.stop, pos.stop_order_id
        if old_id:
            self.client.cancel_algo_order(pos.symbol, old_id)
            pos.stop_order_id = None
        try:
            so = self.client.stop_market_close(pos.symbol, close_side, new_stop)
        except Exception as e:  # noqa
            try:
                so_old = self.client.stop_market_close(pos.symbol, close_side, old_stop)
                pos.stop_order_id = so_old["orderId"]
                pos.stop = old_stop
            except Exception as e2:  # noqa
                self.log(f"[{pos.symbol}] KRİTİK: stop geri konamadı ({e2}); sync onaracak")
            raise e
        pos.stop_order_id = so["orderId"]
        pos.stop = new_stop

    def ensure_protection(self, pos):
        """Borsada stop ve hedef emri var mı? Yoksa yeniden koy (kendi kendini onarma)."""
        try:
            orders = self.client.open_algo_orders(pos.symbol)
        except BinanceError as e:
            self.log(f"[{pos.symbol}] koruma kontrolü yapılamadı: {e}")
            return
        types = {o.get("orderType"): o for o in orders}
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        if "STOP_MARKET" not in types:
            so = self.client.stop_market_close(pos.symbol, close_side, pos.stop)
            pos.stop_order_id = so["orderId"]
            self.log(f"[{pos.symbol}] UYARI: borsada stop yoktu, yeniden kondu -> {pos.stop}")
        else:
            pos.stop_order_id = types["STOP_MARKET"].get("algoId", pos.stop_order_id)
        if "TAKE_PROFIT_MARKET" not in types:
            to = self.client.take_profit_market_close(pos.symbol, close_side, pos.take_profit)
            pos.tp_order_id = to["orderId"]
            self.log(f"[{pos.symbol}] UYARI: borsada hedef emri yoktu, yeniden kondu -> {pos.take_profit}")
        # fazladan stop kaldıysa (iptal başarısız olmuşsa) en yenisi hariç temizle
        extra = [o for o in orders if o.get("orderType") == "STOP_MARKET" and o.get("algoId") != pos.stop_order_id]
        for o in extra:
            try:
                self.client.cancel_algo_order(pos.symbol, o["algoId"])
            except Exception:
                pass

    def reduce(self, symbol, fraction, price):
        pos = self.positions[symbol]
        f = self.client.symbol_filters(symbol)
        part = round_step(pos.qty * fraction, f["step_size"])
        if part < f["min_qty"] or part * price < f["min_notional"] or (pos.qty - part) * price < f["min_notional"]:
            pos.partial_done = True  # bölünemeyecek kadar küçük; kısmi kapanış atla
            return 0.0, 0.0
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        res = self.client.market_order(symbol, close_side, part, reduce_only=True)
        fill = float(res.get("avgPrice") or 0) or price
        gross = (fill - pos.entry) * part * pos.direction
        fee = self._actual_fee(symbol, res["orderId"], part * fill * self.taker)
        self.total_fees += fee
        pos.qty -= part
        pos.realized_partial += gross
        pos.partial_fee += fee
        pos.partial_done = True
        return gross, fee

    def close(self, symbol, price, reason):
        pos = self.positions.pop(symbol)
        close_side = "SELL" if pos.side == "LONG" else "BUY"
        try:
            self.client.cancel_all(symbol)
        except BinanceError:
            pass
        res = self.client.market_order(symbol, close_side, pos.qty, reduce_only=True)
        fill = float(res.get("avgPrice") or 0) or price
        fee = self._actual_fee(symbol, res["orderId"], pos.qty * fill * self.taker)
        return self._finish(pos, fill, fee, reason)

    def _finish(self, pos, fill, exit_fee, reason):
        gross = (fill - pos.entry) * pos.qty * pos.direction
        self.total_fees += exit_fee
        pos.funding_paid = self._funding_since(pos.symbol, pos.opened_at)
        self.total_funding += pos.funding_paid
        return make_trade(pos, fill, gross, exit_fee, reason, self.mode)

    def _funding_since(self, symbol, opened_at):
        try:
            inc = self.client.income(symbol=symbol, income_type="FUNDING_FEE", start_time=int(opened_at * 1000))
            return -sum(float(i["income"]) for i in inc)
        except BinanceError:
            return 0.0

    def check_exit(self, symbol, price):
        return None  # borsadaki STOP/TP emirleri halleder; sync() kapanışı yakalar

    def apply_funding(self, *a, **k):
        return 0.0

    _last_sync = 0.0
    SYNC_EVERY = 20.0        # borsa ile pozisyon eşitleme aralığı (sn)

    def sync(self):
        """Borsadaki gerçek pozisyonlarla eşitle. Borsada kapanmış (SL/TP) pozisyonları Trade olarak döner."""
        closed = []
        if time.time() - self._last_sync < self.SYNC_EVERY:
            return closed
        try:
            live = {p["symbol"]: p for p in self.refresh_account(force=True)["risk"]}
            self._last_sync = time.time()
        except BinanceError as e:
            if "-1003" not in str(e):
                self.log(f"pozisyon senkronu başarısız: {e}")
            return closed
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            if symbol not in live:
                fill, fee, reason = pos.stop, pos.qty * pos.stop * self.taker, "STOP/TP (borsa)"
                # Hangi emir tetiklendi? Tetiklenmeyen algo emri hâlâ açık durur.
                try:
                    remaining = {o.get("orderType") for o in self.client.open_algo_orders(symbol)}
                    if "TAKE_PROFIT_MARKET" in remaining and "STOP_MARKET" not in remaining:
                        reason = "İZ SÜREN STOP" if (pos.stop - pos.entry) * pos.direction > 0 else "STOP"
                    elif "STOP_MARKET" in remaining and "TAKE_PROFIT_MARKET" not in remaining:
                        reason = "TP"
                except BinanceError:
                    remaining = set()
                try:
                    trades = self.client.user_trades(symbol, limit=30, start_time=int(pos.opened_at * 1000))
                    closing = [t for t in trades if t["side"] == ("SELL" if pos.side == "LONG" else "BUY")]
                    if pos.partial_done and closing:
                        closing = closing[-1:]  # son kapanış işlemi
                    if closing:
                        q = sum(float(t["qty"]) for t in closing)
                        fill = sum(float(t["price"]) * float(t["qty"]) for t in closing) / q if q else fill
                        fee = sum(float(t["commission"]) for t in closing if t["commissionAsset"] == "USDT") or fee
                        if reason == "STOP/TP (borsa)":
                            reason = "TP" if abs(fill - pos.take_profit) < abs(fill - pos.stop) else \
                                ("İZ SÜREN STOP" if (pos.stop - pos.entry) * pos.direction > 0 else "STOP")
                    self.client.cancel_all(symbol)
                except BinanceError:
                    pass
                del self.positions[symbol]
                closed.append(self._finish(pos, fill, fee, reason))
            else:
                lp = live[symbol]
                pos.qty = lp["qty"]
                pos.entry = lp["entry"] or pos.entry
                self._sync_n = getattr(self, "_sync_n", 0) + 1
                if self._sync_n % 6 == 1:  # ~2 dakikada bir koruma emirlerini doğrula
                    try:
                        self.ensure_protection(pos)
                    except Exception as e:  # noqa
                        self.log(f"[{symbol}] koruma onarımı başarısız: {e}")
        for symbol in live:
            if symbol not in self.positions and symbol not in self._warned:
                self._warned.add(symbol)
                self.log(f"UYARI: {symbol} için borsada bot dışı pozisyon var; bot bu pozisyona dokunmaz.")
        return closed
