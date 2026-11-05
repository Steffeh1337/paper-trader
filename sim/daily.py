#!/usr/bin/env python3
"""Daily paper-trading simulation (Option B: fully automatic, no broker).

Run once per trading day, after the US close. It replays the day with the
day-trader-skill tools, pinning each tool's clock (--now) so nothing sees
prices from later in the day:

  09:50 ET  scan every US stock for a 15-minute opening-range breakout
            rank the breakouts by the Time Machine's expected R
            for the best ones: entry -> exit plan (2R) -> size -> risk check
  fills     simulated from the day's real 5-minute bars:
            buy at the open of the next bar after the signal (+ slippage),
            then the stop, the target, or flat at 15:55, whichever comes first;
            a bar that touches both the stop and the target counts as the stop

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
RISK_PCT = 0.05          # max loss per trade, share of the account
DAILY_LOSS_PCT = 0.10    # max total risk per day
MAX_TRADES = 3
MAX_TM_CANDIDATES = 80   # breakouts that get a Time Machine run (the scan lists them A to Z, so check them all)
SCAN_AT = "09:50"        # first bar that can close above a 15-minute range
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
        "max_daily_loss_usd": round(equity * DAILY_LOSS_PCT, 2),
        "max_order_usd": round(equity, 2),
        "max_position_pct": 95,
        "cash_buffer_pct": 5,
        "max_trades_per_day": MAX_TRADES,
        "paper": True,
    })
    write_json(os.path.join(TRADER, "config.json"), cfg)
    return cfg


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
    log("%s  account $%.2f  max loss/trade $%.2f  daily risk cap $%.2f"
        % (day, equity_start, cfg["max_loss_per_trade_usd"], cfg["max_daily_loss_usd"]))

    now = "%s %s" % (day.isoformat(), SCAN_AT)
    if not os.path.exists(os.path.join(TRADER, "universe.txt")):
        tool("trader-scanner", "scan.py", "universe", check=True)
    code, out = tool("trader-scanner", "scan.py", "orb", "--now", now, "--at", SCAN_AT, "--quiet")
    log(out.splitlines()[-3] if out else "scan: no output")
    wl = read_json(os.path.join(TRADER, "watchlist.json"), {"hits": []})
    hits = [h for h in wl.get("hits", []) if h.get("price") and h["price"] <= equity_start * 0.9]
    log("%d breakouts at %s; %d cheap enough for at least one share." % (len(wl.get("hits", [])), SCAN_AT, len(hits)))

    # Rank by the Time Machine's expected R, using only data up to the signal.
    ranked = []
    decision = {}
    price_of = {h["symbol"]: h for h in hits}
    for h in hits[:MAX_TM_CANDIDATES]:
        sym = h["symbol"]
        c1, _ = tool("trader-entry", "entry.py", "check", sym, "--setup", "orb", "--now", now,
                     "--write", "trader/cand/%s-entry.json" % sym)
        c2, _ = tool("trader-exit", "exit.py", "plan", "--from", "trader/cand/%s-entry.json" % sym,
                     "--target", "2R", "--now", now, "--write", "trader/cand/%s-plan.json" % sym)
        if c2 != 0:
            decision[sym] = "no valid exit plan"
            continue
        c3, _ = tool("trader-timemachine", "timemachine.py", "run", "--plan", "trader/cand/%s-plan.json" % sym,
                     "--now", now, "--write", "trader/cand/%s-tm.json" % sym)
        tm = read_json(os.path.join(TRADER, "cand", "%s-tm.json" % sym))
        if c3 != 0 or not tm:
            decision[sym] = "time machine failed"
            continue
        mc, hist = tm["mc"], tm.get("history", {})
        ranked.append({"symbol": sym, "avg_R": mc["avg_R"], "p_target": mc["p_target"],
                       "p_stop": mc["p_stop"], "hist_avg_R": hist.get("avg_R")})
    ranked.sort(key=lambda r: r["avg_R"], reverse=True)
    for r in ranked:
        log("  %-6s expected %+.2fR  target first %4.1f%%  stop first %4.1f%%  history %+.2fR"
            % (r["symbol"], r["avg_R"], 100 * r["p_target"], 100 * r["p_stop"], r["hist_avg_R"] or 0))

    picks = [r for r in ranked if r["avg_R"] > 0 and (r["hist_avg_R"] or 0) > 0]
    for r in ranked:
        if r not in picks:
            decision[r["symbol"]] = "skipped: expected R not positive" if r["avg_R"] <= 0 else "skipped: history not positive"
    trades = []
    for r in picks:
        sym = r["symbol"]
        if len(trades) >= MAX_TRADES:
            decision[sym] = "skipped: already 3 trades"
            continue
        plan_src = os.path.join(TRADER, "cand", "%s-plan.json" % sym)
        write_json(os.path.join(TRADER, "plan.json"), read_json(plan_src))
        c, out = tool("trader-budget", "size.py", "--now", now, "--write")
        if c != 0:
            log("  %s: sizing refused: %s" % (sym, out.splitlines()[-1] if out else "?"))
            decision[sym] = "sizing refused"
            continue
        c, out = tool("trader-risk", "check.py", "--now", now, "--today", day.isoformat())
        verdict = read_json(os.path.join(TRADER, "state", "risk.json"), {})
        if c != 0 or "PASS" not in out.split("\n", 1)[0]:
            log("  %s: risk check BLOCKED: %s" % (sym, out.splitlines()[0] if out else "?"))
            decision[sym] = "risk check blocked"
            continue
        order = read_json(os.path.join(TRADER, "orders.json"))["orders"][0]
        qty, stop, target = int(order["qty"]), float(order["stop_loss_stop_price"]), float(order["take_profit_limit_price"])
        plan = read_json(plan_src)
        bars = session_bars(sym, day)
        sig = dt.datetime.strptime(plan["entry_time"], "%I:%M %p").strftime("%H:%M")
        fill = simulate(bars, sig, stop, target)
        if not fill or "skipped" in fill:
            log("  %s: no fill (%s)" % (sym, (fill or {}).get("skipped", "no bars")))
            decision[sym] = "no fill"
            continue
        if qty * fill["entry"] > pf["cash"]:
            qty = int(pf["cash"] // fill["entry"])
            if qty < 1:
                log("  %s: no cash left for a share at %.2f" % (sym, fill["entry"]))
                decision[sym] = "skipped: no cash left"
                continue
        tool("trader-broker", "ticket.py", "fill", "--symbol", sym, "--side", "buy", "--qty", qty,
             "--price", fill["entry"], "--date", day.isoformat(), check=True)
        tool("trader-broker", "ticket.py", "fill", "--symbol", sym, "--side", "sell", "--qty", qty,
             "--price", fill["exit"], "--date", day.isoformat(), check=True)
        pf = read_json(pf_path)
        pnl = round(qty * (fill["exit"] - fill["entry"]), 2)
        R = round((fill["exit"] - fill["entry"]) / (plan["entry"] - stop), 2) if plan["entry"] > stop else 0
        trades.append(sym)
        decision[sym] = "traded"
        append_csv(TRADES_CSV,
                   ["date", "symbol", "qty", "entry_time", "entry", "stop", "target", "exit_time", "exit",
                    "outcome", "pnl_usd", "R", "tm_expected_R"],
                   [day, sym, qty, fill["entry_time"], fill["entry"], stop, target, fill["exit_time"],
                    fill["exit"], fill["outcome"], pnl, R, r["avg_R"]])
        log("  TRADE %s: %d @ %.2f (%s) -> %.2f (%s, %s)  P&L $%+.2f"
            % (sym, qty, fill["entry"], fill["entry_time"], fill["exit"], fill["exit_time"], fill["outcome"], pnl))
    if not trades:
        log("  No trade today: no breakout with a positive expected R passed the rules.")

    for r in ranked:
        h = price_of.get(r["symbol"], {})
        append_csv(CANDS_CSV, ["date", "symbol", "price", "pct_over", "expected_R", "p_target", "p_stop",
                               "history_R", "decision"],
                   [day, r["symbol"], h.get("price"), h.get("pct_over"), r["avg_R"], r["p_target"], r["p_stop"],
                    r["hist_avg_R"], decision.get(r["symbol"], "")])
    append_csv(DAYS_CSV, ["date", "breakouts", "affordable", "analyzed", "positive", "traded"],
               [day, len(wl.get("hits", [])), len(hits), len(ranked), len(picks), len(trades)])

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
                             ("candidates", CANDS_CSV, ("symbol", "decision")), ("days", DAYS_CSV, ())):
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
        "scan_at": SCAN_AT, "target_r": 2, "flat_by": cfg.get("flat_by", "15:55"),
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
