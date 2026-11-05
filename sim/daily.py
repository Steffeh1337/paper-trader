#!/usr/bin/env python3
"""Daily paper-trading simulation (Option B: fully automatic, no broker).

Run once per trading day, after the US close. It replays the day with the
day-trader-skill tools, pinning each tool's clock (--now) so nothing sees
prices from later in the day:

  scan      every US stock's 15-minute opening-range breakouts, 9:50 to 15:30 ET,
            each with the time its 5-minute bar first closed above the range
  walk      the day in time order, one 5-minute bar at a time: book any exits
            that have happened, then for each breakout of that bar (if there is
            cash, risk room and fewer than 10 trades) entry -> exit plan (2R) ->
            Time Machine, all with --now pinned to that time; breakouts of the
            same bar are taken best expected R first, and only if the expected R
            and the history are both positive -> size -> risk check -> buy
  fills     simulated from the day's real 5-minute bars:
            buy at the open of the next bar after the signal (+ slippage),
            then the stop, the target, or flat at 15:55, whichever comes first;
            a bar that touches both the stop and the target counts as the stop
  limits    3% of the account at risk per trade; no new trade once the day's
            net loss plus the risk still open reaches 10% of the account

Money: the account gets EUR 200 at the start of each month, converted at that
day's EUR/USD rate. The same dollars buy SPY on the same day, as the benchmark.

  python3 sim/daily.py                 # today (New York)
  python3 sim/daily.py --date 2026-11-04

Paper only. Nothing here talks to a broker. Not financial advice.
"""

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, ".claude", "skills")
TRADER = os.path.join(ROOT, "trader")
SIM = os.path.join(ROOT, "sim")
STATE = os.path.join(SIM, "state.json")
TRADES_CSV = os.path.join(SIM, "trades.csv")
EQUITY_CSV = os.path.join(SIM, "equity.csv")
CANDS_CSV = os.path.join(SIM, "candidates.csv")
DAYS_CSV = os.path.join(SIM, "days.csv")
DASH = os.path.join(SIM, "dashboard")
RUNLOG = os.path.join(SIM, "logs")

MONTHLY_EUR = 200.0
RISK_PCT = 0.03          # max loss per trade, share of the account
DAILY_LOSS_PCT = 0.10    # stop new trades when the day's net loss plus open risk reaches this
MAX_TRADES = 10
MAX_POSITION_PCT = 33     # one position may use at most this share of the account, so ~3 can be open
MAX_TM_CANDIDATES = 150  # Time Machine runs per day at most
LAST_ENTRY = "15:30"     # breakouts from 9:50 up to this time are considered
SLIPPAGE = 0.0005        # 0.05 % worse on market fills
UA = "paper-trader-sim/1.0 (personal research)"


# ------------------------------------------------------------------ helpers

def log(msg):
    print(msg, flush=True)
    with open(os.path.join(RUNLOG, "latest.log"), "a") as f:
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


def tool(skill, script, *args, check=False):
    """Run one of the skill's scripts from the repo root; return (code, output)."""
    cmd = [sys.executable, "-I", os.path.join(SKILLS, skill, script)] + [str(a) for a in args]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800)
    out = (p.stdout + p.stderr).strip()
    if check and p.returncode != 0:
        raise RuntimeError("%s %s failed:\n%s" % (script, " ".join(map(str, args)), out))
    return p.returncode, out


def yahoo_chart(symbol, interval, rng):
    path = "/v8/finance/chart/%s?range=%s&interval=%s" % (symbol.replace(".", "-"), rng, interval)
    last = None
    for host in ("query2", "query1"):
        for attempt in range(3):
            try:
                req = urllib.request.Request("https://%s.finance.yahoo.com%s" % (host, path),
                                             headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.loads(r.read().decode())["chart"]["result"][0]
                q = res["indicators"]["quote"][0]
                bars = []
                for i, ts in enumerate(res.get("timestamp") or []):
                    o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                    if None in (o, h, l, c):
                        continue
                    bars.append({"t": dt.datetime.fromtimestamp(ts, NY), "o": o, "h": h, "l": l, "c": c})
                return bars
            except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("Yahoo chart for %s failed: %s" % (symbol, last))


def day_range(day):
    back = (dt.datetime.now(NY).date() - day).days
    return "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"


def session_bars(symbol, day):
    """The regular-session 5-minute bars of `day`, 9:30 to 15:55."""
    bars = yahoo_chart(symbol, "5m", day_range(day))
    return [b for b in bars if b["t"].date() == day and
            dt.time(9, 30) <= b["t"].time() <= dt.time(15, 55)]


def eurusd(day):
    bars = yahoo_chart("EURUSD=X", "1d", "1mo")
    on_or_before = [b for b in bars if b["t"].date() <= day]
    return round((on_or_before or bars)[-1]["c"], 5)


# ------------------------------------------------------------------ fills

def simulate(bars, signal_hhmm, stop, target):
    """Entry at the open of the bar that starts at the signal time (the bar
    after the one that closed above the range). Returns a dict or None."""
    hh, mm = map(int, signal_hhmm.split(":"))
    idx = next((i for i, b in enumerate(bars) if b["t"].time() >= dt.time(hh, mm)), None)
    if idx is None:
        return None
    entry_bar = bars[idx]
    entry = round(entry_bar["o"] * (1 + SLIPPAGE), 2)
    if entry <= stop:
        return {"skipped": "opened at or below the stop (%.2f <= %.2f)" % (entry, stop)}
    for b in bars[idx:]:
        if b["t"].time() >= dt.time(15, 55):
            return {"entry": entry, "entry_time": entry_bar["t"].strftime("%H:%M"),
                    "exit": round(b["o"] * (1 - SLIPPAGE), 2), "exit_time": "15:55", "outcome": "flat by close"}
        if b["l"] <= stop:      # stop first when a bar touches both (conservative)
            px = min(b["o"], stop) if b is not entry_bar else stop
            return {"entry": entry, "entry_time": entry_bar["t"].strftime("%H:%M"),
                    "exit": round(px * (1 - SLIPPAGE), 2), "exit_time": b["t"].strftime("%H:%M"), "outcome": "stop"}
        if b["h"] >= target:
            px = max(b["o"], target) if b is not entry_bar else target
            return {"entry": entry, "entry_time": entry_bar["t"].strftime("%H:%M"),
                    "exit": round(px, 2), "exit_time": b["t"].strftime("%H:%M"), "outcome": "target"}
    last = bars[-1]
    return {"entry": entry, "entry_time": entry_bar["t"].strftime("%H:%M"),
            "exit": round(last["c"] * (1 - SLIPPAGE), 2), "exit_time": last["t"].strftime("%H:%M"),
            "outcome": "flat by close"}


# ------------------------------------------------------------------ the day

def configure(equity):
    cfg = read_json(os.path.join(TRADER, "config.json"))
    cfg.update({
        "budget": round(equity, 2),
        "max_loss_per_trade_usd": round(equity * RISK_PCT, 2),
        "max_daily_loss_usd": round(equity, 2),   # sim/daily.py enforces the real daily limit
        "max_order_usd": round(equity, 2),
        "max_position_pct": MAX_POSITION_PCT,
        "cash_buffer_pct": 5,
        "max_trades_per_day": MAX_TRADES,
        "paper": True,
    })
    write_json(os.path.join(TRADER, "config.json"), cfg)
    return cfg


def reason(out, prefix=None):
    """The informative line of a tool's output (not its disclaimer or warnings)."""
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    if prefix:
        hit = [l for l in lines if l.startswith(prefix)]
        if hit:
            return hit[0]
    skip = ("Education and tooling", "WARNING", "Next:", "A stop is", "Nothing")
    useful = [l for l in lines if not l.startswith(skip)]
    return (useful[-1] if useful else (lines[-1] if lines else "?"))[:200]


def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def run(day):
    os.makedirs(RUNLOG, exist_ok=True)
    open(os.path.join(RUNLOG, "latest.log"), "w").close()
    state = read_json(STATE, {"days": [], "deposits": [], "spy_shares": 0.0})
    if day.isoformat() in state["days"]:
        log("%s was already simulated. Nothing to do." % day)
        return 0

    spy = session_bars("SPY", day)
    if not spy:
        log("%s: no SPY bars, so the market was closed (weekend or holiday). Nothing to do." % day)
        return 0

    pf_path = os.path.join(TRADER, "portfolio.json")
    pf = read_json(pf_path, {"cash": 0.0, "positions": []})
    if pf.get("positions"):
        raise RuntimeError("positions left open from an earlier day: %s" % pf["positions"])

    # Monthly deposit, and the SPY benchmark buys with the same dollars.
    month = day.strftime("%Y-%m")
    if not any(d["month"] == month for d in state["deposits"]):
        rate = eurusd(day)
        usd = round(MONTHLY_EUR * rate, 2)
        pf["cash"] = round(pf.get("cash", 0.0) + usd, 2)
        spy_open = spy[0]["o"]
        state["spy_shares"] = state.get("spy_shares", 0.0) + usd / spy_open
        state["deposits"].append({"month": month, "date": day.isoformat(), "eur": MONTHLY_EUR,
                                  "eurusd": rate, "usd": usd, "spy_price": round(spy_open, 2)})
        write_json(pf_path, pf)
        log("Deposit: EUR %.0f at %.4f = $%.2f. Benchmark bought SPY at $%.2f." % (MONTHLY_EUR, rate, usd, spy_open))

    equity_start = pf["cash"]
    cfg = configure(equity_start)
    log("%s  account $%.2f  max loss/trade $%.2f  daily loss limit $%.2f"
        % (day, equity_start, cfg["max_loss_per_trade_usd"], equity_start * DAILY_LOSS_PCT))

    if not os.path.exists(os.path.join(TRADER, "universe.txt")):
        tool("trader-scanner", "scan.py", "universe", check=True)
    # One scan at the end of the entry window lists every opening-range breakout
    # of the day with the time it fired; the day is then walked in time order.
    scan_now = "%s %s" % (day.isoformat(), LAST_ENTRY)
    code, out = tool("trader-scanner", "scan.py", "orb", "--now", scan_now, "--at", LAST_ENTRY,
                     "--until", LAST_ENTRY, "--max-confirm", "0", "--quiet")
    log(out.splitlines()[-3] if out else "scan: no output")
    wl = read_json(os.path.join(TRADER, "watchlist.json"), {"hits": []})
    all_hits = wl.get("hits", [])
    for h in all_hits:
        h["hhmm"] = dt.datetime.strptime(h["at"], "%I:%M %p").strftime("%H:%M")
    hits = sorted((h for h in all_hits if h.get("price") and h["price"] <= equity_start * MAX_POSITION_PCT / 100
                   and h["hhmm"] <= LAST_ENTRY), key=lambda h: (h["hhmm"], h["symbol"]))
    log("%d breakouts between 9:50 and %s; %d cheap enough for a position of at most a third of the account."
        % (len(all_hits), LAST_ENTRY, len(hits)))

    daily_cap = round(equity_start * DAILY_LOSS_PCT, 2)
    open_pos = []           # {symbol, qty, risk, fill, ...} until their exit time
    realized = 0.0
    ranked, decision, trades = [], {}, []
    bar_cache = {}
    analyzed = 0

    def close_until(hhmm):
        """Book the exits of open trades that happened at or before hhmm."""
        nonlocal realized
        for p in sorted(open_pos, key=lambda p: p["fill"]["exit_time"]):
            if p["fill"]["exit_time"] <= hhmm:
                f = p["fill"]
                tool("trader-broker", "ticket.py", "fill", "--symbol", p["symbol"], "--side", "sell",
                     "--qty", p["qty"], "--price", f["exit"], "--date", day.isoformat(), check=True)
                pnl = round(p["qty"] * (f["exit"] - f["entry"]), 2)
                realized += pnl
                R = round((f["exit"] - f["entry"]) / p["risk_per_share"], 2) if p["risk_per_share"] else 0
                append_csv(TRADES_CSV,
                           ["date", "symbol", "qty", "entry_time", "entry", "stop", "target", "exit_time", "exit",
                            "outcome", "pnl_usd", "R", "tm_expected_R"],
                           [day, p["symbol"], p["qty"], f["entry_time"], f["entry"], p["stop"], p["target"],
                            f["exit_time"], f["exit"], f["outcome"], pnl, R, p["exp_R"]])
                log("    EXIT %s %s at %.2f (%s)  P&L $%+.2f  day so far $%+.2f"
                    % (f["exit_time"], p["symbol"], f["exit"], f["outcome"], pnl, realized))
                open_pos.remove(p)

    def blocked():
        """Why no new trade can be taken right now, or None."""
        open_risk = sum(p["qty"] * p["risk_per_share"] for p in open_pos)
        if len(trades) >= MAX_TRADES:
            return "skipped: %d trades already" % MAX_TRADES
        if max(0.0, -realized) + open_risk >= daily_cap:
            return "skipped: daily loss limit reached"
        return None

    # Breakouts that fire in the same 5-minute bar compete: all of them get a
    # Time Machine run and the best expected R goes first. Across the day it is
    # first come, first served, since a later breakout is not known yet.
    groups = {}
    for h in hits:
        groups.setdefault(h["hhmm"], []).append(h)
    for t in sorted(groups):
        close_until(t)
        now = "%s %s" % (day.isoformat(), t)
        pf = read_json(pf_path, pf)
        eligible = []
        for h in groups[t]:
            sym = h["symbol"]
            why = blocked()
            if why:
                decision[sym] = (t, why, None)
                continue
            if any(p["symbol"] == sym for p in open_pos):
                decision[sym] = (t, "skipped: already holding it", None)
                continue
            if pf["cash"] - equity_start * 0.05 < h["price"]:   # the 5% cash buffer stays untouched
                decision[sym] = (t, "skipped: cash tied up in open trades", None)
                continue
            if analyzed >= MAX_TM_CANDIDATES:
                decision[sym] = (t, "skipped: analysis limit for the day", None)
                continue
            analyzed += 1
            tool("trader-entry", "entry.py", "check", sym, "--setup", "orb", "--now", now,
                 "--write", "trader/cand/%s-entry.json" % sym)
            c2, _ = tool("trader-exit", "exit.py", "plan", "--from", "trader/cand/%s-entry.json" % sym,
                         "--target", "2R", "--now", now, "--write", "trader/cand/%s-plan.json" % sym)
            if c2 != 0:
                decision[sym] = (t, "no valid exit plan", None)
                continue
            c3, _ = tool("trader-timemachine", "timemachine.py", "run", "--plan", "trader/cand/%s-plan.json" % sym,
                         "--now", now, "--write", "trader/cand/%s-tm.json" % sym)
            tm = read_json(os.path.join(TRADER, "cand", "%s-tm.json" % sym))
            if c3 != 0 or not tm:
                decision[sym] = (t, "time machine failed", None)
                continue
            mc, hist = tm["mc"], tm.get("history", {})
            r = {"symbol": sym, "avg_R": mc["avg_R"], "p_target": mc["p_target"], "p_stop": mc["p_stop"],
                 "hist_avg_R": hist.get("avg_R")}
            ranked.append(r)
            if r["avg_R"] <= 0:
                decision[sym] = (t, "skipped: expected R not positive", r)
            elif (r["hist_avg_R"] or 0) <= 0:
                decision[sym] = (t, "skipped: history not positive", r)
            else:
                eligible.append(r)

        for r in sorted(eligible, key=lambda r: r["avg_R"], reverse=True):
            sym = r["symbol"]
            why = blocked()
            if why:
                decision[sym] = (t, why.replace("skipped:", "skipped (a better one at the same time went first):"), r)
                continue
            pf = read_json(pf_path, pf)
            plan_src = os.path.join(TRADER, "cand", "%s-plan.json" % sym)
            plan = read_json(plan_src)
            write_json(os.path.join(TRADER, "plan.json"), plan)
            c, out = tool("trader-budget", "size.py", "--now", now, "--write")
            if c != 0:
                log("  %s %s: sizing refused: %s" % (t, sym, reason(out)))
                decision[sym] = (t, "sizing refused", r)
                continue
            c, out = tool("trader-risk", "check.py", "--now", now, "--today", day.isoformat())
            if c != 0 or not any(line.startswith("PASS") for line in out.splitlines()):
                log("  %s %s: risk check BLOCKED: %s" % (t, sym, reason(out, "BLOCK")))
                decision[sym] = (t, "risk check blocked", r)
                continue
            order = read_json(os.path.join(TRADER, "orders.json"))["orders"][0]
            qty, stop, target = int(order["qty"]), float(order["stop_loss_stop_price"]), float(order["take_profit_limit_price"])
            if sym not in bar_cache:
                bar_cache[sym] = session_bars(sym, day)
            sig = dt.datetime.strptime(plan["entry_time"], "%I:%M %p").strftime("%H:%M")
            fill = simulate(bar_cache[sym], sig, stop, target)
            if not fill or "skipped" in fill:
                decision[sym] = (t, "no fill", r)
                continue
            risk_ps = max(0.01, fill["entry"] - stop)
            open_risk = sum(p["qty"] * p["risk_per_share"] for p in open_pos)
            room = daily_cap - max(0.0, -realized) - open_risk
            qty = min(qty, int(pf["cash"] // fill["entry"]), int(room // risk_ps))
            if qty < 1:
                decision[sym] = (t, "skipped: no cash or daily risk room left", r)
                continue
            tool("trader-broker", "ticket.py", "fill", "--symbol", sym, "--side", "buy", "--qty", qty,
                 "--price", fill["entry"], "--date", day.isoformat(), check=True)
            open_pos.append({"symbol": sym, "qty": qty, "stop": stop, "target": target, "fill": fill,
                             "risk_per_share": risk_ps, "exp_R": r["avg_R"]})
            trades.append(sym)
            decision[sym] = (t, "traded", r)
            log("  %s BUY %d %s at %.2f  stop %.2f  target %.2f  risk $%.2f  expected %+.2fR"
                % (fill["entry_time"], qty, sym, fill["entry"], stop, target, qty * risk_ps, r["avg_R"]))
    close_until("23:59")
    if not trades:
        log("  No trade today: no breakout with a positive expected R passed the rules.")

    for h in hits:
        if h["symbol"] not in decision:
            continue
        t, why, r = decision[h["symbol"]]
        r = r or {}
        append_csv(CANDS_CSV, ["date", "time", "symbol", "price", "pct_over", "expected_R", "p_target", "p_stop",
                               "history_R", "decision"],
                   [day, t, h["symbol"], h.get("price"), h.get("pct_over"), r.get("avg_R"), r.get("p_target"),
                    r.get("p_stop"), r.get("hist_avg_R"), why])
    append_csv(DAYS_CSV, ["date", "breakouts", "affordable", "analyzed", "positive", "traded"],
               [day, len(all_hits), len(hits), len(ranked),
                sum(1 for r in ranked if r["avg_R"] > 0 and (r["hist_avg_R"] or 0) > 0), len(trades)])

    pf = read_json(pf_path, pf)
    deposited = sum(d["usd"] for d in state["deposits"])
    spy_value = state["spy_shares"] * spy[-1]["c"]
    append_csv(EQUITY_CSV, ["date", "equity_usd", "deposited_usd", "spy_benchmark_usd", "trades"],
               [day, round(pf["cash"], 2), round(deposited, 2), round(spy_value, 2), len(trades)])
    state["days"].append(day.isoformat())
    write_json(STATE, state)
    log("End of day: account $%.2f | deposited $%.2f | SPY benchmark $%.2f"
        % (pf["cash"], deposited, spy_value))
    write_summary(state)
    export_dashboard(state, day)
    return 0


def export_dashboard(state, day):
    """JSON copies of the logs for the dashboard (uploaded after each run)."""
    os.makedirs(DASH, exist_ok=True)
    num = lambda v: None if v in ("", None) else (float(v) if any(ch in v for ch in ".e") else int(v))
    for name, path, text in (("equity", EQUITY_CSV, ()), ("trades", TRADES_CSV, ("symbol", "entry_time", "exit_time", "outcome")),
                             ("candidates", CANDS_CSV, ("symbol", "decision", "time")), ("days", DAYS_CSV, ())):
        rows = []
        if os.path.exists(path):
            for row in csv.DictReader(open(path)):
                rows.append({k: (v if k == "date" or k in text else num(v)) for k, v in row.items()})
        write_json(os.path.join(DASH, name + ".json"), rows)
    cfg = read_json(os.path.join(TRADER, "config.json"), {})
    write_json(os.path.join(DASH, "status.json"), [{
        "last_run_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_session": day.isoformat(),
        "monthly_eur": MONTHLY_EUR,
        "risk_pct": RISK_PCT, "daily_risk_pct": DAILY_LOSS_PCT, "max_trades": MAX_TRADES,
        "scan_at": "09:50 to " + LAST_ENTRY, "target_r": 2, "flat_by": cfg.get("flat_by", "15:55"),
        "max_loss_per_trade_usd": cfg.get("max_loss_per_trade_usd"),
        "deposits": len(state["deposits"]),
        "eur_deposited": MONTHLY_EUR * len(state["deposits"]),
    }])


def write_summary(state):
    rows = list(csv.DictReader(open(EQUITY_CSV))) if os.path.exists(EQUITY_CSV) else []
    trades = list(csv.DictReader(open(TRADES_CSV))) if os.path.exists(TRADES_CSV) else []
    if not rows:
        return
    last = rows[-1]
    eq, dep, spy = float(last["equity_usd"]), float(last["deposited_usd"]), float(last["spy_benchmark_usd"])
    wins = sum(1 for t in trades if float(t["pnl_usd"]) > 0)
    lines = [
        "# Paper-trading results", "",
        "Simulated with the day-trader-skill, EUR 200 a month, paper only. Updated %s." % last["date"], "",
        "| | USD |", "|---|---:|",
        "| Deposited | %.2f |" % dep,
        "| Day-trading account | %.2f (%+.1f%%) |" % (eq, 100 * (eq - dep) / dep if dep else 0),
        "| Same money in SPY | %.2f (%+.1f%%) |" % (spy, 100 * (spy - dep) / dep if dep else 0), "",
        "Trading days: %d. Trades: %d, winners %d, total P&L $%.2f."
        % (len(rows), len(trades), wins, sum(float(t["pnl_usd"]) for t in trades)), "",
        "Details: `sim/trades.csv`, `sim/equity.csv`, last run's log in `sim/logs/latest.log`.", "",
        "A few weeks is a tiny sample: treat these numbers as noise until there are many trades.",
        "Not financial advice.", "",
    ]
    with open(os.path.join(SIM, "RESULTS.md"), "w") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in New York)")
    a = ap.parse_args()
    day = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(NY).date()
    if day == dt.datetime.now(NY).date() and dt.datetime.now(NY).time() < dt.time(16, 5):
        print("The session for %s is not over yet (it is %s in New York). Run after 16:05."
              % (day, dt.datetime.now(NY).strftime("%H:%M")))
        return 1
    return run(day)


if __name__ == "__main__":
    sys.exit(main())
