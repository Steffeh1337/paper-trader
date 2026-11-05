#!/usr/bin/env python3
"""Backtest step 2: every opening-range breakout of every tested day, scored.

Reads the Alpaca bars saved by alpaca_fetch.py (Yahoo chart shape). For each
stock and each tested day (every session after the first WARMUP sessions, so
the Time Machine always has 60 days of history, as it does live):
  - 15-minute opening range = bars 9:30, 9:35, 9:40 (all three must exist)
  - signal = the first 5-minute bar from 9:45 on that CLOSES above the range high,
    closing no later than 15:30 (the skill's entry rule and window), at $5 or more
  - the skill's Time Machine runs on that plan (stop at the range midpoint,
    target 2R) on the 60 sessions before the day plus the day itself, with its
    clock pinned to the signal bar's close: expected R, odds, and the rule's
    60-day history. It never sees anything after the signal.
The bars after the signal are kept so step 3 can simulate fills for any rule.

  python3 backtest/candidates.py            # -> backtest/cache/candidates.jsonl
"""
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
DATA = os.path.join(CACHE, "alpaca")
WARMUP = 60          # sessions of history before the first tested day (the live Time Machine uses ~60)
REPLAYS = 2000       # per candidate (the live run uses 10,000; this keeps the backtest fast)
MIN_PRICE = 5.0
LAST_SIGNAL_MIN = 15 * 60 + 30
FAR_FUTURE = tm.parse_now("2030-01-01 00:00")


def clock(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def sliced_text(raw, keep):
    """The chart JSON with only the bars whose index is in keep (a range)."""
    r = raw["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    lo, hi = keep
    doc = {"chart": {"result": [{"meta": r["meta"], "timestamp": r["timestamp"][lo:hi],
                                 "indicators": {"quote": [{k: q[k][lo:hi] for k in q}]}}], "error": None}}
    return json.dumps(doc, separators=(",", ":"))


def one(path):
    sym = os.path.basename(path)[:-len(".json.gz")]
    try:
        raw = json.load(gzip.open(path, "rt"))
        result = raw["chart"]["result"][0]
        _, bars, _ = tm.parse_bars(result, FAR_FUTURE)
    except Exception as e:  # a bad or empty file: skip the stock
        return sym, [], "skip: %s" % e
    days = tm.split_sessions(bars)
    # index range of each date in the raw arrays (for the 60-day slices)
    stamps = result["timestamp"]
    first_idx, last_idx = {}, {}
    for i, ts in enumerate(stamps):
        d = tm.to_eastern(tm.utc_of(ts)).date().isoformat()
        first_idx.setdefault(d, i)
        last_idx[d] = i + 1
    out = []
    for n, d in enumerate(SESSIONS):
        if n < WARMUP or d not in days:
            continue
        s = days[d]
        if not all(k in s for k in (0, 1, 2)) or len(s) < 70:
            continue
        oh = max(s[k]["h"] for k in (0, 1, 2))
        ol = min(s[k]["l"] for k in (0, 1, 2))
        sig = None
        for i in range(3, tm.SESSION_BARS):
            close_min = tm.OPEN_MIN + (i + 1) * 5
            if close_min > LAST_SIGNAL_MIN:
                break
            if i in s and s[i]["c"] > oh:
                sig = i
                break
        if sig is None or s[sig]["c"] < MIN_PRICE:
            continue
        after = [s[k] for k in range(sig + 1, tm.SESSION_BARS) if k in s]
        if not after:
            continue
        entry = round(s[sig]["c"], 2)
        stop = math.floor((oh + ol) / 2 * 100) / 100
        if stop >= entry:
            continue
        target = round(entry + 2 * (entry - stop), 2)
        close_min = tm.OPEN_MIN + (sig + 1) * 5
        start_day = SESSIONS[n - WARMUP]
        lo = first_idx.get(start_day)
        if lo is None:
            lo = min(first_idx[x] for x in first_idx if x >= start_day)
        try:
            plan = tm.normalize_plan({"symbol": sym.replace(".", "-"), "side": "buy", "date": d, "entry": entry,
                                      "stop": stop, "target": target, "entry_time": tm.clock12(close_min),
                                      "setup": "orb", "opening_range": {"minutes": 15}, "stop_rule": "or_mid",
                                      "interval": "5m"})
            doc = tm.time_machine(sliced_text(raw, (lo, last_idx[d])), plan,
                                  tm.parse_now("%s %s" % (d, clock(close_min))), replays=REPLAYS)
        except Exception:
            continue
        mc, hist = doc["mc"], doc.get("history") or {}
        out.append({
            "date": d, "symbol": sym, "time": clock(close_min), "entry_ref": entry,
            "or_high": round(oh, 4), "or_low": round(ol, 4),
            "exp_R": mc["avg_R"], "p_target": mc["p_target"], "p_stop": mc["p_stop"],
            "hist_R": hist.get("avg_R"), "hist_n": hist.get("triggered"),
            "bars": [[b["start"], b["o"], b["h"], b["l"], b["c"]] for b in after],
        })
    return sym, out, "ok"


def init(sessions):
    global SESSIONS
    SESSIONS = sessions


def main():
    spy_raw = json.load(gzip.open(os.path.join(DATA, "SPY.json.gz"), "rt"))
    _, sbars, _ = tm.parse_bars(spy_raw["chart"]["result"][0], FAR_FUTURE)
    sdays = tm.split_sessions(sbars)
    sessions = [d for d in sorted(sdays) if len(sdays[d]) >= 70 and 0 in sdays[d]]
    tested = sessions[WARMUP:]
    last_slot = lambda d: sdays[d][max(sdays[d])]
    json.dump({"days": tested, "spy_open": {d: sdays[d][0]["o"] for d in tested},
               "spy_close": {d: last_slot(d)["c"] for d in tested}},
              open(os.path.join(CACHE, "days.json"), "w"))
    print("%d sessions, testing %d: %s to %s" % (len(sessions), len(tested), tested[0], tested[-1]), flush=True)

    files = sorted(f for f in glob.glob(os.path.join(DATA, "*.json.gz")) if not f.endswith("/SPY.json.gz"))
    n = 0
    with mp.Pool(4, initializer=init, initargs=(sessions,)) as pool, \
            open(os.path.join(CACHE, "candidates.jsonl"), "w") as f:
        for i, (sym, rows, status) in enumerate(pool.imap_unordered(one, files)):
            for r in rows:
                f.write(json.dumps(r) + "\n")
            n += len(rows)
            if (i + 1) % 50 == 0:
                print("stocks %d of %d, breakouts %d" % (i + 1, len(files), n), flush=True)
    print("done: %d breakouts" % n, flush=True)


if __name__ == "__main__":
    main()
