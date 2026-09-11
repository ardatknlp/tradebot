"""Flask web arayüzü (sadece localhost)."""
import copy
import threading

import hmac

from flask import Flask, Response, jsonify, render_template, request

from . import config as cfgmod
from .backtester import run_backtest

SECRET_FIELDS = ("api_key", "api_secret", "testnet_api_key", "testnet_api_secret")
_bt_lock = threading.Lock()


def create_app(trader):
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    @app.before_request
    def _auth():
        """web.password ayarlıysa tüm istekler HTTP Basic Auth ister (sunucuda zorunlu)."""
        pw = (trader.cfg.get("web") or {}).get("password") or ""
        if not pw:
            return None
        a = request.authorization
        if a and a.password and hmac.compare_digest(a.password, pw):
            return None
        return Response("Giriş gerekli", 401, {"WWW-Authenticate": 'Basic realm="tradebot"'})

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        return jsonify(trader.status())

    @app.get("/api/trades")
    def trades():
        return jsonify(trader.storage.trades(request.args.get("mode") or trader.mode, limit=200))

    @app.get("/api/logs")
    def logs():
        return jsonify(list(trader.logs)[-150:])

    @app.get("/api/config")
    def get_config():
        cfg = copy.deepcopy(trader.cfg)
        for f in SECRET_FIELDS:
            cfg[f + "_set"] = bool(cfg.get(f))
            cfg[f] = ""
        return jsonify(cfg)

    @app.post("/api/config")
    def set_config():
        incoming = request.get_json(force=True) or {}
        current = cfgmod.load()
        for f in SECRET_FIELDS:
            if not incoming.get(f):
                incoming[f] = current.get(f, "")
            incoming.pop(f + "_set", None)
        merged = cfgmod._merge(current, incoming)
        if merged["mode"] not in ("demo", "testnet", "live"):
            return jsonify({"ok": False, "msg": "geçersiz mod"}), 400
        merged["symbols"] = [s.strip().upper() for s in merged["symbols"] if s.strip()]
        cfgmod.save(merged)
        trader.reload_config()
        return jsonify({"ok": True, "msg": "kaydedildi"})

    @app.post("/api/start")
    def start():
        ok, msg = trader.start()
        return jsonify({"ok": ok, "msg": msg})

    @app.post("/api/stop")
    def stop():
        ok, msg = trader.stop()
        return jsonify({"ok": ok, "msg": msg})

    @app.post("/api/reset_demo")
    def reset_demo():
        ok, msg = trader.reset_demo()
        return jsonify({"ok": ok, "msg": msg})

    @app.post("/api/close/<symbol>")
    def close(symbol):
        ok, msg = trader.close_position(symbol.upper())
        return jsonify({"ok": ok, "msg": msg})

    @app.post("/api/backtest")
    def backtest():
        body = request.get_json(force=True) or {}
        symbols = [s.strip().upper() for s in (body.get("symbols") or trader.cfg["symbols"]) if s.strip()]
        interval = body.get("interval") or trader.cfg["interval"]
        days = int(body.get("days") or 90)
        if not _bt_lock.acquire(blocking=False):
            return jsonify({"ok": False, "msg": "zaten bir backtest çalışıyor"}), 429
        try:
            result = run_backtest(trader.public, cfgmod.load(), symbols, interval, days)
            return jsonify({"ok": True, "result": result})
        except Exception as e:  # noqa
            return jsonify({"ok": False, "msg": str(e)}), 400
        finally:
            _bt_lock.release()

    return app
