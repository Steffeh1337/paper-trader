#!/usr/bin/env python3
"""Daily paper-trading run for the European market (paper only, no broker).

Universe: Euro Stoxx 50 stocks (sim/universe_eu.json, Yahoo tickers), traded with
the same engine as the US run (sim/engine.py) under sim/rules_eu.json.

Times are Romanian time (European exchanges open 09:00 CET = 10:00 in Romania):
  opening range  10:00-10:15 (the first three 5-minute bars)
  entries        first 5-minute close above the range high, from 10:15 up to last_entry
  exits          stop 1% below the entry, target 2% above, anything open sold at flat_by (14:30)
Several breakouts in the same bar: the most traded (opening-range turnover) first.

Money: EUR 500 a month in its own account (sim/eu/), compared with the same money
in the Euro Stoxx 50 index, bought on the deposit day's open.
Data: Yahoo Finance 5-minute bars, one request per stock, slowly (it rate-limits).

  python3 sim/daily_eu.py                    # today
  python3 sim/daily_eu.py --date 2026-11-18

Not financial advice.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
import engine  # noqa: E402

RO = ZoneInfo("Europe/Bucharest")
EU = os.path.join(ROOT, "sim", "eu")
STATE = os.path.join(EU, "state.json")
PORTFOLIO = os.path.join(EU, "portfolio.json")
TRADES_CSV = os.path.join(EU, "trades.csv")
EQUITY_CSV = os.path.join(EU, "equity.csv")
CANDS_CSV = os.path.join(EU, "candidates.csv")
DAYS_CSV = os.path.join(EU, "days.csv")
LOG = os.path.join(EU, "logs", "latest.log")
DASH = os.path.join(EU, "dashboard")
RULES = os.path.join(ROOT, "sim", "rules_eu.json")
UNIVERSE = os.path.join(ROOT, "sim", "universe_eu.json")
BENCH = "^STOXX50E"
UA = "paper-trader-sim/1.0 (personal research)"
PAUSE = 1.5


def log(msg):
    print(msg, flush=True)
    with open(LOG, "a") as f:
        f.write(msg + "\n")


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def yahoo_bars(symbol, day):
    """The day's 5-minute bars in Romanian time: [[HH:MM, o, h, l, c, v], ...]; [] if none."""
    path = "/v8/finance/chart/%s?range=5d&interval=5m" % urllib.parse.quote(symbol)
    for attempt in range(3):
        try:
            req = urllib.request.Request("https://query2.finance.yahoo.com" + path,
                                         headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.load(r)["chart"]["result"][0]
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(60)
                continue
            return None
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError):
            if attempt < 2:
                time.sleep(5)
                continue
            return None
    finally_sleep = PAUSE
    time.sleep(finally_sleep)
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c) or ts % 300:
            continue
        t = dt.datetime.fromtimestamp(ts, RO)
        if t.date() == day:
            out.append([t.strftime("%H:%M"), o, h, l, c, v or 0])
    return out


def candidate(symbol, bars, rules):
    """The day's opening-range breakout for one stock, or None."""
    rng = [b for b in bars if "10:00" <= b[0] < "10:15"]
    if len(rng) < 3:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    turnover = sum(b[4] * b[5] for b in rng)
    for i, b in enumerate(bars):
        if b[0] < "10:15":
            continue
        h, m = map(int, b[0].split(":"))
        close_t = "%02d:%02d" % divmod(h * 60 + m + 5, 60)
        if close_t > rules["last_entry"]:
            return None
        if b[4] > hi:
            after = [[x[0], x[1], x[2], x[3], x[4]] for x in bars[i + 1:]]
            if not after:
                return None
            return {"date": None, "symbol": symbol, "time": close_t, "entry_ref": round(b[4], 4),
                    "or_high": hi, "or_low": lo, "exp_R": 0.0, "hist_R": None, "turnover": turnover, "bars": after}
    return None


def run(day):
    os.makedirs(os.path.join(EU, "logs"), exist_ok=True)
    open(LOG, "w").close()
    state = read_json(STATE, {"days": [], "deposits": [], "bench_units": 0.0})
    if day.isoformat() in state["days"]:
        log("%s was already simulated for Europe. Nothing to do." % day)
        return 0
    rules = dict(engine.DEFAULT_RULES, **read_json(RULES, {}))
    bench = yahoo_bars(BENCH, day)
    if not bench or not any(b[0] >= "10:00" for b in bench):
        log("%s: no Euro Stoxx 50 bars, so European markets were closed (weekend or holiday). Nothing to do." % day)
        return 0
    bench = [b for b in bench if b[0] >= "10:00"]

    pf = read_json(PORTFOLIO, {"cash": 0.0})
    month = day.strftime("%Y-%m")
    target = float(rules.get("monthly_eur", 500))
    have = sum(d["eur"] for d in state["deposits"] if d["month"] == month)
    if have < target - 0.005:
        eur = round(target - have, 2)
        pf["cash"] = round(pf.get("cash", 0.0) + eur, 2)
        state["bench_units"] = state.get("bench_units", 0.0) + eur / bench[0][1]
        state["deposits"].append({"month": month, "date": day.isoformat(), "eur": eur, "bench_price": round(bench[0][1], 2)})
        log("Deposit: EUR %.2f. Benchmark bought the Euro Stoxx 50 at %.2f." % (eur, bench[0][1]))
    equity_start = pf["cash"]
    log("%s  Europe  account EUR %.2f  rules %s" % (day, equity_start, json.dumps(rules)))

    symbols = read_json(UNIVERSE, [])
    cands, missing = [], 0
    for s in symbols:
        bars = yahoo_bars(s, day)
        if not bars:
            missing += 1
            continue
        c = candidate(s, bars, rules)
        if c:
            c["date"] = day.isoformat()
            cands.append(c)
    log("%d of %d stocks had data; %d broke out of the 10:00-10:15 range by %s." % (len(symbols) - missing, len(symbols),
                                                                                  len(cands), rules["last_entry"]))
    cands.sort(key=lambda c: -c["turnover"])          # same-bar ties: the most traded first (stable sort)
    end_cash, trades, decisions = engine.trade_day(cands, equity_start, rules)
    for t in trades:
        append_csv(TRADES_CSV, ["date", "symbol", "qty", "entry_time", "entry", "stop", "target", "exit_time", "exit",
                                "outcome", "pnl_eur", "R"],
                   [day] + [t[k] for k in ("symbol", "qty", "entry_time", "entry", "stop", "target", "exit_time", "exit",
                                           "outcome", "pnl_usd", "R")])
        log("  %s BUY %g %s at %.2f -> %s %.2f (%s)  P&L EUR %+.2f" % (t["entry_time"], t["qty"], t["symbol"], t["entry"],
                                                                      t["exit_time"], t["exit"], t["outcome"], t["pnl_usd"]))
    for c in sorted(cands, key=lambda c: (c["time"], -c["turnover"])):
        append_csv(CANDS_CSV, ["date", "time", "symbol", "price", "pct_over", "decision"],
                   [day, c["time"], c["symbol"], round(c["entry_ref"], 2), round(100 * (c["entry_ref"] / c["or_high"] - 1), 2),
                    decisions.get(c["symbol"], "")])
    append_csv(DAYS_CSV, ["date", "breakouts", "traded"], [day, len(cands), len(trades)])
    if not trades:
        log("  No trade today.")

    pf["cash"] = end_cash
    write_json(PORTFOLIO, pf)
    deposited = sum(d["eur"] for d in state["deposits"])
    bench_value = state["bench_units"] * bench[-1][4]
    append_csv(EQUITY_CSV, ["date", "equity_eur", "deposited_eur", "benchmark_eur", "trades"],
               [day, end_cash, round(deposited, 2), round(bench_value, 2), len(trades)])
    state["days"].append(day.isoformat())
    write_json(STATE, state)
    log("End of day (Europe): account EUR %.2f | deposited EUR %.2f | Euro Stoxx 50 benchmark EUR %.2f"
        % (end_cash, deposited, bench_value))
    export(rules, state, day, equity_start)
    return 0


def export(rules, state, day, equity_start):
    os.makedirs(DASH, exist_ok=True)
    num = lambda v: None if v in ("", None) else float(v)
    for name, path, text in (("equity", EQUITY_CSV, ("date",)), ("trades", TRADES_CSV, ("date", "symbol", "entry_time", "exit_time", "outcome"))):
        rows = []
        if os.path.exists(path):
            for r in csv.DictReader(open(path)):
                rows.append({k: (v if k in text else num(v)) for k, v in r.items()})
        write_json(os.path.join(DASH, name + ".json"), rows)
    write_json(os.path.join(DASH, "status.json"), [{
        "last_run_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_session": day.isoformat(), "monthly_eur": rules.get("monthly_eur", 500),
        "eur_deposited": round(sum(d["eur"] for d in state["deposits"]), 2),
        "stop_pct": rules.get("stop_pct"), "target_pct": rules.get("target_pct"), "max_trades": rules["max_trades"],
        "window": "10:15 to " + rules["last_entry"], "flat_by": rules["flat_by"], "benchmark": "Euro Stoxx 50",
        "data": "Yahoo Finance", "universe": len(read_json(UNIVERSE, [])),
    }])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in Romania)")
    a = ap.parse_args()
    now = dt.datetime.now(RO)
    day = dt.date.fromisoformat(a.date) if a.date else now.date()
    if day == now.date() and now.time() < dt.time(14, 40):
        print("Europe's trading window for %s is not over yet (it is %s in Romania). Run after 14:40." % (day, now.strftime("%H:%M")))
        return 1
    return run(day)


if __name__ == "__main__":
    sys.exit(main())
