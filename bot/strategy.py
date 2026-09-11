"""Strateji v2: trend takip + çoklu tetikleyici + üst zaman dilimi onayı + filtreler.
Aynı fonksiyonlar hem canlı bot hem backtest tarafından kullanılır (tutarlılık için)."""
from .indicators import ema, rsi, atr, adx
from .models import Signal

HTF_MAP = {"1m": "15m", "3m": "30m", "5m": "1h", "15m": "4h", "30m": "4h", "1h": "4h", "2h": "1d", "4h": "1d"}


def htf_interval(scfg, interval):
    return scfg.get("htf_interval") or HTF_MAP.get(interval, "4h")


def sma(values, period):
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def compute_indicators(candles, scfg):
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]
    a, pdi, mdi = adx(highs, lows, closes, scfg["adx_period"])
    return {
        "ema_fast": ema(closes, scfg["ema_fast"]),
        "ema_slow": ema(closes, scfg["ema_slow"]),
        "ema_trend": ema(closes, scfg["ema_trend"]),
        "rsi": rsi(closes, scfg["rsi_period"]),
        "atr": atr(highs, lows, closes, scfg["atr_period"]),
        "adx": a, "pdi": pdi, "mdi": mdi,
        "vol_sma": sma(vols, 20),
    }


def min_bars(scfg):
    return max(scfg["ema_trend"], 2 * scfg["adx_period"] + 2, scfg["rsi_period"] + 2, scfg["atr_period"] + 2,
               int(scfg.get("breakout_bars", 20)) + 1, 21) + 5


def htf_bias_series(htf_candles):
    """Üst zaman dilimi mumları için yön: +1 yukarı, -1 aşağı, 0 nötr. Her HTF mumu için (close_time, bias)."""
    closes = [c.close for c in htf_candles]
    e50, e200 = ema(closes, 50), ema(closes, 200)
    out = []
    for i, c in enumerate(htf_candles):
        b = 0
        if e50[i] is not None and e200[i] is not None:
            if e50[i] > e200[i] and c.close > e200[i]:
                b = 1
            elif e50[i] < e200[i] and c.close < e200[i]:
                b = -1
        out.append((c.close_time, b))
    return out


def htf_bias_at(series, time_ms):
    """time_ms anına kadar KAPANMIŞ son HTF mumunun yönü (lookahead yok)."""
    b = 0
    for ct, bias in series:
        if ct <= time_ms:
            b = bias
        else:
            break
    return b


def signal_at(candles, ind, i, scfg, fee_round_trip, htf_bias=None):
    """i indeksindeki KAPANMIŞ mum için sinyal üretir; yoksa None döner."""
    if i < 1:
        return None
    ef, es, et = ind["ema_fast"][i], ind["ema_slow"][i], ind["ema_trend"][i]
    ef_p, es_p = ind["ema_fast"][i - 1], ind["ema_slow"][i - 1]
    r, a, atr_v = ind["rsi"][i], ind["adx"][i], ind["atr"][i]
    if None in (ef, es, et, ef_p, es_p, r, a, atr_v) or atr_v <= 0:
        return None
    cur, prev = candles[i], candles[i - 1]
    c = cur.close
    if a < scfg["adx_min"]:
        return None

    # Volatilite bandı: çok düşükse komisyon yer, çok yüksekse stoplar sık düşer
    atr_pct = atr_v / c * 100
    if atr_pct < scfg.get("atr_pct_min", 0) or atr_pct > scfg.get("atr_pct_max", 100):
        return None

    # Hacim onayı
    if scfg.get("volume_filter", False):
        vs = ind["vol_sma"][i]
        if vs is None or cur.volume < scfg.get("volume_mult", 1.2) * vs:
            return None

    trend_up = es > et and c > et and ind["pdi"][i] > ind["mdi"][i]
    trend_down = es < et and c < et and ind["mdi"][i] > ind["pdi"][i]
    if scfg.get("htf_filter", False) and htf_bias is not None:
        trend_up = trend_up and htf_bias == 1
        trend_down = trend_down and htf_bias == -1
    long_ok = scfg.get("allow_long", True) and trend_up and r < scfg["rsi_max_long"]
    short_ok = scfg.get("allow_short", True) and trend_down and r > scfg["rsi_min_short"]
    if not long_ok and not short_ok:
        return None

    mode = scfg.get("entry_mode", "any")
    modes = ("cross", "pullback", "breakout") if mode == "any" else (mode,)
    trig_long = trig_short = None
    for m in modes:
        if m == "cross":
            tl = ef > es and ef_p <= es_p
            ts = ef < es and ef_p >= es_p
        elif m == "pullback":
            tl = prev.low <= es and cur.close > ef and cur.close > prev.high and cur.close > cur.open
            ts = prev.high >= es and cur.close < ef and cur.close < prev.low and cur.close < cur.open
        elif m == "breakout":
            n = int(scfg.get("breakout_bars", 20))
            if i < n:
                continue
            tl = cur.close > max(x.high for x in candles[i - n:i])
            ts = cur.close < min(x.low for x in candles[i - n:i])
        else:
            continue
        if tl and trig_long is None:
            trig_long = m
        if ts and trig_short is None:
            trig_short = m

    side = trig = None
    if long_ok and trig_long:
        side, trig = "LONG", trig_long
    elif short_ok and trig_short:
        side, trig = "SHORT", trig_short
    if side is None:
        return None

    d = 1 if side == "LONG" else -1
    stop = c - d * scfg["sl_atr"] * atr_v
    tp = c + d * scfg["tp_atr"] * atr_v
    if stop <= 0 or tp <= 0:
        return None

    # Komisyon kapısı: hedeflenen kâr yüzdesi, gidiş-dönüş komisyonun N katından küçükse işlem yok.
    if abs(tp - c) / c < fee_round_trip * scfg["min_tp_fee_multiple"]:
        return None

    reason = f"{trig}, ADX {a:.1f}, RSI {r:.1f}, ATR %{atr_pct:.2f}" + (f", HTF {'↑' if htf_bias == 1 else '↓'}" if htf_bias else "")
    return Signal(side=side, price=c, stop=stop, take_profit=tp, atr=atr_v, reason=reason)


def update_trailing(pos, price, scfg, fee_round_trip):
    """İz süren stop mantığı. Yeni stop fiyatı döner (değişmediyse None)."""
    d = pos.direction
    if pos.best_price == 0:
        pos.best_price = pos.entry
    if (price - pos.best_price) * d > 0:
        pos.best_price = price

    profit_atr = (pos.best_price - pos.entry) * d / pos.atr if pos.atr else 0
    new_stop = None
    if not pos.trail_active and profit_atr >= scfg["trail_activate_atr"]:
        pos.trail_active = True
        new_stop = breakeven_price(pos, fee_round_trip)
    if pos.trail_active:
        trail = pos.best_price - d * scfg["trail_atr"] * pos.atr
        cand = max(new_stop or pos.stop, trail) if d == 1 else min(new_stop or pos.stop, trail)
        if (cand - pos.stop) * d > 0:
            new_stop = cand
        elif new_stop is not None and (new_stop - pos.stop) * d <= 0:
            new_stop = None
    if new_stop is not None:
        pos.stop = new_stop
    return new_stop


def breakeven_price(pos, fee_round_trip):
    return pos.entry * (1 + pos.direction * fee_round_trip * 1.5)


def partial_tp_price(pos, scfg):
    """Kısmi kâr alma seviyesi: giriş + partial_r x (ilk risk mesafesi)."""
    if not scfg.get("partial_tp", False) or pos.partial_done:
        return None
    risk = abs(pos.entry - pos.initial_stop)
    return pos.entry + pos.direction * scfg.get("partial_r", 1.0) * risk
