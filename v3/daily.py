#!/usr/bin/env python3
"""v3 daily paper-trading run: the ICT / smart-money strategy (v3/STRATEGY.md, rules in v3/ict.py)
on the DAX 40 and the Nasdaq-100 as CFDs (long and short), one EUR account. Paper only, never places orders.

Once a day after 22:55 Romanian time (everything is flat by then):
  news      no trading on a day with a high-impact (red folder) event: EUR or USD for the DAX, USD for
            the Nasdaq-100 (ForexFactory weekly calendar). A blocked day is still simulated as a reference.
  windows   Romanian time, entries only 10:15-16:15, 16:45-19:00, 20:00-21:30 (the DAX in all three, the
            Nasdaq-100 in the New York ones); the DAX HOD/LOD setup (high/low until 09:59) in 10:15-12:00.
  risk      1% of the account at the stop per trade, target 2R, at most 3 trades per instrument a day,
            one position at a time, 1:20 leverage cap, 16:15 rule, flat by 22:55.

  python3 v3/daily.py [--date YYYY-MM-DD]
"""
import argparse
import csv
import datetime as dt
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "v2"))
import common as C   # noqa: E402
import ict           # noqa: E402
import markets as M  # noqa: E402

V2 = __import__("daily")             # v2/daily.py: helpers (ECB rate, CSV and JSON files) re-pointed below

LIVE = os.path.join(HERE, "live")
for name, path in (("LIVE", LIVE), ("STATE", os.path.join(LIVE, "state.json")), ("TRADES", os.path.join(LIVE, "trades.csv")),
                   ("EQUITY", os.path.join(LIVE, "equity.csv")), ("SESSIONS_CSV", os.path.join(LIVE, "sessions.csv")),
                   ("LOG", os.path.join(LIVE, "logs", "latest.log")), ("DASH", os.path.join(LIVE, "dashboard"))):
    setattr(V2, name, path)
RULES = os.path.join(HERE, "rules.json")
TRADE_COLS = ["date", "session", "symbol", "currency", "qty", "entry_time", "entry", "stop", "target", "exit_time",
              "exit", "outcome", "pnl", "pnl_eur", "R", "side", "setup"]
log = V2.log


def migrate():
    """First run: carry the v2 account, history and benchmarks over."""
    if os.path.exists(V2.STATE):
        return
    os.makedirs(os.path.join(LIVE, "logs"), exist_ok=True)
    src = os.path.join(ROOT, "v2", "live")
    for f in ("state.json", "equity.csv", "sessions.csv"):
        if os.path.exists(os.path.join(src, f)):
            shutil.copy(os.path.join(src, f), os.path.join(LIVE, f))
    rows = list(csv.DictReader(open(os.path.join(src, "trades.csv")))) if os.path.exists(os.path.join(src, "trades.csv")) else []
    with open(V2.TRADES, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS)
        w.writeheader()
        for r in rows:
            r.setdefault("side", "long")
            r.setdefault("setup", "opening-range breakout (v2)")
            w.writerow({k: r.get(k, "") for k in TRADE_COLS})
    st = V2.read_json(V2.STATE, {})
    st["strategy_since"] = None
    V2.write_json(V2.STATE, st)
    log("Migrated the v2 account and history to v3.")


def write_trade(day, t):
    with open(V2.TRADES, "a", newline="") as f:
        csv.writer(f).writerow([day, t["session"], t["symbol"], t["currency"], t["units"], t["entry_time"], t["entry"],
                                t["stop"], t["target"], t["exit_time"], t["exit"], t["outcome"], t["pnl_ccy"], t["pnl"],
                                t["R"], t["side"], t["setup"]])
    log("  %s %s %s %s %g @ %s -> %s %s (%s) %+.2fR  EUR %+.2f   [%s]" % (
        t["session"], t["entry_time"], t["symbol"], t["side"], t["units"], t["entry"], t["exit_time"], t["exit"],
        t["outcome"], t["R"], t["pnl"], t["setup"]))


def get_bars(inst, rules, day):
    """(today's bars, previous trading day's bars, source, cfg) for one instrument, or (None, ...) if closed."""
    I = rules["instruments"][inst]
    o = M.oanda_ro(I["oanda"], day - dt.timedelta(days=5), day)
    if o and o["bars"].get(day.isoformat()):
        days = sorted(d for d in o["bars"] if d <= day.isoformat())
        prev = o["bars"][days[-2]] if len(days) > 1 else None
        cfg = M.cfg(rules, inst, "oanda")
        if o["spread"]:
            cfg["spread"] = max(cfg["spread"], o["spread"])
        return o["bars"][day.isoformat()], prev, "OANDA %s (spread %.1f)" % (I["oanda"], cfg["spread"]), cfg
    if inst == "DAX":
        data = M.yahoo_dax_ro("5d")
        src = "Yahoo ^GDAXI cash index 10:00-18:30 (no CFD feed: no 09:59 HOD/LOD, nothing after 18:30)"
        cfg = M.cfg(rules, inst, "yahoo")
    else:
        data = M.alpaca_ro(I["alpaca"], (day - dt.timedelta(days=7)).isoformat(), (day + dt.timedelta(days=1)).isoformat())
        src = "Alpaca QQQ (no CFD feed)"
        cfg = M.cfg(rules, inst, "alpaca")
    days = sorted(d for d in data if d <= day.isoformat())
    if not days or days[-1] != day.isoformat():
        return None, None, src, cfg
    return data[days[-1]], (data[days[-2]] if len(days) > 1 else None), src, cfg


def run(day):
    rules, state = V2.read_json(RULES), None
    migrate()
    state = V2.load_state()
    if day.isoformat() in state.get("v3_days", []):
        log("%s was already simulated. Nothing to do." % day)
        return 0
    rate = V2.ecb_eurusd(day)
    if state.get("cash_usd", 0) > 0:                       # CFDs settle in the EUR account: one conversion back
        V2.convert(state, {"fx_fee": rules["fx_fee"]}, rate, usd_to_eur=state["cash_usd"])
    if state.get("strategy_since") is None:
        state["strategy_since"] = day.isoformat()

    # benchmarks (the same deposits held in the DAX ETF and in QQQ) and the market calendar
    etf = C.yahoo_bars(C.yahoo("EXS1.DE", "5d", pause=3.0, tries=4), C.RO).get(day.isoformat())
    qqq = M.alpaca_ro("QQQ", day.isoformat(), (day + dt.timedelta(days=1)).isoformat()).get(day.isoformat(), [])
    qqq_reg = [b for b in qqq if V2.ny_to_ro(day, "09:30") <= b[0] < V2.ny_to_ro(day, "16:00")]
    open_mkt = {"DAX": bool(etf), "NAS": len(qqq_reg) >= 30}
    if not any(open_mkt.values()):
        log("%s: both markets closed (weekend or holiday). Nothing to do." % day)
        return 0
    V2.deposit(state, rules, day, rate, etf[0][1] if etf else None)
    if state.get("pending_qqq_eur") and qqq_reg:
        state["qqq_units"] += state["pending_qqq_eur"] * rate / qqq_reg[0][1]
        state["pending_qqq_eur"] = 0.0
    news = M.red_folder(day, os.path.join(LIVE, "calendar", "%s-W%02d.json" % day.isocalendar()[:2]))
    equity0 = state["cash_eur"]
    risk_eur = round(equity0 * rules["risk_per_trade"], 2)
    log("%s | account EUR %.2f | risk per trade EUR %.2f | red-folder news: %s" % (
        day, equity0, risk_eur, "calendar unavailable" if news is None else
        (", ".join("%s %s %s" % n for n in news) or "none")))

    n_all = 0
    for inst, session in (("DAX", "EU"), ("NAS", "US")):
        I = rules["instruments"][inst]
        if not open_mkt[inst]:
            log("%s: closed today." % I["name"])
            continue
        bars, prev, src, cfg = get_bars(inst, rules, day)
        if not bars:
            log("%s: no price data today (%s)." % (I["name"], src))
            V2.append_csv(V2.SESSIONS_CSV, ["date", "session", "status", "breakouts", "trades", "pnl_eur", "shadow_eur", "reason"],
                          [day, session, "sat out", 0, 0, 0.0, "", "no price data (%s)" % src])
            continue
        cfg["max_notional"] = rules["leverage"] * equity0
        prev_hl = (max(b[2] for b in prev), min(b[3] for b in prev)) if prev else None
        vpp = 1.0 if I["currency"] == "EUR" else 1.0 / rate
        setups = ict.find_setups(bars, prev_hl, cfg["windows"], cfg, hodlod=cfg["hodlod"])
        blocked = None if news is None else [n for n in news if n[0] in rules["news_block"][inst]]
        ref = ict.simulate(bars, setups, cfg["windows"], dict(cfg, max_notional=rules["leverage"] * 1000.0), 10.0, vpp)
        shadow = round(sum(t["pnl"] for t in ref), 2)
        if news is None or blocked:
            why = "calendar unavailable (news filter is mandatory)" if news is None else \
                "red-folder news: " + "; ".join("%s %s %s" % b for b in blocked)
            log("%s: %s -> sat out. %d setups; it would have made EUR %+.2f per EUR 1,000. Data: %s"
                % (I["name"], why, len(setups), shadow, src))
            V2.append_csv(V2.SESSIONS_CSV, ["date", "session", "status", "breakouts", "trades", "pnl_eur", "shadow_eur", "reason"],
                          [day, session, "sat out", len(setups), 0, 0.0, shadow, why])
            state["%s_today" % session.lower()] = {"date": day.isoformat(), "switch": "off", "why": why}
            continue
        trades = ict.simulate(bars, setups, cfg["windows"], cfg, risk_eur, vpp)
        pnl = round(sum(t["pnl"] for t in trades), 2)
        state["cash_eur"] = round(state["cash_eur"] + pnl, 2)
        why = "no red-folder news; %d setups. Data: %s" % (len(setups), src)
        log("%s: traded. %s. %d trades, P&L EUR %+.2f" % (I["name"], why, len(trades), pnl))
        for t in trades:
            t.update(session=session, symbol=inst, currency="points", pnl_ccy=t["pnl"] if vpp == 1 else round(t["pnl"] * rate, 2),
                     setup="%s swept %s, MSS %s, FVG %s" % (t["liquidity"], t["sweep_time"], t["mss_time"], t["fvg_time"]))
            write_trade(day, t)
        V2.append_csv(V2.SESSIONS_CSV, ["date", "session", "status", "breakouts", "trades", "pnl_eur", "shadow_eur", "reason"],
                      [day, session, "traded", len(setups), len(trades), pnl, shadow, why])
        state["%s_today" % session.lower()] = {"date": day.isoformat(), "switch": "on", "why": why}
        n_all += len(trades)
    if etf:
        state["last_dax_close"] = etf[-1][4]
    if qqq_reg:
        state["last_qqq_close"] = qqq_reg[-1][4]
    state.setdefault("v3_days", []).append(day.isoformat())
    for k in ("days", "eu_days", "us_days"):
        if day.isoformat() not in state[k]:
            state[k].append(day.isoformat())
    row = V2.upsert_equity(day, state, rate, n_all)
    V2.write_json(V2.STATE, state)
    log("End of day: account EUR %s | deposited EUR %s | DAX EUR %s | Nasdaq-100 EUR %s"
        % (row["equity_eur"], row["deposited_eur"], row["dax_eur"], row["nasdaq_eur"]))
    export(rules, state, day, news)
    return 0


def export(rules, state, day, news):
    os.makedirs(V2.DASH, exist_ok=True)
    num = lambda v: None if v in ("", None) else float(v)
    for name, path, text in (("equity", V2.EQUITY, ("date",)),
                             ("trades", V2.TRADES, ("date", "session", "symbol", "currency", "entry_time", "exit_time",
                                                    "outcome", "side", "setup")),
                             ("sessions", V2.SESSIONS_CSV, ("date", "session", "status", "reason"))):
        rows = [{k: (v if k in text else num(v)) for k, v in r.items()} for r in csv.DictReader(open(path))] \
            if os.path.exists(path) else []
        V2.write_json(os.path.join(V2.DASH, name + ".json"), rows)
    eu_t, us_t = state.get("eu_today", {}), state.get("us_today", {})
    V2.write_json(os.path.join(V2.DASH, "status.json"), [{
        "last_run_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "last_session": day.isoformat(),
        "strategy": "ICT smart money (v3)", "strategy_since": state.get("strategy_since"),
        "monthly_eur": rules["monthly_eur"], "risk_per_trade": rules["risk_per_trade"], "rr": rules["rr"],
        "max_trades_per_session": rules["max_trades_per_instrument"], "leverage": rules["leverage"],
        "fx_fee": rules["fx_fee"], "fees_eur": round(state.get("fees_eur", 0.0), 2),
        "cash_eur": state["cash_eur"], "cash_usd": state.get("cash_usd", 0.0),
        "news_today": "unavailable" if news is None else ("; ".join("%s %s %s" % n for n in news) or "none"),
        "eu_switch_date": eu_t.get("date"), "eu_switch": eu_t.get("switch"), "eu_why": eu_t.get("why"),
        "us_switch_date": us_t.get("date"), "us_switch": us_t.get("switch"), "us_why": us_t.get("why"),
        "eur_deposited": round(sum(d["eur"] for d in state["deposits"]), 2),
    }])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, Romanian time)")
    a = ap.parse_args()
    os.makedirs(os.path.join(LIVE, "logs"), exist_ok=True)
    open(V2.LOG, "w").close()
    now = dt.datetime.now(C.RO)
    day = dt.date.fromisoformat(a.date) if a.date else now.date()
    if day == now.date() and now.time() < dt.time(22, 58):
        print("The day is not over yet (%s Romanian time). Run after 22:58." % now.strftime("%H:%M"))
        return 1
    return run(day)


if __name__ == "__main__":
    sys.exit(main())
