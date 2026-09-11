"""Demo (kağıt para) borsa simülasyonu: canlı fiyat, komisyon (maker/taker), kayma, fonlama,
limit giriş emirleri ve kısmi kapanış dahil."""
import time

from .models import Position, Trade, PendingOrder


class PaperExchange:
    mode = "demo"

    def __init__(self, cfg):
        self.cfg = cfg
        self.balance = float(cfg["demo"]["start_balance"])
        self.positions = {}          # symbol -> Position
        self.pending = {}            # symbol -> PendingOrder
        self.total_fees = 0.0
        self.total_funding = 0.0
        self.last_funding_time = {}

    # ---- oranlar ----
    @property
    def slippage(self):
        return float(self.cfg["demo"]["slippage_bps"]) / 10_000.0

    def fee_rate(self, taker=True):
        f = self.cfg["fees"]
        r = float(f["taker"] if taker else f["maker"])
        return r * 0.9 if f.get("bnb_discount") else r

    # ---- durum ----
    def equity(self, prices):
        eq = self.balance
        for sym, p in self.positions.items():
            price = prices.get(sym)
            if price:
                eq += p.unrealized(price)
        return eq

    def used_margin(self):
        return sum(p.qty * p.entry / p.leverage for p in self.positions.values())

    def to_state(self):
        return {
            "balance": self.balance, "total_fees": self.total_fees, "total_funding": self.total_funding,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "pending": {s: o.to_dict() for s, o in self.pending.items()},
            "last_funding_time": self.last_funding_time,
        }

    def load_state(self, st):
        if not st:
            return
        self.balance = float(st.get("balance", self.balance))
        self.total_fees = float(st.get("total_fees", 0))
        self.total_funding = float(st.get("total_funding", 0))
        self.last_funding_time = st.get("last_funding_time", {})
        self.positions = {}
        for s, d in st.get("positions", {}).items():
            d = {k: v for k, v in d.items() if k in Position.__dataclass_fields__}
            self.positions[s] = Position(**d)
        self.pending = {}
        for s, d in st.get("pending", {}).items():
            d = {k: v for k, v in d.items() if k in PendingOrder.__dataclass_fields__}
            self.pending[s] = PendingOrder(**d)

    def reset(self):
        self.balance = float(self.cfg["demo"]["start_balance"])
        self.positions = {}
        self.pending = {}
        self.total_fees = 0.0
        self.total_funding = 0.0
        self.last_funding_time = {}

    # ---- giriş ----
    def _open(self, symbol, side, qty, fill, stop, tp, atr, leverage, taker, reason=""):
        fee = qty * fill * self.fee_rate(taker)
        margin = qty * fill / leverage
        if margin + fee > self.balance - self.used_margin():
            raise ValueError("yetersiz demo bakiye")
        self.balance -= fee
        self.total_fees += fee
        pos = Position(symbol=symbol, side=side, qty=qty, entry=fill, stop=stop, take_profit=tp, atr=atr,
                       leverage=leverage, entry_fee=fee, best_price=fill, initial_stop=stop,
                       entry_type="market" if taker else "limit")
        self.positions[symbol] = pos
        return pos

    def place_entry(self, symbol, side, qty, price, stop, tp, atr, leverage, limit_price=None, expires_at=None, reason=""):
        """limit_price verilirse bekleyen emir oluşturur (PendingOrder döner), yoksa market ile açar (Position döner)."""
        if limit_price is None:
            fill = price * (1 + self.slippage) if side == "LONG" else price * (1 - self.slippage)
            return self._open(symbol, side, qty, fill, stop, tp, atr, leverage, taker=True, reason=reason)
        o = PendingOrder(symbol=symbol, side=side, qty=qty, limit_price=limit_price, stop=stop, take_profit=tp,
                         atr=atr, leverage=leverage, created_at=time.time(), expires_at=expires_at or time.time() + 3600,
                         reason=reason)
        self.pending[symbol] = o
        return o

    def poll_pending(self, symbol, price, now=None):
        """Bekleyen limit emri doldu mu? Dolduysa Position döner; süresi geçtiyse iptal eder."""
        o = self.pending.get(symbol)
        if not o:
            return None
        now = now or time.time()
        filled = (o.side == "LONG" and price <= o.limit_price) or (o.side == "SHORT" and price >= o.limit_price)
        if filled:
            del self.pending[symbol]
            try:
                return self._open(symbol, o.side, o.qty, o.limit_price, o.stop, o.take_profit, o.atr, o.leverage, taker=False)
            except ValueError:
                return None
        if now >= o.expires_at:
            del self.pending[symbol]
        return None

    def cancel_pending(self, symbol):
        return self.pending.pop(symbol, None)

    # ---- yönetim ----
    def update_stop(self, pos, new_stop):
        pos.stop = new_stop

    def reduce(self, symbol, fraction, price):
        """Pozisyonun bir kısmını piyasa fiyatından kapat (kısmi kâr alma)."""
        pos = self.positions[symbol]
        part = pos.qty * fraction
        fill = price * (1 - self.slippage) if pos.side == "LONG" else price * (1 + self.slippage)
        gross = (fill - pos.entry) * part * pos.direction
        fee = part * fill * self.fee_rate(True)
        self.balance += gross - fee
        self.total_fees += fee
        pos.qty -= part
        pos.realized_partial += gross
        pos.partial_fee += fee
        pos.partial_done = True
        return gross, fee

    def close(self, symbol, price, reason):
        pos = self.positions.pop(symbol)
        fill = price * (1 - self.slippage) if pos.side == "LONG" else price * (1 + self.slippage)
        gross = (fill - pos.entry) * pos.qty * pos.direction
        fee = pos.qty * fill * self.fee_rate(True)
        self.balance += gross - fee
        self.total_fees += fee
        return make_trade(pos, fill, gross, fee, reason, "demo")

    def check_exit(self, symbol, price):
        pos = self.positions.get(symbol)
        if not pos:
            return None
        if pos.side == "LONG":
            if price <= pos.stop:
                return "STOP"
            if price >= pos.take_profit:
                return "TP"
        else:
            if price >= pos.stop:
                return "STOP"
            if price <= pos.take_profit:
                return "TP"
        return None

    def apply_funding(self, symbol, funding_rate, mark_price, next_funding_time):
        pos = self.positions.get(symbol)
        if not pos or not self.cfg["demo"].get("apply_funding", True):
            return 0.0
        last = self.last_funding_time.get(symbol)
        now_ms = int(time.time() * 1000)
        if last is None:
            self.last_funding_time[symbol] = next_funding_time
            return 0.0
        if now_ms >= last and next_funding_time != last:
            payment = funding_rate * pos.qty * mark_price * pos.direction
            self.balance -= payment
            pos.funding_paid += payment
            self.total_funding += payment
            self.last_funding_time[symbol] = next_funding_time
            return payment
        return 0.0

    def sync(self):
        return []


def make_trade(pos, fill, gross_remaining, exit_fee, reason, mode):
    gross = gross_remaining + pos.realized_partial
    fees = pos.entry_fee + pos.partial_fee + exit_fee
    net = gross - fees - pos.funding_paid
    return Trade(symbol=pos.symbol, side=pos.side, qty=pos.qty, entry=pos.entry, exit=fill, gross_pnl=gross,
                 fees=fees, funding=pos.funding_paid, net_pnl=net,
                 reason=reason + (" +kısmi" if pos.partial_done else ""),
                 opened_at=pos.opened_at, closed_at=time.time(), mode=mode)
