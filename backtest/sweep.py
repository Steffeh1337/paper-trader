#!/usr/bin/env python3
"""Backtest step 3: replay many rule variants on the scored breakouts.

The days are split in time: the first TRAIN_SHARE of them pick the winner, the
rest (never used for picking) check it. Every variant trades the same account
the live simulator does: EUR 200 a month (taken as $224 here), whole shares,
at most a third of the account per position, a 5% cash buffer, flat by 15:55,
fills at the open of the bar after the signal plus 0.05% slippage, and a bar
that touches both the stop and the target counts as the stop.

  python3 backtest/sweep.py    # -> backtest/results/*.json, *.csv
"""
import csv
import itertools
import json
import math
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "backtest", "cache")
OUT = os.path.join(ROOT, "backtest", "results")
TRAIN_SHARE = 0.6
DEPOSIT = 224.0
SLIP = 0.0005
POS_CAP = 0.33
BUFFER = 0.05
DAILY_LOSS = 0.10
MIN_TRAIN_TRADES = 30

GRID = {
    "min_exp_R": [None, 0.0, 0.05, 0.10, 0.20],    # None = take every breakout, no Time Machine filter
    "target_R": [1.0, 1.5, 2.0, 3.0],
    "stop": ["mid", "low"],                           # opening-range midpoint or low
    "breakeven": [False, True],                      # stop to entry once the trade is up 1R
    "last_entry": ["10:30", "15:30"],
    "max_trades": [3, 10],
}
CURRENT = {"min_exp_R": 0.0, "target_R": 2.0, "stop": "mid", "breakeven": False, "last_entry": "15:30",
           "max_trades": 10}


def fill(c, stop_kind, target_R, breakeven):
    """Simulated trade for one breakout under one exit rule, or None if not taken."""
    bars = c["bars"]
    if not bars:
        return None
    stop = math.floor(((c["or_high"] + c["or_low"]) / 2 if stop_kind == "mid" else c["or_low"]) * 100) / 100
    entry = round(bars[0][1] * (1 + SLIP), 2)
    if entry <= stop:
        return None
    rps = entry - stop
    target = round(entry + target_R * rps, 2)
    be_level = entry + rps
    armed = False
    for j, (start, o, h, l, cl) in enumerate(bars):
        if start >= "15:55":
            return {"entry": entry, "exit": round(o * (1 - SLIP), 2), "exit_time": "15:55", "rps": rps, "how": "close"}
        cur_stop = entry if armed else stop
        if l <= cur_stop:
            px = cur_stop if j == 0 else min(o, cur_stop)
            return {"entry": entry, "exit": round(px * (1 - SLIP), 2), "exit_time": start, "rps": rps,
                    "how": "breakeven" if armed else "stop"}
        if h >= target:
            px = target if j == 0 else max(o, target)
            return {"entry": entry, "exit": round(px, 2), "exit_time": start, "rps": rps, "how": "target"}
        if breakeven and h >= be_level:
            armed = True
    last = bars[-1]
    return {"entry": entry, "exit": round(last[4] * (1 - SLIP), 2), "exit_time": last[0], "rps": rps, "how": "close"}


def run(days, by_day, fills, p, risk=0.03, spy=None):
    """One variant over the given days -> stats."""
    cash = 0.0
    month = None
    deposited = 0.0
    spy_shares = 0.0
    trades = []
    curve = []
    max_dd = 0.0
    for d in days:
        if d[:7] != month:
            month = d[:7]
            cash += DEPOSIT
            deposited += DEPOSIT
            if spy:
                spy_shares += DEPOSIT / spy["spy_open"][d]
        eq0 = cash
        cap = eq0 * DAILY_LOSS
        open_pos, realized, n = [], 0.0, 0

        def close_until(t):
            nonlocal cash, realized
            for q in sorted(open_pos, key=lambda q: q["f"]["exit_time"]):
                if q["f"]["exit_time"] <= t:
                    pnl = q["qty"] * (q["f"]["exit"] - q["f"]["entry"])
                    cash += q["qty"] * q["f"]["exit"]
                    realized += pnl
                    trades.append({"date": d, "pnl": pnl, "R": (q["f"]["exit"] - q["f"]["entry"]) / q["f"]["rps"],
                                   "exp_R": q["c"]["exp_R"], "how": q["f"]["how"]})
                    open_pos.remove(q)

        for c in by_day.get(d, []):
            if c["time"] > p["last_entry"]:
                continue
            ok = p["min_exp_R"] is None or (c["exp_R"] > p["min_exp_R"] and (c["hist_R"] or 0) > 0)
            if not ok:
                continue
            close_until(c["time"])
            if n >= p["max_trades"]:
                continue
            open_risk = sum(q["qty"] * q["f"]["rps"] for q in open_pos)
            room = cap - max(0.0, -realized) - open_risk
            if room <= 0 or any(q["c"]["symbol"] == c["symbol"] for q in open_pos):
                continue
            f = fills[(id(c), p["stop"], p["target_R"], p["breakeven"])]
            if f is None:
                continue
            usable = cash - eq0 * BUFFER
            qty = min(int(eq0 * risk // f["rps"]), int(eq0 * POS_CAP // f["entry"]),
                      int(max(0, usable) // f["entry"]), int(room // f["rps"]))
            if qty < 1:
                continue
            cash -= qty * f["entry"]
            open_pos.append({"c": c, "f": f, "qty": qty})
            n += 1
        close_until("23:59")
        curve.append({"date": d, "equity": round(cash, 2), "deposited": deposited,
                      "spy": round(spy_shares * spy["spy_close"][d], 2) if spy else None})
    # drawdown on equity minus deposits (profit line)
    hi = -1e18
    for pt in curve:
        prof = pt["equity"] - pt["deposited"]
        hi = max(hi, prof)
        max_dd = max(max_dd, hi - prof)
    wins = [t for t in trades if t["pnl"] > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    final = curve[-1] if curve else {"equity": 0, "deposited": 0, "spy": 0}
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0,
        "avg_R": sum(t["R"] for t in trades) / len(trades) if trades else 0,
        "pnl": round(final["equity"] - final["deposited"], 2),
        "return_pct": (final["equity"] / final["deposited"] - 1) if final["deposited"] else 0,
        "spy_return_pct": (final["spy"] / final["deposited"] - 1) if spy and final["deposited"] else None,
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "max_drawdown": round(max_dd, 2),
        "curve": curve,
        "trade_list": trades,
    }


def label(p):
    return "min %s | %gR target | stop %s | %s | until %s | max %d" % (
        "none" if p["min_exp_R"] is None else "+%.2fR" % p["min_exp_R"], p["target_R"],
        "midpoint" if p["stop"] == "mid" else "range low", "breakeven at +1R" if p["breakeven"] else "no breakeven",
        p["last_entry"], p["max_trades"])


def main():
    os.makedirs(OUT, exist_ok=True)
    meta = json.load(open(os.path.join(CACHE, "days.json")))
    days = meta["days"]
    cands = [json.loads(l) for l in open(os.path.join(CACHE, "candidates.jsonl"))]
    by_day = defaultdict(list)
    for c in cands:
        by_day[c["date"]].append(c)
    for d in by_day:   # time order; same bar: best expected R first
        by_day[d].sort(key=lambda c: (c["time"], -c["exp_R"]))
    fills = {}
    for c in cands:
        for s, t, b in itertools.product(GRID["stop"], GRID["target_R"], GRID["breakeven"]):
            fills[(id(c), s, t, b)] = fill(c, s, t, b)
    cut = int(len(days) * TRAIN_SHARE)
    train, test = days[:cut], days[cut:]
    print("%d breakouts, %d days: train %s to %s (%d), test %s to %s (%d)" % (
        len(cands), len(days), train[0], train[-1], len(train), test[0], test[-1], len(test)), flush=True)

    rows = []
    keys = list(GRID)
    for combo in itertools.product(*GRID.values()):
        p = dict(zip(keys, combo))
        tr = run(train, by_day, fills, p, spy=meta)
        te = run(test, by_day, fills, p, spy=meta)
        rows.append({"params": p, "label": label(p), "train": tr, "test": te})
    eligible = [r for r in rows if r["train"]["trades"] >= MIN_TRAIN_TRADES]
    eligible.sort(key=lambda r: r["train"]["pnl"], reverse=True)
    winner = eligible[0]
    current = next(r for r in rows if r["params"] == CURRENT)

    risk_rows = []
    for risk in (0.01, 0.02, 0.03, 0.05, 0.08):
        risk_rows.append({"risk": risk, "train": run(train, by_day, fills, winner["params"], risk, meta),
                          "test": run(test, by_day, fills, winner["params"], risk, meta),
                          "all": run(days, by_day, fills, winner["params"], risk, meta)})

    slim = lambda s: {k: v for k, v in s.items() if k not in ("curve", "trade_list")}
    with open(os.path.join(OUT, "variants.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(keys + ["train_trades", "train_win_rate", "train_avg_R", "train_pnl", "train_return_pct",
                           "train_max_dd", "test_trades", "test_win_rate", "test_avg_R", "test_pnl",
                           "test_return_pct", "test_max_dd"])
        for r in sorted(rows, key=lambda r: r["train"]["pnl"], reverse=True):
            w.writerow([r["params"][k] for k in keys] + [r["train"][k] for k in ("trades", "win_rate", "avg_R", "pnl", "return_pct", "max_drawdown")]
                       + [r["test"][k] for k in ("trades", "win_rate", "avg_R", "pnl", "return_pct", "max_drawdown")])
    # forecast check: does a higher expected R mean better trades? (all days, current exit rule)
    cal = []
    for lo, hi, name in [(-9, 0, "below 0"), (0, 0.05, "0 to +0.05"), (0.05, 0.10, "+0.05 to +0.10"),
                         (0.10, 0.20, "+0.10 to +0.20"), (0.20, 9, "+0.20 and up")]:
        rs = [((f["exit"] - f["entry"]) / f["rps"]) for c in cands if lo <= c["exp_R"] < hi
              for f in [fills[(id(c), "mid", 2.0, False)]] if f]
        cal.append({"group": name, "breakouts": len(rs), "actual_R": round(sum(rs) / len(rs), 3) if rs else None})
    summary = {
        "days": {"train": [train[0], train[-1], len(train)], "test": [test[0], test[-1], len(test)]},
        "breakouts": len(cands), "variants": len(rows), "eligible": len(eligible),
        "winner": {"params": winner["params"], "label": winner["label"], "train": slim(winner["train"]), "test": slim(winner["test"])},
        "current": {"params": current["params"], "label": current["label"], "train": slim(current["train"]), "test": slim(current["test"])},
        "top10": [{"label": r["label"], "params": r["params"], "train": slim(r["train"]), "test": slim(r["test"])} for r in eligible[:10]],
        "test_rank_of_winner": sorted(rows, key=lambda r: r["test"]["pnl"], reverse=True).index(winner) + 1,
        "test_share_profitable": sum(1 for r in eligible if r["test"]["pnl"] > 0) / len(eligible),
        "risk": [{"risk": r["risk"], "train": slim(r["train"]), "test": slim(r["test"]), "all": slim(r["all"])} for r in risk_rows],
        "calibration": cal,
        "curves": {"winner": winner["test"]["curve"], "current": current["test"]["curve"],
                   "winner_all": run(days, by_day, fills, winner["params"], 0.03, meta)["curve"],
                   "current_all": run(days, by_day, fills, current["params"], 0.03, meta)["curve"]},
    }
    json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=1)
    print(json.dumps({k: summary[k] for k in ("days", "breakouts", "variants", "eligible", "test_rank_of_winner", "test_share_profitable")}, indent=1))
    for name in ("winner", "current"):
        s = summary[name]
        print("\n%s: %s" % (name.upper(), s["label"]))
        for part in ("train", "test"):
            x = s[part]
            print("  %-5s trades %3d  win %4.0f%%  avg %+.2fR  P&L $%+7.2f  return %+6.1f%%  SPY %+5.1f%%  max drawdown $%.2f"
                  % (part, x["trades"], 100 * x["win_rate"], x["avg_R"], x["pnl"], 100 * x["return_pct"],
                     100 * (x["spy_return_pct"] or 0), x["max_drawdown"]))
    print("\nTOP 10 BY TRAIN P&L (and their test):")
    for r in summary["top10"]:
        print("  train $%+7.2f  test $%+7.2f  (%3d/%3d trades)  %s" % (r["train"]["pnl"], r["test"]["pnl"], r["train"]["trades"], r["test"]["trades"], r["label"]))
    print("\nRISK PER TRADE (winner, all days):")
    for r in summary["risk"]:
        print("  %2.0f%%  P&L $%+7.2f  return %+6.1f%%  max drawdown $%.2f" % (100 * r["risk"], r["all"]["pnl"], 100 * r["all"]["return_pct"], r["all"]["max_drawdown"]))
    print("\nFORECAST CHECK:", cal)


if __name__ == "__main__":
    main()
