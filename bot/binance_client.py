"""Binance USDⓈ-M Futures REST istemcisi (bağımlılık: requests).
Public uçlar anahtar gerektirmez; imzalı uçlar için api_key/secret verilmelidir."""
import hashlib
import hmac
import math
import time
import urllib.parse

import requests

LIVE_URL = "https://fapi.binance.com"
TESTNET_URL = "https://testnet.binancefuture.com"

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


class BinanceError(Exception):
    pass


class BinanceFutures:
    def __init__(self, api_key="", api_secret="", testnet=False, timeout=10):
        self.api_key = api_key or ""
        self.api_secret = (api_secret or "").encode()
        self.base = TESTNET_URL if testnet else LIVE_URL
        self.timeout = timeout
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["X-MBX-APIKEY"] = self.api_key
        self._time_offset = 0
        self._exchange_info = None
        self._exchange_info_ts = 0
        self.net_errors = 0
        self.used_weight = 0          # son yanıttaki X-MBX-USED-WEIGHT-1M
        self.weight_soft_limit = 1500 # bu ağırlığın üstünde istekler arasına bekleme konur
        self.banned_until = 0.0

    # ---------- düşük seviye ----------
    def _request(self, method, path, params=None, signed=False):
        if time.time() < self.banned_until:
            raise BinanceError(f"-1003: istek limiti; {int(self.banned_until - time.time())} sn bekleniyor")
        # Testnet paylaşımlı IP kullandığı için bildirdiği ağırlık bizim değil; yumuşak yavaşlatma sadece ana ağda
        if self.base == LIVE_URL and self.used_weight > self.weight_soft_limit:
            time.sleep(min(5.0, (self.used_weight - self.weight_soft_limit) / 200.0))
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            if not self.api_key or not self.api_secret:
                raise BinanceError("API anahtarı/secret tanımlı değil")
            params["timestamp"] = int(time.time() * 1000) + self._time_offset
            params["recvWindow"] = 10000
            query = urllib.parse.urlencode(params, doseq=True)
            sig = hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()
            query += "&signature=" + sig
            url = f"{self.base}{path}?{query}"
            params, full_url = None, url
        else:
            full_url = f"{self.base}{path}"
        try:
            resp = self.session.request(method, full_url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            self.net_errors += 1
            raise BinanceError(f"ağ hatası: {type(e).__name__}")
        self.net_errors = 0
        try:
            self.used_weight = int(resp.headers.get("X-MBX-USED-WEIGHT-1M", self.used_weight))
        except ValueError:
            pass
        if resp.status_code in (418, 429):
            retry = int(resp.headers.get("Retry-After", 60))
            self.banned_until = time.time() + retry
            raise BinanceError(f"-1003: istek limiti aşıldı (HTTP {resp.status_code}), {retry} sn bekleniyor")
        try:
            data = resp.json()
        except ValueError:
            raise BinanceError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        code = None
        if isinstance(data, dict) and "code" in data:
            try:
                code = int(data["code"])
            except (TypeError, ValueError):
                code = None
        if code == -1003:
            self.banned_until = time.time() + 10
        if resp.status_code >= 400 or (code is not None and code < 0 and "msg" in data):
            raise BinanceError(f"{data.get('code')}: {data.get('msg')}" if isinstance(data, dict) else str(data))
        return data

    def sync_time(self):
        server = self._request("GET", "/fapi/v1/time")["serverTime"]
        self._time_offset = server - int(time.time() * 1000)

    # ---------- public ----------
    def ping(self):
        return self._request("GET", "/fapi/v1/ping")

    def klines(self, symbol, interval, limit=500, start_time=None, end_time=None):
        raw = self._request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit,
            "startTime": start_time, "endTime": end_time,
        })
        from .models import Candle
        return [Candle(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), int(k[6])) for k in raw]

    def klines_range(self, symbol, interval, start_ms, end_ms=None, page=1500):
        """Uzun tarih aralığı için sayfalı mum çekimi."""
        out = []
        cur = start_ms
        end_ms = end_ms or int(time.time() * 1000)
        step = INTERVAL_MS[interval]
        while cur < end_ms:
            batch = self.klines(symbol, interval, limit=page, start_time=cur, end_time=end_ms)
            if not batch:
                break
            out.extend(batch)
            time.sleep(0.35)  # limit=1500 mum = 10 ağırlık; ~1700 ağırlık/dk üst sınır
            nxt = batch[-1].open_time + step
            if nxt <= cur:
                break
            cur = nxt
            if len(batch) < page:
                break
        # açık (kapanmamış) son mumu at
        now = int(time.time() * 1000)
        return [c for c in out if c.close_time < now]

    def premium_index(self, symbol):
        d = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return {
            "mark_price": float(d["markPrice"]),
            "index_price": float(d["indexPrice"]),
            "funding_rate": float(d["lastFundingRate"]),
            "next_funding_time": int(d["nextFundingTime"]),
        }

    def ticker_24hr(self):
        return self._request("GET", "/fapi/v1/ticker/24hr")

    def perpetual_symbols(self, min_listing_days=0):
        cutoff = (time.time() - min_listing_days * 86400) * 1000
        return {s["symbol"] for s in self.exchange_info()["symbols"]
                if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
                and int(s.get("onboardDate", 0)) <= cutoff}

    def top_symbols(self, top_n=20, min_quote_volume=1e8, exclude=(), min_listing_days=90, max_change_pct=30.0):
        """24 saatlik USDT hacmine göre en likit perpetual'lar. Yeni listelenen ve o gün aşırı hareket eden
        (pompalanan) coinler elenir."""
        perps = self.perpetual_symbols(min_listing_days)
        rows = [t for t in self.ticker_24hr() if t["symbol"] in perps and t["symbol"] not in exclude
                and float(t.get("quoteVolume", 0)) >= min_quote_volume
                and abs(float(t.get("priceChangePercent", 0))) <= max_change_pct
                and t["symbol"].isascii()]
        rows.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
        return [t["symbol"] for t in rows[:top_n]]

    def premium_index_all(self):
        out = {}
        for d in self._request("GET", "/fapi/v1/premiumIndex"):
            try:
                out[d["symbol"]] = {"mark_price": float(d["markPrice"]), "index_price": float(d["indexPrice"]),
                                    "funding_rate": float(d["lastFundingRate"] or 0), "next_funding_time": int(d["nextFundingTime"])}
            except (KeyError, ValueError):
                continue
        return out

    def ticker_price(self, symbol):
        return float(self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})["price"])

    def exchange_info(self):
        if self._exchange_info is None or time.time() - self._exchange_info_ts > 3600:
            self._exchange_info = self._request("GET", "/fapi/v1/exchangeInfo")
            self._exchange_info_ts = time.time()
        return self._exchange_info

    def symbol_filters(self, symbol):
        info = self.exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                f = {"tick_size": 0.01, "step_size": 0.001, "min_qty": 0.001, "min_notional": 5.0,
                     "price_precision": s.get("pricePrecision", 2), "qty_precision": s.get("quantityPrecision", 3)}
                for flt in s["filters"]:
                    t = flt["filterType"]
                    if t == "PRICE_FILTER":
                        f["tick_size"] = float(flt["tickSize"])
                    elif t == "LOT_SIZE" or t == "MARKET_LOT_SIZE":
                        if t == "LOT_SIZE":
                            f["step_size"] = float(flt["stepSize"])
                            f["min_qty"] = float(flt["minQty"])
                    elif t == "MIN_NOTIONAL":
                        f["min_notional"] = float(flt.get("notional", 5))
                return f
        raise BinanceError(f"Sembol bulunamadı: {symbol}")

    # ---------- imzalı ----------
    def commission_rate(self, symbol):
        d = self._request("GET", "/fapi/v1/commissionRate", {"symbol": symbol}, signed=True)
        return float(d["makerCommissionRate"]), float(d["takerCommissionRate"])

    def balance_usdt(self):
        for b in self._request("GET", "/fapi/v2/balance", signed=True):
            if b["asset"] == "USDT":
                return {"wallet": float(b["balance"]), "available": float(b["availableBalance"]),
                        "unrealized": float(b.get("crossUnPnl", 0))}
        return {"wallet": 0.0, "available": 0.0, "unrealized": 0.0}

    def position_risk(self, symbol=None):
        data = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        out = []
        for p in data:
            amt = float(p["positionAmt"])
            if amt == 0:
                continue
            out.append({
                "symbol": p["symbol"], "qty": abs(amt), "side": "LONG" if amt > 0 else "SHORT",
                "entry": float(p["entryPrice"]), "mark": float(p["markPrice"]),
                "unrealized": float(p["unRealizedProfit"]), "leverage": int(p["leverage"]),
                "liquidation": float(p["liquidationPrice"] or 0),
            })
        return out

    def is_hedge_mode(self):
        return bool(self._request("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"])

    def set_leverage(self, symbol, leverage):
        return self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        try:
            return self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, signed=True)
        except BinanceError as e:
            if "-4046" in str(e):  # zaten bu marjin tipinde
                return None
            raise

    def market_order(self, symbol, side, qty, reduce_only=False):
        return self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty,
            "reduceOnly": "true" if reduce_only else None, "newOrderRespType": "RESULT",
        }, signed=True)

    def limit_order(self, symbol, side, qty, price, reduce_only=False):
        return self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "LIMIT", "quantity": qty, "price": price,
            "timeInForce": "GTC", "reduceOnly": "true" if reduce_only else None, "newOrderRespType": "RESULT",
        }, signed=True)

    def get_order(self, symbol, order_id):
        return self._request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    # ---- Algo Order API (koşullu emirler: STOP_MARKET / TAKE_PROFIT_MARKET buraya taşındı) ----
    def algo_order(self, symbol, side, order_type, trigger_price, close_position=True, qty=None, working_type="MARK_PRICE"):
        return self._request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": order_type,
            "triggerPrice": trigger_price, "closePosition": "true" if close_position else None,
            "quantity": None if close_position else qty, "reduceOnly": None if close_position else "true",
            "workingType": working_type, "priceProtect": "true",
        }, signed=True)

    def stop_market_close(self, symbol, side, stop_price):
        """Pozisyonun tamamını kapatan STOP_MARKET (mark price tetikli). Döner: {'orderId': algoId, ...}"""
        r = self.algo_order(symbol, side, "STOP_MARKET", stop_price)
        r["orderId"] = r.get("algoId")
        return r

    def take_profit_market_close(self, symbol, side, stop_price):
        r = self.algo_order(symbol, side, "TAKE_PROFIT_MARKET", stop_price)
        r["orderId"] = r.get("algoId")
        return r

    def cancel_algo_order(self, symbol, algo_id):
        try:
            return self._request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id}, signed=True)
        except BinanceError as e:
            if any(code in str(e) for code in ("-2011", "-4110", "-4124", "-4125")):  # bulunamadı / zaten tetiklendi-iptal
                return None
            raise

    def open_algo_orders(self, symbol=None):
        return self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)

    def cancel_all_algo(self, symbol):
        return self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)

    def cancel_order(self, symbol, order_id):
        try:
            return self._request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True)
        except BinanceError as e:
            if "-2011" in str(e):  # unknown order (zaten dolmuş/iptal)
                return None
            raise

    def cancel_all(self, symbol):
        """Sembolün tüm normal ve koşullu (algo) açık emirlerini iptal eder."""
        errs = []
        for fn in (lambda: self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True),
                   lambda: self.cancel_all_algo(symbol)):
            try:
                fn()
            except BinanceError as e:
                errs.append(str(e))
        if len(errs) == 2:
            raise BinanceError("; ".join(errs))
        return True

    def open_orders(self, symbol=None):
        return self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    def user_trades(self, symbol, limit=20, start_time=None):
        return self._request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": limit, "startTime": start_time}, signed=True)

    def income(self, symbol=None, income_type=None, start_time=None, limit=100):
        return self._request("GET", "/fapi/v1/income", {
            "symbol": symbol, "incomeType": income_type, "startTime": start_time, "limit": limit}, signed=True)


# ---------- yardımcılar ----------
def round_step(value, step):
    if step <= 0:
        return value
    precision = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    return float(f"{math.floor(value / step) * step:.{precision}f}")


def round_price(value, tick):
    if tick <= 0:
        return value
    precision = max(0, int(round(-math.log10(tick)))) if tick < 1 else 0
    return float(f"{round(value / tick) * tick:.{precision}f}")
