"""Geçmiş veriyle strateji testi. Canlı botla aynı sinyal/iz süren stop kodunu kullanır.
Komisyon (maker limit giriş / taker çıkış), kayma, 8 saatlik fonlama tahmini, kısmi kâr alma,
zaman stopu ve üst zaman dilimi filtresi dahildir. Mum verisi data/cache altında önbelleklenir."""
import os
import pickle
import time
from datetime import datetime, timezone

from .binance_client import INTERVAL_MS
from .config import ROOT, round_trip_fee
from .models import Position
from .risk import position_size
from .strategy import (compute_indicators, signal_at, update_trailing, min_bars, htf_interval,
                       htf_bias_series, breakeven_price, partial_tp_price)

FUNDING_MS = 8 * 3600 * 1000
CACHE_DIR = os.path.join(ROOT, "data", "cache")


def load_candles(client, symbol, interval, start_ms, end_ms):
    """Önbellekli mum yükleme: eksik kısmı Binance'ten tamamlar."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{symbol}_{interval}.pkl")
    candles = []
    if os.path.exists(path):
        try:
            candles = pickle.load(open(path, "rb"))
        except Exception:
            candles = []
    step = INTERVAL_MS[interval]
    if candles and candles[0].open_time <= start_ms + step:
        # önbellek başlangıcı kapsıyor; sadece kuyruğu tamamla
        if candles[-1].close_time < end_ms - step:
            candles = candles + client.klines_range(symbol, interval, candles[-1].close_time + 1, end_ms)
    elif candles:
        # önbellek var ama daha erken başlangıç isteniyor: baş tarafı çek, alınamazsa eldekiyle devam
        head = client.klines_range(symbol, interval, start_ms, candles[0].open_time - 1)
        tail = client.klines_range(symbol, interval, candles[-1].close_time + 1, end_ms) if candles[-1].close_time < end_ms - step else []
        candles = head + candles + tail
    else:
        candles = client.klines_range(symbol, interval, start_ms, end_ms)
    if candles:
        pickle.dump(candles, open(path, "wb"))
    return [c for c in candles if start_ms <= c.open_time <= end_ms]


def run_backtest(client, cfg, symbols, interval, days, start_balance=None, funding_rate_est=0.0001, end_ms=None):
    scfg, rcfg = cfg["strategy"], cfg["risk"]
    entry_limit = scfg.get("entry_order", "limit") == "limit"
    fee_rt = round_trip_fee(cfg, taker_in=not entry_limit)
    fees = cfg["fees"]
    disc = 0.9 if fees.get("bnb_discount") else 1.0
    taker, maker = fees["taker"] * disc, fees["maker"] * disc
    slip = cfg["demo"]["slippage_bps"] / 10_000.0
    balance = float(start_balance or cfg["demo"]["start_balance"])
    step = INTERVAL_MS[interval]
    end_ms = end_ms or int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    warm = min_bars(scfg) * step
    htf_iv = htf_interval(scfg, interval)
    use_htf = scfg.get("htf_filter", False)

    data = {}
    for s in symbols:
        try:
            candles = load_candles(client, s, interval, start_ms - warm, end_ms)
        except Exception:
            continue
        if len(candles) < min_bars(scfg) + 10:
            continue
        d = {"candles": candles, "ind": compute_indicators(candles, scfg), "filters": client.symbol_filters(s)}
        if use_htf:
            htf = load_candles(client, s, htf_iv, start_ms - 260 * INTERVAL_MS[htf_iv], end_ms)
            series = htf_bias_series(htf)
            bias, j, cur = [], 0, 0
            for c in candles:
                while j < len(series) and series[j][0] <= c.open_time:
                    cur = series[j][1]
                    j += 1
                bias.append(cur)
            d["bias"] = bias
        data[s] = d
    if not data:
        raise ValueError("yeterli veri yok")

    times = sorted({c.open_time for d in data.values() for c in d["candles"] if c.open_time >= start_ms})
    idx = {s: {c.open_time: i for i, c in enumerate(d["candles"])} for s, d in data.items()}

    positions, pending, trades, equity_curve = {}, {}, [], []
    total_fees = total_funding = 0.0
    peak, max_dd = balance, 0.0
    last_funding_bucket = None
    wins = losses = 0
    gross_profit = gross_loss = 0.0
    signals_seen = 0

    def side_count(side):
        return sum(1 for p in positions.values() if p.side == side)

    def used_margin():
        return sum(p.qty * p.entry / p.leverage for p in positions.values())

    def close_pos(sym, pos, price, reason, t):
        nonlocal balance, total_fees, wins, losses, gross_profit, gross_loss
        fill = price * (1 - slip) if pos.side == "LONG" else price * (1 + slip)
        gross = (fill - pos.entry) * pos.qty * pos.direction
        fee = pos.qty * fill * taker
        total_fees += fee
        balance += gross - fee
        gross_total = gross + pos.realized_partial
        fees_total = fee + pos.entry_fee + pos.partial_fee
        net = gross_total - fees_total - pos.funding_paid
        if net >= 0:
            wins += 1
            gross_profit += net
        else:
            losses += 1
            gross_loss -= net
        trades.append({"symbol": sym, "side": pos.side, "qty": pos.qty, "entry": pos.entry, "exit": fill,
                       "gross_pnl": gross_total, "fees": fees_total, "funding": pos.funding_paid, "net_pnl": net,
                       "reason": reason + (" +kısmi" if pos.partial_done else ""), "entry_type": pos.entry_type,
                       "opened_at": pos.opened_at / 1000, "closed_at": t / 1000})

    def open_pos(sym, sig, qty, fill, t, is_limit):
        nonlocal balance, total_fees
        fee = qty * fill * (maker if is_limit else taker)
        total_fees += fee
        balance -= fee
        positions[sym] = Position(symbol=sym, side=sig.side, qty=qty, entry=fill, stop=sig.stop, take_profit=sig.take_profit,
                                  atr=sig.atr, leverage=rcfg["leverage"], opened_at=t, entry_fee=fee, best_price=fill,
                                  initial_stop=sig.stop, entry_type="limit" if is_limit else "market")

    for t in times:
        # 1) Bekleyen emirler (bu mumda dolar mı?)
        for sym, o in list(pending.items()):
            i = idx[sym].get(t)
            if i is None:
                continue
            c = data[sym]["candles"][i]
            sig, qty, lim, bars = o["sig"], o["qty"], o["limit"], o["bars"]
            if sym in positions:
                del pending[sym]
                continue
            fill = None
            if lim is None:
                fill = c.open * (1 + slip) if sig.side == "LONG" else c.open * (1 - slip)
            elif sig.side == "LONG" and c.open <= lim:
                fill = c.open
            elif sig.side == "LONG" and c.low <= lim:
                fill = lim
            elif sig.side == "SHORT" and c.open >= lim:
                fill = c.open
            elif sig.side == "SHORT" and c.high >= lim:
                fill = lim
            if fill is not None:
                del pending[sym]
                if side_count(sig.side) >= rcfg.get("max_positions_per_side", 99) or len(positions) >= rcfg["max_open_positions"]:
                    continue
                open_pos(sym, sig, qty, fill, t, lim is not None)
            else:
                o["bars"] = bars + 1
                if o["bars"] >= int(scfg.get("limit_timeout_bars", 1)):
                    del pending[sym]

        # 2) Fonlama (8 saatte bir, tahmini oran)
        bucket = t // FUNDING_MS
        if last_funding_bucket is not None and bucket != last_funding_bucket:
            for sym, pos in positions.items():
                i = idx[sym].get(t)
                if i is None:
                    continue
                pay = funding_rate_est * pos.qty * data[sym]["candles"][i].open * pos.direction
                pos.funding_paid += pay
                balance -= pay
                total_funding += pay
        last_funding_bucket = bucket

        # 3) Açık pozisyonlar: stop / kısmi / hedef / zaman stopu / iz süren stop
        for sym in list(positions.keys()):
            i = idx[sym].get(t)
            if i is None:
                continue
            pos = positions[sym]
            c = data[sym]["candles"][i]
            pos.bars_open += 1
            d = pos.direction
            stop_hit = (c.low <= pos.stop) if d == 1 else (c.high >= pos.stop)
            if stop_hit:
                px = pos.stop
                if (d == 1 and c.open < px) or (d == -1 and c.open > px):
                    px = c.open
                close_pos(sym, pos, px, "STOP", c.close_time)
                del positions[sym]
                continue
            ptp = partial_tp_price(pos, scfg)
            if ptp is not None and ((d == 1 and c.high >= ptp) or (d == -1 and c.low <= ptp)):
                part = pos.qty * scfg.get("partial_fraction", 0.5)
                fill = ptp * (1 - slip) if d == 1 else ptp * (1 + slip)
                gross = (fill - pos.entry) * part * d
                fee = part * fill * taker
                balance += gross - fee
                total_fees += fee
                pos.qty -= part
                pos.realized_partial += gross
                pos.partial_fee += fee
                pos.partial_done = True
                be = breakeven_price(pos, fee_rt)
                if (be - pos.stop) * d > 0:
                    pos.stop = be
            tp_hit = (c.high >= pos.take_profit) if d == 1 else (c.low <= pos.take_profit)
            if tp_hit:
                close_pos(sym, pos, pos.take_profit, "TP", c.close_time)
                del positions[sym]
                continue
            mb = int(scfg.get("max_bars_in_trade", 0) or 0)
            if mb and pos.bars_open >= mb:
                close_pos(sym, pos, c.close, "ZAMAN", c.close_time)
                del positions[sym]
                continue
            update_trailing(pos, c.close, scfg, fee_rt)

        # 4) Yeni sinyal (kapanan mum) -> bir sonraki mumda gir
        eq_now = None
        for sym, dd in data.items():
            i = idx[sym].get(t)
            if i is None or sym in positions or sym in pending:
                continue
            bias = dd["bias"][i] if use_htf else None
            sig = signal_at(dd["candles"], dd["ind"], i, scfg, fee_rt, htf_bias=bias)
            if not sig:
                continue
            signals_seen += 1
            if side_count(sig.side) >= rcfg.get("max_positions_per_side", 99) or len(positions) + len(pending) >= rcfg["max_open_positions"]:
                continue
            if eq_now is None:
                eq_now = balance + sum(p.unrealized(data[s]["candles"][idx[s][t]].close) for s, p in positions.items() if t in idx[s])
            if used_margin() >= eq_now * rcfg.get("max_total_margin_pct", 100) / 100:
                continue
            lim = None
            if entry_limit:
                off = scfg.get("limit_offset_bps", 0) / 10_000
                lim = sig.price * (1 - off) if sig.side == "LONG" else sig.price * (1 + off)
            qty, _ = position_size(eq_now, lim or sig.price, sig.stop, rcfg, dd["filters"])
            if qty <= 0:
                continue
            pending[sym] = {"sig": sig, "qty": qty, "limit": lim, "bars": 0}

        # 5) Equity
        eq = balance
        for sym, pos in positions.items():
            i = idx[sym].get(t)
            if i is not None:
                eq += pos.unrealized(data[sym]["candles"][i].close)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak else 0)
        equity_curve.append([t, round(eq, 2)])

    for sym, pos in list(positions.items()):
        c = data[sym]["candles"][-1]
        close_pos(sym, pos, c.close, "BACKTEST_END", c.close_time)

    n = len(trades)
    net = sum(tr["net_pnl"] for tr in trades)
    start_bal = float(start_balance or cfg["demo"]["start_balance"])
    limit_trades = sum(1 for tr in trades if tr["entry_type"] == "limit")
    per_symbol = {}
    for tr in trades:
        ps = per_symbol.setdefault(tr["symbol"], {"trades": 0, "net": 0.0})
        ps["trades"] += 1
        ps["net"] = round(ps["net"] + tr["net_pnl"], 2)
    return {
        "symbols": list(data.keys()), "interval": interval, "htf_interval": htf_iv if use_htf else None, "days": days,
        "start_balance": start_bal, "end_balance": round(balance, 2),
        "net_pnl": round(net, 2), "net_pct": round(net / start_bal * 100, 2),
        "gross_pnl": round(sum(tr["gross_pnl"] for tr in trades), 2),
        "total_fees": round(total_fees, 2), "total_funding": round(total_funding, 2),
        "trades": n, "wins": wins, "losses": losses, "signals": signals_seen, "limit_fills": limit_trades,
        "win_rate": round(wins / n * 100, 1) if n else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (999.0 if gross_profit else 0.0),
        "avg_net_per_trade": round(net / n, 2) if n else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "from": datetime.fromtimestamp(times[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        "to": datetime.fromtimestamp(times[-1] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        "equity_curve": equity_curve[:: max(1, len(equity_curve) // 400)],
        "trade_list": trades[-100:], "per_symbol": per_symbol,
    }
