"""Saf Python teknik göstergeler. Tüm fonksiyonlar girdi uzunluğunda liste döner; hesaplanamayan başlangıç değerleri None."""


def ema(values, period):
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    prev = sma
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes, period=14):
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period

    def _rsi(g, l):
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def true_range(highs, lows, closes):
    n = len(closes)
    tr = [None] * n
    if n == 0:
        return tr
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    return tr


def atr(highs, lows, closes, period=14):
    """Wilder ATR."""
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    tr = true_range(highs, lows, closes)
    first = sum(tr[1:period + 1]) / period
    out[period] = first
    prev = first
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def adx(highs, lows, closes, period=14):
    """Wilder ADX. Döner: (adx, plus_di, minus_di)."""
    n = len(closes)
    adx_out = [None] * n
    pdi_out = [None] * n
    mdi_out = [None] * n
    if n <= 2 * period:
        return adx_out, pdi_out, mdi_out
    tr = true_range(highs, lows, closes)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    # İlk yumuşatılmış değerler (Wilder toplam)
    s_tr = sum(tr[1:period + 1])
    s_pdm = sum(plus_dm[1:period + 1])
    s_mdm = sum(minus_dm[1:period + 1])
    dx_list = []
    for i in range(period, n):
        if i > period:
            s_tr = s_tr - s_tr / period + tr[i]
            s_pdm = s_pdm - s_pdm / period + plus_dm[i]
            s_mdm = s_mdm - s_mdm / period + minus_dm[i]
        pdi = 100.0 * s_pdm / s_tr if s_tr else 0.0
        mdi = 100.0 * s_mdm / s_tr if s_tr else 0.0
        pdi_out[i] = pdi
        mdi_out[i] = mdi
        denom = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / denom if denom else 0.0
        dx_list.append(dx)
        if len(dx_list) == period:
            adx_out[i] = sum(dx_list) / period
        elif len(dx_list) > period:
            adx_out[i] = (adx_out[i - 1] * (period - 1) + dx) / period
    return adx_out, pdi_out, mdi_out
