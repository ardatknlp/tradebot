#!/usr/bin/env python3
"""Komut satırı backtest. Örnek: python3 backtest.py --symbols BTCUSDT,ETHUSDT --interval 15m --days 90"""
import argparse
import json

from bot import config as cfgmod
from bot.backtester import run_backtest
from bot.binance_client import BinanceFutures

if __name__ == "__main__":
    cfg = cfgmod.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(cfg["symbols"]))
    ap.add_argument("--interval", default=cfg["interval"])
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--balance", type=float, default=cfg["demo"]["start_balance"])
    ap.add_argument("--json", action="store_true", help="tam sonucu JSON olarak yaz")
    a = ap.parse_args()
    r = run_backtest(BinanceFutures(), cfg, a.symbols.split(","), a.interval, a.days, a.balance)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        print(f"\n{r['from']} -> {r['to']}  {r['interval']}  {', '.join(r['symbols'])}")
        print(f"Başlangıç : {r['start_balance']:.2f} USDT")
        print(f"Bitiş     : {r['end_balance']:.2f} USDT   ({r['net_pct']:+.2f}%)")
        print(f"Brüt PnL  : {r['gross_pnl']:+.2f}   Komisyon: -{r['total_fees']:.2f}   Fonlama: {-r['total_funding']:+.2f}")
        print(f"Net PnL   : {r['net_pnl']:+.2f}")
        print(f"İşlem     : {r['trades']}  (kazanan {r['wins']}, kaybeden {r['losses']})  Kazanma: %{r['win_rate']}")
        print(f"Profit factor: {r['profit_factor']}   Ort. net/işlem: {r['avg_net_per_trade']:+.2f}   Maks. düşüş: %{r['max_drawdown_pct']}\n")
