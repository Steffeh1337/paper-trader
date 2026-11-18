#!/usr/bin/env python3
"""Which instruments to trade: the v2 rule on the last ~60 trading days (Yahoo's 5-minute limit).

Groups compared, each traded on its own with the full pot (EUR 1,000 at the start, no deposits):
  EU: DAX stocks, DAX ETF (EXS1), both
  US: Nasdaq-100 stocks, QQQ, GBP/USD, all three
plus "adaptive" versions that trade a group on a day only if that group's own results
over the previous 20 trading days were positive (the only rule here that changes with findings).
US trades pay a 0.15% currency-conversion fee each way (Trading 212), forex no fee beyond slippage.

  python3 v2/backtest.py      # -> v2/results.json and a printed table
"""
import datetime as dt
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
import common as C   # noqa: E402
import engine        # noqa: E402

CACHE = os.path.join(HERE, "cache")
FX_FEE = float(os.environ.get("FX_FEE", "0.0015"))
RULES = {"min_exp_R": None, "stop": "pct", "stop_pct": 0.01, "target_pct": 0.02, "max_trades": 3, "risk": 0.03,
         "daily_loss": 0.10, "fractional": True}


def load():
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "bars.json.gz")
    if os.path.exists(path):
        return json.load(gzip.open(path, "rt"))
    inst = json.load(open(C.INSTRUMENTS))
    data = {"EU": {}, "US": {}, "EURUSD": {}}
    for s in inst["dax"] + [inst["dax_etf"]]:
        data["EU"][s] = C.yahoo_bars(C.yahoo(s, "60d"), C.RO)
        print("yahoo", s, len(data["EU"][s]), "days", flush=True)
    data["US"][inst["fx"]] = C.yahoo_bars(C.yahoo(inst["fx"], "60d"), C.NY)
    eurusd = C.yahoo_bars(C.yahoo("EURUSD=X", "60d"), C.NY)
    data["EURUSD"] = {d: rows[-1][4] for d, rows in eurusd.items() if rows}
    first = min(min(v) for v in data["EU"].values() if v)
    us = C.alpaca_bars(inst["ndx"] + [inst["ndx_etf"]], first + "T00:00:00Z",
                       (dt.date.today() + dt.timedelta(days=1)).isoformat() + "T00:00:00Z")
    data["US"].update(us)
    json.dump(data, gzip.open(path, "wt"))
    return data


def groups(inst):
    return {
        "EU: DAX stocks": ("EU", inst["dax"]),
        "EU: DAX ETF": ("EU", [inst["dax_etf"]]),
        "EU: DAX stocks + ETF": ("EU", inst["dax"] + [inst["dax_etf"]]),
        "US: Nasdaq-100 stocks": ("US", inst["ndx"]),
        "US: QQQ": ("US", [inst["ndx_etf"]]),
        "US: GBP/USD": ("US", [inst["fx"]]),
        "US: Nasdaq-100 + QQQ + GBP/USD": ("US", inst["ndx"] + [inst["ndx_etf"], inst["fx"]]),
    }


def day_cands(data, session, symbols, day, fx_symbol):
    out = []
    for s in symbols:
        rows = data[session].get(s, {}).get(day)
        if not rows:
            continue
        c = C.candidate(s, rows, session, "fx" if s == fx_symbol else "stock")
        if c:
            c["date"] = day
            c["cost"] = 0.0 if (session == "EU" or s == fx_symbol) else FX_FEE
            out.append(c)
    out.sort(key=lambda c: -c["turnover"])
    return out


def run_group(data, session, symbols, days, fx_symbol, adaptive=False):
    rules = dict(RULES, last_entry=C.SESSIONS[session]["last_entry"], flat_by=C.SESSIONS[session]["flat_by"])
    cash, trades, daily = 1000.0, [], []
    for i, d in enumerate(days):
        if adaptive and sum(daily[-20:]) <= 0 and i >= 20:
            daily.append(0.0)          # still record what the group would have made, for the next decision
            shadow = run_day(data, session, symbols, d, fx_symbol, rules, 1000.0)
            daily[-1] = shadow
            continue
        before = cash
        cash, t = run_day(data, session, symbols, d, fx_symbol, rules, cash, trades=True)
        trades += t
        daily.append(cash - before)
    w = sum(1 for t in trades if t["pnl_usd"] > 0)
    return {"trades": len(trades), "win_rate": w / len(trades) if trades else 0,
            "avg_R": sum(t["R"] for t in trades) / len(trades) if trades else 0,
            "return_pct": cash / 1000.0 - 1, "end": round(cash, 2)}


def run_day(data, session, symbols, d, fx_symbol, rules, cash_eur, trades=False):
    cands = day_cands(data, session, symbols, d, fx_symbol)
    rate = data["EURUSD"].get(d) if session == "US" else 1.0
    if session == "US" and not rate:
        return (cash_eur, []) if trades else 0.0
    end, t, _ = engine.trade_day(cands, cash_eur * rate, rules)
    if trades:
        return end / rate, t
    return end / rate - cash_eur


def bench(data, sym, days, session):
    rows = [data[session][sym].get(d) for d in days]
    rows = [r for r in rows if r]
    return rows[-1][-1][4] / rows[0][0][1] - 1 if rows else None


def main():
    data = load()
    inst = json.load(open(C.INSTRUMENTS))
    eu_days = sorted(set(data["EU"][inst["dax_etf"]]))
    us_days = sorted(set(data["US"][inst["ndx_etf"]]) & set(data["EURUSD"]))
    print("EU days %s to %s (%d), US days %s to %s (%d)" % (eu_days[0], eu_days[-1], len(eu_days), us_days[0], us_days[-1], len(us_days)))
    res = {}
    for name, (session, syms) in groups(inst).items():
        days = eu_days if session == "EU" else us_days
        for adaptive in (False, True):
            r = run_group(data, session, syms, days, inst["fx"], adaptive)
            key = name + (" (adaptive)" if adaptive else "")
            res[key] = r
            print("%-42s trades %3d  win %4.1f%%  avg %+.2fR  return %+6.2f%%" % (key, r["trades"], 100 * r["win_rate"], r["avg_R"], 100 * r["return_pct"]))
    res["_bench"] = {"DAX ETF buy and hold": bench(data, inst["dax_etf"], eu_days, "EU"),
                     "QQQ buy and hold": bench(data, inst["ndx_etf"], us_days, "US")}
    res["_days"] = {"EU": [eu_days[0], eu_days[-1], len(eu_days)], "US": [us_days[0], us_days[-1], len(us_days)]}
    print(res["_bench"])
    json.dump(res, open(os.path.join(HERE, "results.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
