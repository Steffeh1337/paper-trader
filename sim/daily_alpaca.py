#!/usr/bin/env python3
"""Daily paper-trading run on Alpaca data, with the backtest's exact engine.

After the US close: download the day's 5-minute bars (plus the 60 sessions
before it) for the 800-stock universe from Alpaca, find every 15-minute
opening-range breakout, score each with the skill's Time Machine pinned to the
signal time, then trade the day with sim/engine.py under sim/rules.json.
Writes the same logs, results and dashboard files as sim/daily.py.

Keys: APCA_API_KEY_ID and APCA_API_SECRET_KEY from the environment (never printed).
FX for the monthly EUR 200 deposit: the ECB's reference rate.

  python3 sim/daily_alpaca.py                    # today (New York), after 16:05
  python3 sim/daily_alpaca.py --date 2026-11-05

Paper only. Nothing here talks to a broker. Not financial advice.
"""
import argparse
import datetime as dt
import gzip
import json
import multiprocessing as mp
import os
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, os.path.join(ROOT, "backtest"))
import daily as D            # noqa: E402  paths, CSV helpers, results page, dashboard export
import engine                # noqa: E402
import candidates as C       # noqa: E402  breakout finder + Time Machine scoring (shared with the backtest)
import alpaca_fetch as A     # noqa: E402  Alpaca client and file format

NY = ZoneInfo("America/New_York")
RULES = os.path.join(ROOT, "sim", "rules.json")
UNIVERSE = os.path.join(ROOT, "sim", "universe_alpaca.json")
WORK = os.path.join(ROOT, "sim", "cache", "alpaca")
REPLAYS = 10000


def eurusd(day):
    url = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": D.UA}), timeout=30) as r:
        root = ET.fromstring(r.read())
    best = None
    for cube in root.iter():
        t = cube.attrib.get("time")
        if t and t <= day.isoformat():
            for c in cube:
                if c.attrib.get("currency") == "USD" and (best is None or t > best[0]):
                    best = (t, float(c.attrib["rate"]))
    if not best:
        raise RuntimeError("no ECB EUR/USD rate on or before %s" % day)
    return best[1]


def download(day, symbols):
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    A.OUT = WORK
    start = (day - dt.timedelta(days=100)).isoformat() + "T00:00:00Z"
    end = (day + dt.timedelta(days=1)).isoformat() + "T00:00:00Z"
    for i in range(0, len(symbols), 20):
        batch = symbols[i:i + 20]
        got = A.bars(batch, "5Min", start, end)
        for s in batch:
            if got.get(s):
                A.save(s, got[s])


def run(day):
    os.makedirs(D.RUNLOG, exist_ok=True)
    open(os.path.join(D.RUNLOG, "latest.log"), "w").close()
    log = D.log
    state = D.read_json(D.STATE, {"days": [], "deposits": [], "spy_shares": 0.0})
    if day.isoformat() in state["days"]:
        log("%s was already simulated. Nothing to do." % day)
        return 0
    rules = dict(engine.DEFAULT_RULES, **D.read_json(RULES, {}))
    symbols = D.read_json(UNIVERSE)
    log("%s: downloading 5-minute bars for %d stocks and SPY from Alpaca..." % (day, len(symbols)))
    download(day, symbols + ["SPY"])

    spy_raw = json.load(gzip.open(os.path.join(WORK, "SPY.json.gz"), "rt"))
    _, sbars, _ = C.tm.parse_bars(spy_raw["chart"]["result"][0], C.FAR_FUTURE)
    sdays = C.tm.split_sessions(sbars)
    if day.isoformat() not in sdays or len(sdays[day.isoformat()]) < 70:
        log("%s: no full SPY session, so the market was closed (weekend or holiday). Nothing to do." % day)
        return 0
    sessions = [d for d in sorted(sdays) if len(sdays[d]) >= 70 and 0 in sdays[d] and d <= day.isoformat()][-61:]
    spy_day = sdays[day.isoformat()]

    pf_path = os.path.join(D.TRADER, "portfolio.json")
    pf = D.read_json(pf_path, {"cash": 0.0, "positions": []})
    month = day.strftime("%Y-%m")
    if not any(d["month"] == month for d in state["deposits"]):
        rate = eurusd(day)
        usd = round(D.MONTHLY_EUR * rate, 2)
        pf["cash"] = round(pf.get("cash", 0.0) + usd, 2)
        spy_open = spy_day[0]["o"]
        state["spy_shares"] = state.get("spy_shares", 0.0) + usd / spy_open
        state["deposits"].append({"month": month, "date": day.isoformat(), "eur": D.MONTHLY_EUR,
                                  "eurusd": rate, "usd": usd, "spy_price": round(spy_open, 2)})
        log("Deposit: EUR %.0f at %.4f (ECB) = $%.2f. Benchmark bought SPY at $%.2f." % (D.MONTHLY_EUR, rate, usd, spy_open))
    equity_start = pf["cash"]
    log("%s  account $%.2f  rules %s" % (day, equity_start, json.dumps(rules)))

    C.REPLAYS = REPLAYS
    files = sorted(os.path.join(WORK, s + ".json.gz") for s in symbols if os.path.exists(os.path.join(WORK, s + ".json.gz")))
    cands = []
    with mp.Pool(4, initializer=C.init, initargs=(sessions,)) as pool:
        for _, rows, _ in pool.imap_unordered(C.one, files):
            cands.extend(r for r in rows if r["date"] == day.isoformat())
    log("%d breakouts between 9:50 and 15:30, each scored by the Time Machine at its signal time." % len(cands))

    end_cash, trades, decisions = engine.trade_day(cands, equity_start, rules)
    for t in trades:
        D.append_csv(D.TRADES_CSV, ["date", "symbol", "qty", "entry_time", "entry", "stop", "target", "exit_time",
                                    "exit", "outcome", "pnl_usd", "R", "tm_expected_R"],
                     [day] + [t[k] for k in ("symbol", "qty", "entry_time", "entry", "stop", "target", "exit_time",
                                             "exit", "outcome", "pnl_usd", "R", "tm_expected_R")])
        log("  %s BUY %d %s at %.2f -> %s %.2f (%s)  P&L $%+.2f" % (t["entry_time"], t["qty"], t["symbol"], t["entry"],
                                                                   t["exit_time"], t["exit"], t["outcome"], t["pnl_usd"]))
    for c in sorted(cands, key=lambda c: (c["time"], -c["exp_R"])):
        D.append_csv(D.CANDS_CSV, ["date", "time", "symbol", "price", "pct_over", "expected_R", "p_target", "p_stop",
                                   "history_R", "decision"],
                     [day, c["time"], c["symbol"], c["entry_ref"], round(100 * (c["entry_ref"] / c["or_high"] - 1), 2),
                      c["exp_R"], c["p_target"], c["p_stop"], c["hist_R"], decisions.get(c["symbol"], "")])
    passing = [c for c in cands if rules["min_exp_R"] is None or (c["exp_R"] > rules["min_exp_R"] and (c["hist_R"] or 0) > 0)]
    affordable = [c for c in cands if c["entry_ref"] <= equity_start * engine.POS_CAP]
    D.append_csv(D.DAYS_CSV, ["date", "breakouts", "affordable", "analyzed", "positive", "traded"],
                 [day, len(cands), len(affordable), len(cands), len(passing), len(trades)])
    if not trades:
        log("  No trade today.")

    pf["cash"] = end_cash
    pf["positions"] = []
    D.write_json(pf_path, pf)
    deposited = sum(d["usd"] for d in state["deposits"])
    spy_value = state["spy_shares"] * spy_day[max(spy_day)]["c"]
    D.append_csv(D.EQUITY_CSV, ["date", "equity_usd", "deposited_usd", "spy_benchmark_usd", "trades"],
                 [day, end_cash, round(deposited, 2), round(spy_value, 2), len(trades)])
    state["days"].append(day.isoformat())
    D.write_json(D.STATE, state)
    log("End of day: account $%.2f | deposited $%.2f | SPY benchmark $%.2f" % (end_cash, deposited, spy_value))
    D.write_summary(state)
    D.export_dashboard(state, day)
    status = D.read_json(os.path.join(D.DASH, "status.json"))[0]
    status.update({"risk_pct": rules["risk"], "daily_risk_pct": rules["daily_loss"], "max_trades": rules["max_trades"],
                   "scan_at": "09:50 to " + rules["last_entry"], "target_r": rules["target_R"],
                   "max_loss_per_trade_usd": round(equity_start * rules["risk"], 2),
                   "min_expected_r": rules["min_exp_R"], "stop_rule": rules["stop"], "breakeven": rules["breakeven"],
                   "data": "Alpaca (IEX)"})
    D.write_json(os.path.join(D.DASH, "status.json"), [status])
    shutil.rmtree(WORK, ignore_errors=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in New York)")
    a = ap.parse_args()
    day = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(NY).date()
    if day == dt.datetime.now(NY).date() and dt.datetime.now(NY).time() < dt.time(16, 20):
        print("The session for %s is not over yet (it is %s in New York). Run after 16:20."
              % (day, dt.datetime.now(NY).strftime("%H:%M")))
        return 1
    return run(day)


if __name__ == "__main__":
    sys.exit(main())
