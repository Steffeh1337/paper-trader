"""The ICT / smart-money strategy from v3/STRATEGY.md, as exact rules on 5-minute bars (Romanian time).

A day's bars for one instrument: [[HH:MM, open, high, low, close], ...] (mid prices, Romanian time).

Setup (long shown; short is the mirror image):
  1. Liquidity sweep: a bar inside an active window trades below a liquidity level (it takes the lows):
       - the low of the day formed until 09:59 (the HOD/LOD setup; DAX, only 10:15-12:00),
       - the previous day's low, or the day's low so far at the start of the window (major),
       - a 5-minute swing low from the last two hours (minor).
  2. Within the next hour (12 bars), all three confirmations, in any order:
       MSS           a close above the last swing high before the sweep (market structure shift up)
       displacement  a big bullish bar in that move: body >= 1.5x the average body of the previous 20 bars
                     and >= 60% of the bar's range
       FVG           a bullish fair value gap in that move: a bar's low above the high two bars earlier
  3. Entry: a buy limit at the top of the FVG (its retest), valid for an hour while the window is open.
     Stop: below the sweep's lowest low minus a buffer. Target: 2R (R = entry - stop).
  The order is cancelled if the price reaches the target or the stop before it fills.

Management: one position at a time per instrument, at most max_trades a day; no new entries outside the
active windows; at 16:15 a position opened before 16:15 is kept only if it is at least +1R, otherwise it
is closed (the 16:15 rule); anything still open at flat_by is closed. Fills: a bar that touches both the
stop and the target counts as the stop; a stop gapped through fills at the bar's open; the spread is paid
on every trade (half at entry, half at exit).
"""

WINDOWS = [("10:15", "16:15"), ("16:45", "19:00"), ("20:00", "21:30")]
GOLDEN_DAX = ("10:15", "12:00")
HODLOD_UNTIL = "09:59"
RULE_1615 = "16:15"


def in_windows(t, windows):
    return any(a <= t < b for a, b in windows)


def window_of(t, windows):
    for a, b in windows:
        if a <= t < b:
            return (a, b)
    return None


def swings(bars, k=2):
    """Indices of swing highs and lows: a high (low) above (below) the k bars on each side."""
    hi, lo = [], []
    for i in range(k, len(bars) - k):
        h, l = bars[i][2], bars[i][3]
        if all(h > bars[j][2] for j in range(i - k, i + k + 1) if j != i):
            hi.append(i)
        if all(l < bars[j][3] for j in range(i - k, i + k + 1) if j != i):
            lo.append(i)
    return hi, lo


def find_setups(bars, prev_hl, windows, cfg, hodlod=True):
    """Every complete setup of the day, in the order its order becomes ready.
    prev_hl: (previous day's high, low) or None."""
    n = len(bars)
    if n < 30:
        return []
    sw_hi, sw_lo = swings(bars, 2)
    times = [b[0] for b in bars]
    k = 2
    out, used = [], set()

    def levels_at(i):
        """Liquidity levels that bar i can sweep: (side, price, name)."""
        t = times[i]
        lv = []
        if hodlod and GOLDEN_DAX[0] <= t < GOLDEN_DAX[1]:
            pre = [b for b in bars if b[0] <= HODLOD_UNTIL]
            if pre:
                lv.append(("high", max(b[2] for b in pre), "HOD until 09:59"))
                lv.append(("low", min(b[3] for b in pre), "LOD until 09:59"))
        if prev_hl:
            lv.append(("high", prev_hl[0], "previous day's high"))
            lv.append(("low", prev_hl[1], "previous day's low"))
        w = window_of(t, windows)
        before = [b for b in bars if b[0] < w[0]]
        if before:
            lv.append(("high", max(b[2] for b in before), "day's high before %s" % w[0]))
            lv.append(("low", min(b[3] for b in before), "day's low before %s" % w[0]))
        for p in sw_hi:
            if i - 24 <= p and p + k < i:
                lv.append(("high", bars[p][2], "swing high %s" % times[p]))
        for p in sw_lo:
            if i - 24 <= p and p + k < i:
                lv.append(("low", bars[p][3], "swing low %s" % times[p]))
        return lv

    for s in range(20, n):
        if not in_windows(times[s], windows):
            continue
        for side, level, name in levels_at(s):
            key = (side, round(level, 6))
            if key in used:
                continue
            # the level must still be untouched at the bar before the sweep
            if side == "high" and not (bars[s][2] > level and max(b[2] for b in bars[s - 1:s]) <= level):
                continue
            if side == "low" and not (bars[s][3] < level and min(b[3] for b in bars[s - 1:s]) >= level):
                continue
            used.add(key)
            st = _confirm(bars, s, side, sw_hi, sw_lo, cfg)
            if st:
                st.update(sweep_time=times[s], liquidity=name, level=level)
                out.append(st)
    out.sort(key=lambda x: x["ready"])
    return out


def _confirm(bars, s, side, sw_hi, sw_lo, cfg):
    n, k = len(bars), 2
    bodies = [abs(b[4] - b[1]) for b in bars[max(0, s - 20):s]]
    avg_body = sum(bodies) / len(bodies) if bodies else 0
    if side == "high":                              # highs taken -> look for a short
        piv = [p for p in sw_lo if p + k < s and p >= s - 24]
        if not piv:
            return None
        mss_level, ext, m = bars[piv[-1]][3], bars[s][2], None
        for j in range(s + 1, min(n, s + 13)):
            if bars[j][4] < mss_level:
                m = j
                break
            ext = max(ext, bars[j][2])
        if m is None:
            return None
        disp = any(bars[j][4] < bars[j][1] and (bars[j][1] - bars[j][4]) >= 1.5 * avg_body
                   and (bars[j][1] - bars[j][4]) >= 0.6 * (bars[j][2] - bars[j][3])
                   for j in range(s, m + 1))
        if not disp:
            return None
        fvg = None
        for i in range(s, min(m + 1, n - 2)):
            if bars[i][3] > bars[i + 2][2] and bars[i + 1][4] < bars[i + 1][1]:
                fvg = i
        if fvg is None:
            return None
        entry = bars[fvg + 2][2]
        stop = ext + cfg["buffer"]
        risk = stop - entry
        if risk <= cfg["min_risk"]:
            return None
        return {"side": "short", "ready": max(fvg + 2, m), "entry": entry, "stop": stop,
                "target": entry - cfg["rr"] * risk, "risk": risk, "mss_time": bars[m][0], "fvg_time": bars[fvg + 1][0]}
    piv = [p for p in sw_hi if p + k < s and p >= s - 24]
    if not piv:
        return None
    mss_level, ext, m = bars[piv[-1]][2], bars[s][3], None
    for j in range(s + 1, min(n, s + 13)):
        if bars[j][4] > mss_level:
            m = j
            break
        ext = min(ext, bars[j][3])
    if m is None:
        return None
    disp = any(bars[j][4] > bars[j][1] and (bars[j][4] - bars[j][1]) >= 1.5 * avg_body
               and (bars[j][4] - bars[j][1]) >= 0.6 * (bars[j][2] - bars[j][3])
               for j in range(s, m + 1))
    if not disp:
        return None
    fvg = None
    for i in range(s, min(m + 1, n - 2)):
        if bars[i][2] < bars[i + 2][3] and bars[i + 1][4] > bars[i + 1][1]:
            fvg = i
    if fvg is None:
        return None
    entry = bars[fvg + 2][3]
    stop = ext - cfg["buffer"]
    risk = entry - stop
    if risk <= cfg["min_risk"]:
        return None
    return {"side": "long", "ready": max(fvg + 2, m), "entry": entry, "stop": stop,
            "target": entry + cfg["rr"] * risk, "risk": risk, "mss_time": bars[m][0], "fvg_time": bars[fvg + 1][0]}


def simulate(bars, setups, windows, cfg, risk_money, value_per_point):
    """Trade the setups in order: one position at a time, at most max_trades a day.
    risk_money: account currency lost at the stop. value_per_point: account currency per point per unit.
    A setup that became ready while a position was open is dropped. Returns the trades."""
    n, times = len(bars), [b[0] for b in bars]
    trades, free_from = [], 0
    half = cfg["spread"] / 2.0
    for st in setups:
        if len(trades) >= cfg["max_trades"]:
            break
        if st["ready"] < free_from:
            continue
        filled = None
        for j in range(st["ready"] + 1, min(n, st["ready"] + 13)):
            if not in_windows(times[j], windows):
                break
            o, h, l = bars[j][1], bars[j][2], bars[j][3]
            if st["side"] == "long":
                if l <= st["entry"]:
                    filled = j
                    break
                if h >= st["target"]:
                    break                                    # ran to the target without a retest
            else:
                if h >= st["entry"]:
                    filled = j
                    break
                if l <= st["target"]:
                    break
        if filled is None:
            continue
        t = _manage(bars, filled, st, cfg)
        units = risk_money / (st["risk"] * value_per_point)
        units = min(units, cfg["max_notional"] / (st["entry"] * value_per_point))
        sign = 1 if st["side"] == "long" else -1
        pts = sign * (t["exit"] - st["entry"]) - 2 * half
        trades.append({"side": st["side"], "entry_time": times[filled], "entry": round(st["entry"], 2),
                       "stop": round(st["stop"], 2), "target": round(st["target"], 2), "exit_time": t["time"],
                       "exit": round(t["exit"], 2), "outcome": t["how"], "units": round(units, 4),
                       "pnl": round(units * pts * value_per_point, 2), "R": round(pts / st["risk"], 2),
                       "liquidity": st["liquidity"], "sweep_time": st["sweep_time"], "mss_time": st["mss_time"],
                       "fvg_time": st["fvg_time"]})
        free_from = t["index"] + 1
    return trades


def _manage(bars, j0, st, cfg):
    n = len(bars)
    long = st["side"] == "long"
    for j in range(j0, n):
        t, o, h, l, c = bars[j]
        if t >= cfg["flat_by"]:
            return {"exit": o, "time": t, "how": "flat by close", "index": j}
        if cfg.get("rule_1615") and j > j0 and t == RULE_1615 and bars[j0][0] < RULE_1615:
            open_r = ((o - st["entry"]) if long else (st["entry"] - o)) / st["risk"]
            if open_r < 1.0:
                return {"exit": o, "time": t, "how": "16:15 rule", "index": j}
        if long:
            if l <= st["stop"]:
                px = st["stop"] if j == j0 else min(o, st["stop"])
                return {"exit": px, "time": t, "how": "stop", "index": j}
            if j > j0 and h >= st["target"]:
                return {"exit": max(o, st["target"]), "time": t, "how": "target", "index": j}
        else:
            if h >= st["stop"]:
                px = st["stop"] if j == j0 else max(o, st["stop"])
                return {"exit": px, "time": t, "how": "stop", "index": j}
            if j > j0 and l <= st["target"]:
                return {"exit": min(o, st["target"]), "time": t, "how": "target", "index": j}
    last = bars[-1]
    return {"exit": last[4], "time": last[0], "how": "flat by close", "index": n - 1}
