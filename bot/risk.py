"""Pozisyon boyutlandırma ve hesap seviyesinde risk kuralları."""
import time
from datetime import datetime, timezone

from .binance_client import round_step


def position_size(equity, entry, stop, rcfg, filters):
    """Risk bazlı miktar (coin cinsinden). Sığmıyorsa 0 döner."""
    stop_dist = abs(entry - stop)
    if stop_dist <= 0 or equity <= 0:
        return 0.0, "geçersiz stop"
    risk_amount = equity * rcfg["risk_per_trade_pct"] / 100.0
    qty_by_risk = risk_amount / stop_dist
    max_notional = equity * rcfg["max_position_pct"] / 100.0 * rcfg["leverage"]
    qty_by_cap = max_notional / entry
    qty = min(qty_by_risk, qty_by_cap)
    qty = round_step(qty, filters["step_size"])
    if qty < filters["min_qty"]:
        return 0.0, "min miktar altı"
    if qty * entry < filters["min_notional"]:
        return 0.0, f"min notional ({filters['min_notional']} USDT) altı"
    return qty, "ok"


class AccountGuard:
    """Günlük zarar limiti, ardışık kayıp ve soğuma süresi takibi."""

    def __init__(self, rcfg):
        self.rcfg = rcfg
        self.day = self._today()
        self.day_start_equity = None
        self.daily_realized = 0.0
        self.consecutive_losses = 0
        self.cooldown_until = 0.0
        self.halted_reason = None

    @staticmethod
    def _today():
        return datetime.now(timezone.utc).date()

    def roll_day(self, equity):
        if self._today() != self.day:
            self.day = self._today()
            self.day_start_equity = equity
            self.daily_realized = 0.0
            self.halted_reason = None
        if self.day_start_equity is None:
            self.day_start_equity = equity

    def record_trade(self, net_pnl):
        self.daily_realized += net_pnl
        if net_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.rcfg["max_consecutive_losses"]:
                self.cooldown_until = time.time() + self.rcfg["cooldown_minutes"] * 60
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0

    def can_open(self, equity, open_positions):
        self.roll_day(equity)
        if open_positions >= self.rcfg["max_open_positions"]:
            return False, "maksimum açık pozisyon"
        if time.time() < self.cooldown_until:
            mins = int((self.cooldown_until - time.time()) / 60) + 1
            return False, f"soğuma süresi ({mins} dk)"
        base = self.day_start_equity or equity
        limit = -base * self.rcfg["max_daily_loss_pct"] / 100.0
        if self.daily_realized <= limit:
            self.halted_reason = "günlük zarar limiti"
            return False, "günlük zarar limiti aşıldı"
        return True, "ok"

    def to_dict(self):
        return {
            "daily_realized": self.daily_realized,
            "day_start_equity": self.day_start_equity,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until,
            "halted_reason": self.halted_reason,
        }
