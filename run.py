#!/usr/bin/env python3
"""Web arayüzü + bot. Kullanım: python3 run.py"""
import socket
socket.setdefaulttimeout(15)  # DNS dahil tüm soket işlemlerine üst sınır

from bot.storage import Storage
from bot.trader import Trader
from bot.web import create_app
from bot import config as cfgmod

import os

if __name__ == "__main__":
    trader = Trader(Storage())
    app = create_app(trader)
    # Konteyner yeniden başladıysa bot kaldığı yerden devam etsin (önceden çalışıyorsa veya TRADEBOT_AUTOSTART=true)
    if os.environ.get("TRADEBOT_AUTOSTART", "").lower() in ("1", "true", "yes") or trader.storage.get("autostart"):
        ok, msg = trader.start()
        print("otomatik başlatma:", msg, flush=True)
    web = trader.cfg["web"]
    if web["host"] not in ("127.0.0.1", "localhost") and not web.get("password"):
        raise SystemExit("HATA: web.host dış erişime açık ama web.password boş. config.json içinde web.password ayarlayın.")
    print(f"\nArayüz: http://{web['host']}:{web['port']}\n")
    app.run(host=web["host"], port=int(web["port"]), threaded=True, debug=False, use_reloader=False)
