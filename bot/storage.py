"""SQLite kalıcı depolama: kapanan işlemler ve bot durumu."""
import json
import os
import sqlite3
import threading

from .config import DATA_DIR
from .models import Trade

DB_PATH = os.path.join(DATA_DIR, "tradebot.db")


class Storage:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, symbol TEXT, side TEXT, qty REAL,
            entry REAL, exit REAL, gross_pnl REAL, fees REAL, funding REAL, net_pnl REAL,
            reason TEXT, opened_at REAL, closed_at REAL)""")
        self.conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self.conn.commit()

    def add_trade(self, t: Trade):
        with self.lock:
            self.conn.execute(
                "INSERT INTO trades (mode,symbol,side,qty,entry,exit,gross_pnl,fees,funding,net_pnl,reason,opened_at,closed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.mode, t.symbol, t.side, t.qty, t.entry, t.exit, t.gross_pnl, t.fees, t.funding, t.net_pnl,
                 t.reason, t.opened_at, t.closed_at))
            self.conn.commit()

    def trades(self, mode=None, limit=200):
        with self.lock:
            q = "SELECT mode,symbol,side,qty,entry,exit,gross_pnl,fees,funding,net_pnl,reason,opened_at,closed_at FROM trades"
            args = []
            if mode:
                q += " WHERE mode=?"
                args.append(mode)
            q += " ORDER BY closed_at DESC LIMIT ?"
            args.append(limit)
            rows = self.conn.execute(q, args).fetchall()
        keys = ["mode", "symbol", "side", "qty", "entry", "exit", "gross_pnl", "fees", "funding", "net_pnl",
                "reason", "opened_at", "closed_at"]
        return [dict(zip(keys, r)) for r in rows]

    def clear_trades(self, mode):
        with self.lock:
            self.conn.execute("DELETE FROM trades WHERE mode=?", (mode,))
            self.conn.commit()

    def get(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO kv (k,v) VALUES (?,?)", (key, json.dumps(value)))
            self.conn.commit()
