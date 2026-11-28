#!/usr/bin/env python3
"""The ICT strategy (v3/ict.py) on the last ~60 trading days, with the data available without a CFD feed:
  DAX: Yahoo ^GDAXI 5-minute bars (cash index, 10:00-18:30 Romanian time only, so no 09:59 HOD/LOD
       and nothing after 18:30); Nasdaq-100: QQQ 5-minute bars from Alpaca (pre-market included).
No news filter (there is no free calendar history). Risk EUR 10 a trade on a fixed EUR 1,000.

  python3 v3/backtest.py
"""
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "v2"))
import common as C   # noqa: E402
import ict           # noqa: E402
import markets as M  # noqa: E402


def main():
    rules = json.load(open(os.path.join(HERE, "rules.json")))
    res = C.yahoo("^GDAXI", "60d", pause=3.0, tries=4)
    dax = {d: [r[:5] for r in rows] for d, rows in C.yahoo_bars(res, C.RO).items()}
    days = sorted(dax)
    start, end = days[0], (dt.date.fromisoformat(days[-1]) + dt.timedelta(days=1)).isoformat()
    qqq = M.alpaca_ro("QQQ", start, end)
    out = {}
    for name, data, inst in (("DAX (cash index, from 10:00)", dax, "DAX"), ("Nasdaq-100 (QQQ)", qqq, "NAS")):
        cfg = M.cfg(rules, inst, data_kind="yahoo" if inst == "DAX" else "alpaca")
        ds = sorted(data)
        trades, per_day = [], []
        for k in range(1, len(ds)):
            bars, prev = data[ds[k]], data[ds[k - 1]]
            if len(bars) < 30:
                continue
            prev_hl = (max(b[2] for b in prev), min(b[3] for b in prev))
            setups = ict.find_setups(bars, prev_hl, cfg["windows"], cfg, hodlod=cfg["hodlod"])
            vpp = 1.0 if inst == "DAX" else 1 / 1.16
            t = ict.simulate(bars, setups, cfg["windows"], cfg, 10.0, vpp)
            for x in t:
                x["date"] = ds[k]
            trades += t
            per_day.append(sum(x["pnl"] for x in t))
        n = len(trades)
        wins = sum(1 for x in trades if x["pnl"] > 0)
        r = {"days": len(per_day), "trades": n, "win_rate": round(wins / n, 3) if n else None,
             "avg_R": round(sum(x["R"] for x in trades) / n, 2) if n else None,
             "total_eur_on_1000": round(sum(per_day), 2),
             "outcomes": {o: sum(1 for x in trades if x["outcome"] == o) for o in {x["outcome"] for x in trades}},
             "long": sum(1 for x in trades if x["side"] == "long"), "short": sum(1 for x in trades if x["side"] == "short")}
        out[name] = r
        print(name, json.dumps(r))
        for x in trades[-6:]:
            print("   ", x["date"], x["side"], x["liquidity"], x["sweep_time"], "->", x["entry_time"], x["entry"],
                  "stop", x["stop"], "tgt", x["target"], x["outcome"], x["exit_time"], x["exit"], "R", x["R"])
    json.dump(out, open(os.path.join(HERE, "backtest_results.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
