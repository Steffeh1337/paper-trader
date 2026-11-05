"""The trading rules shared by the daily run and the backtest.

fill(): one breakout -> simulated trade under one exit rule, from its 5-minute bars.
trade_day(): one day of breakouts (time order, same bar: best expected R first)
-> the trades taken and, for every breakout, what was decided and why.

Account rules (same live and in the backtest): whole shares, a position may use
at most POS_CAP of the day's starting equity, a BUFFER of it stays in cash, risk
per trade is a share of the day's starting equity, and no new trade once the
day's net loss plus the risk still open reaches the daily limit. Fills: the open
of the bar after the signal plus slippage; a bar touching both the stop and the
target counts as the stop; anything open is sold at the 15:55 bar's open.
"""
import math

SLIP = 0.0005
POS_CAP = 0.33
BUFFER = 0.05

DEFAULT_RULES = {
    "min_exp_R": 0.0,       # None: no Time Machine filter; a number: expected R above it AND positive history
    "target_R": 2.0,
    "stop": "mid",          # "mid": opening-range midpoint, "low": opening-range low
    "breakeven": False,     # move the stop to the entry once the trade is up 1R
    "last_entry": "15:30",
    "max_trades": 10,
    "risk": 0.03,
    "daily_loss": 0.10,
}


def fill(c, stop_kind, target_R, breakeven):
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
            return {"entry": entry, "entry_time": bars[0][0], "stop": stop, "target": target,
                    "exit": round(o * (1 - SLIP), 2), "exit_time": "15:55", "rps": rps, "how": "flat by close"}
        cur_stop = entry if armed else stop
        if l <= cur_stop:
            px = cur_stop if j == 0 else min(o, cur_stop)
            return {"entry": entry, "entry_time": bars[0][0], "stop": stop, "target": target,
                    "exit": round(px * (1 - SLIP), 2), "exit_time": start, "rps": rps,
                    "how": "breakeven" if armed else "stop"}
        if h >= target:
            px = target if j == 0 else max(o, target)
            return {"entry": entry, "entry_time": bars[0][0], "stop": stop, "target": target,
                    "exit": round(px, 2), "exit_time": start, "rps": rps, "how": "target"}
        if breakeven and h >= be_level:
            armed = True
    last = bars[-1]
    return {"entry": entry, "entry_time": bars[0][0], "stop": stop, "target": target,
            "exit": round(last[4] * (1 - SLIP), 2), "exit_time": last[0], "rps": rps, "how": "flat by close"}


def trade_day(cands, cash, rules, fill_of=None):
    """cands: one day's breakouts. cash: the account at the open (all flat).
    Returns (end_cash, trades, decisions)."""
    p = dict(DEFAULT_RULES, **rules)
    fill_of = fill_of or (lambda c: fill(c, p["stop"], p["target_R"], p["breakeven"]))
    eq0 = cash
    cap = eq0 * p["daily_loss"]
    open_pos, trades, decisions = [], [], {}
    realized = 0.0
    n = 0

    def close_until(t):
        nonlocal cash, realized
        for q in sorted(open_pos, key=lambda q: q["f"]["exit_time"]):
            if q["f"]["exit_time"] <= t:
                f = q["f"]
                pnl = q["qty"] * (f["exit"] - f["entry"])
                cash += q["qty"] * f["exit"]
                realized += pnl
                trades.append({"symbol": q["c"]["symbol"], "qty": q["qty"], "entry_time": f["entry_time"],
                               "entry": f["entry"], "stop": f["stop"], "target": f["target"],
                               "exit_time": f["exit_time"], "exit": f["exit"], "outcome": f["how"],
                               "pnl_usd": round(pnl, 2), "R": round((f["exit"] - f["entry"]) / f["rps"], 2),
                               "tm_expected_R": q["c"]["exp_R"]})
                open_pos.remove(q)

    for c in sorted(cands, key=lambda c: (c["time"], -c["exp_R"])):
        sym = c["symbol"]
        if c["time"] > p["last_entry"]:
            decisions[sym] = "skipped: after the entry window"
            continue
        if p["min_exp_R"] is not None:
            if c["exp_R"] <= p["min_exp_R"]:
                decisions[sym] = "skipped: expected R %s" % ("not positive" if p["min_exp_R"] == 0 else "below the minimum")
                continue
            if (c["hist_R"] or 0) <= 0:
                decisions[sym] = "skipped: history not positive"
                continue
        close_until(c["time"])
        if n >= p["max_trades"]:
            decisions[sym] = "skipped: %d trades already" % p["max_trades"]
            continue
        open_risk = sum(q["qty"] * q["f"]["rps"] for q in open_pos)
        room = cap - max(0.0, -realized) - open_risk
        if room <= 0:
            decisions[sym] = "skipped: daily loss limit reached"
            continue
        f = fill_of(c)
        if f is None:
            decisions[sym] = "no fill: opened below the stop"
            continue
        usable = cash - eq0 * BUFFER
        qty = min(int(eq0 * p["risk"] // f["rps"]), int(eq0 * POS_CAP // f["entry"]),
                  int(max(0.0, usable) // f["entry"]), int(room // f["rps"]))
        if qty < 1:
            decisions[sym] = "skipped: no cash or risk room for one share"
            continue
        cash -= qty * f["entry"]
        open_pos.append({"c": c, "f": f, "qty": qty})
        n += 1
        decisions[sym] = "traded"
    close_until("23:59")
    return round(cash, 2), trades, decisions
