#!/usr/bin/env python3
"""Backtest step 2: every opening-range breakout of every past day, scored.

For each cached chart and each full session (skipping the first WARMUP sessions
so the Time Machine has history to draw from, and skipping today):
  - 15-minute opening range = bars 9:30, 9:35, 9:40
  - signal = the first 5-minute bar from 9:45 on that CLOSES above the range high,
    closing no later than 15:30 (the skill's entry rule and window)
  - the skill's Time Machine runs on that plan (stop at the range midpoint,
    target 2R) with its clock pinned to the signal bar's close, so it only sees
    data up to that moment: expected R, odds, and the 60-day history of the rule
The bars after the signal are kept so step 3 can simulate fills for any rule.

  python3 backtest/candidates.py            # -> backtest/cache/candidates.jsonl
"""
import datetime as dt
import glob
import gzip
import json
import math
import multiprocessing as mp
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".claude", "skills", "trader-timemachine"))
import timemachine as tm  # noqa: E402  (the skill's own Time Machine)

CACHE = os.path.join(ROOT, "backtest", "cache")
WARMUP = 12          # sessions before the first tested day
REPLAYS = 2000       # per candidate (the live run uses 10,000; this keeps the backtest fast)
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 5_000_000   # median dollars traded a day
LAST_SIGNAL_MIN = 15 * 60 + 30
FAR_FUTURE = tm.parse_now("2030-01-01 00:00")


def clock(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def one(path):
    sym = os.path.basename(path)[:-len(".json.gz")]
    try:
        text = gzip.open(path, "rt").read()
        result = tm.chart_result(text, sym)
        _, bars, _ = tm.parse_bars(result, FAR_FUTURE)
    except Exception as e:  # a bad or empty chart: skip the stock
        return sym, [], "skip: %s" % e
    days = tm.split_sessions(bars)
    # Liquid stocks priced $5-$150 only, judged on the whole window (median of the days)
    full = [days[d] for d in days if len(days[d]) == tm.SESSION_BARS]
    if len(full) < 20:
        return sym, [], "skip: too few full sessions"
    closes = sorted(s[tm.SESSION_BARS - 1]["c"] for s in full)
    dollars = sorted(sum(b["c"] * b["v"] for b in s.values()) for s in full)
    med_close, med_dollars = closes[len(closes) // 2], dollars[len(dollars) // 2]
    if not (MIN_PRICE <= med_close <= 150) or med_dollars < MIN_DOLLAR_VOLUME:
        return sym, [], "skip: price %.2f, daily dollar volume %.0f" % (med_close, med_dollars)
    test_days = set(TEST_DAYS)
    out = []
    for d in sorted(days):
        if d not in test_days or len(days[d]) != tm.SESSION_BARS:
            continue
        D = [days[d][i] for i in range(tm.SESSION_BARS)]
        oh = max(b["h"] for b in D[:3])
        ol = min(b["l"] for b in D[:3])
        sig = None
        for i in range(3, tm.SESSION_BARS):
            close_min = tm.OPEN_MIN + (i + 1) * 5
            if close_min > LAST_SIGNAL_MIN:
                break
            if D[i]["c"] > oh:
                sig = i
                break
        if sig is None or D[sig]["c"] < MIN_PRICE or sig + 1 >= tm.SESSION_BARS:
            continue
        entry = round(D[sig]["c"], 2)
        stop = math.floor((oh + ol) / 2 * 100) / 100
        if stop >= entry:
            continue
        target = round(entry + 2 * (entry - stop), 2)
        close_min = tm.OPEN_MIN + (sig + 1) * 5
        try:
            plan = tm.normalize_plan({"symbol": sym, "side": "buy", "date": d, "entry": entry, "stop": stop,
                                      "target": target, "entry_time": tm.clock12(close_min), "setup": "orb",
                                      "opening_range": {"minutes": 15}, "stop_rule": "or_mid", "interval": "5m"})
            doc = tm.time_machine(text, plan, tm.parse_now("%s %s" % (d, clock(close_min))), replays=REPLAYS)
        except Exception:
            continue
        mc, hist = doc["mc"], doc.get("history") or {}
        out.append({
            "date": d, "symbol": sym, "time": clock(close_min), "entry_ref": entry,
            "or_high": round(oh, 4), "or_low": round(ol, 4),
            "exp_R": mc["avg_R"], "p_target": mc["p_target"], "p_stop": mc["p_stop"],
            "hist_R": hist.get("avg_R"), "hist_n": hist.get("triggered"),
            # bars after the signal bar: [start, open, high, low, close]
            "bars": [[b["start"], b["o"], b["h"], b["l"], b["c"]] for b in D[sig + 1:]],
        })
    return sym, out, "ok"


def init(days):
    global TEST_DAYS
    TEST_DAYS = days


def main():
    spy = os.path.join(CACHE, "charts", "SPY.json.gz")
    if not os.path.exists(spy):
        import urllib.request
        req = urllib.request.Request("https://query2.finance.yahoo.com/v8/finance/chart/SPY?range=60d&interval=5m",
                                     headers={"User-Agent": "paper-trader-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r, gzip.open(spy, "wb") as f:
            f.write(r.read())
    _, sbars, _ = tm.parse_bars(tm.chart_result(gzip.open(spy, "rt").read(), "SPY"), FAR_FUTURE)
    sdays = tm.split_sessions(sbars)
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=-5))).date().isoformat()
    sessions = [d for d in sorted(sdays) if d < today and len(sdays[d]) == tm.SESSION_BARS]
    days = sessions[WARMUP:]
    # SPY closes for the benchmark
    json.dump({"days": days, "spy_open": {d: sdays[d][0]["o"] for d in days},
               "spy_close": {d: sdays[d][tm.SESSION_BARS - 1]["c"] for d in days}},
              open(os.path.join(CACHE, "days.json"), "w"))
    print("%d full sessions, testing %d: %s to %s" % (len(sessions), len(days), days[0], days[-1]), flush=True)

    files = sorted(glob.glob(os.path.join(CACHE, "charts", "*.json.gz")))
    n = 0
    with mp.Pool(4, initializer=init, initargs=(days,)) as pool, \
            open(os.path.join(CACHE, "candidates.jsonl"), "w") as f:
        for i, (sym, rows, status) in enumerate(pool.imap_unordered(one, files, chunksize=4)):
            for r in rows:
                f.write(json.dumps(r) + "\n")
            n += len(rows)
            if (i + 1) % 100 == 0:
                print("stocks %d of %d, breakouts %d" % (i + 1, len(files), n), flush=True)
    print("done: %d breakouts" % n, flush=True)


if __name__ == "__main__":
    main()
