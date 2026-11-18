#!/usr/bin/env python3
"""v2 daily paper-trading run: DAX in the morning, Nasdaq-100 in the evening, one account in EUR and USD.

Rules (v2/rules.json; times from expert guidance on intraday activity, tested on the last 60 days):
  money     EUR 1,000 deposited each month into the EUR balance
  EU        DAX 40 stocks + DAX ETF (EXS1) from the EUR balance, every trading day.
            Romanian time: range 10:00-10:15, entries until 12:00, flat 15:25 (before US data at 15:30)
  US        Nasdaq-100 stocks + QQQ from the USD balance, only while the US session's own results over
            its previous 20 sessions are positive (it is simulated every day on a EUR 1,000 reference).
            New York time: range 9:30-10:00, entries until 11:30, flat 15:55
            (Romanian time 16:30-17:00, 18:30, 22:55; an hour earlier when daylight-saving dates differ)
  currency  while the US trades, half the account is held in USD; otherwise everything is in EUR.
            Converting only when that changes pays Trading 212's 0.15% fee once, not on every trade.
  exits     stop-loss 1% below the entry, take-profit 2% above, fractional shares, up to 3 trades per
            session, each up to a third of its balance; same-bar breakouts: the most traded first
Benchmarks: the same deposits held in the DAX ETF (EXS1) and in QQQ.
Data: Yahoo (DAX, slowly), Alpaca IEX (Nasdaq-100), ECB reference rate (EUR/USD).

  python3 v2/daily.py                 # today, after the US close
  python3 v2/daily.py --date 2026-11-18

Paper only. Never places orders. Not financial advice.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
import common as C   # noqa: E402
import engine        # noqa: E402

LIVE = os.path.join(HERE, "live")
STATE = os.path.join(LIVE, "state.json")
TRADES = os.path.join(LIVE, "trades.csv")
EQUITY = os.path.join(LIVE, "equity.csv")
SESSIONS_CSV = os.path.join(LIVE, "sessions.csv")
LOG = os.path.join(LIVE, "logs", "latest.log")
DASH = os.path.join(LIVE, "dashboard")
RULES = os.path.join(HERE, "rules.json")


def log(msg):
    print(msg, flush=True)
    with open(LOG, "a") as f:
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


def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def ecb_eurusd(day):
    url = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": C.UA}), timeout=30) as r:
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


def ny_to_ro(day, hhmm):
    t = dt.datetime.combine(day, dt.time(*map(int, hhmm.split(":"))), C.NY)
    return t.astimezone(C.RO).strftime("%H:%M")


def session_rules(rules, session):
    S = C.SESSIONS[session]
    return dict(engine.DEFAULT_RULES, min_exp_R=None, stop="pct", stop_pct=rules["stop_pct"],
                target_pct=rules["target_pct"], max_trades=rules["max_trades_per_session"], risk=rules["risk"],
                daily_loss=rules["daily_loss"], fractional=True, last_entry=S["last_entry"], flat_by=S["flat_by"])


def candidates(bars_by_symbol, day, session, cost):
    out = []
    for s, by_day in bars_by_symbol.items():
        rows = by_day.get(day.isoformat())
        if not rows:
            continue
        c = C.candidate(s, rows, session, "stock")
        if c:
            c["date"], c["cost"] = day.isoformat(), cost
            out.append(c)
    out.sort(key=lambda c: -c["turnover"])
    return out


def total_eur(state, rate):
    return state["cash_eur"] + state["cash_usd"] / rate


def convert(state, rules, rate, eur_to_usd=None, usd_to_eur=None):
    """One currency conversion inside the account, paying the broker's fee once."""
    fee = rules["fx_fee"]
    if eur_to_usd:
        state["cash_eur"] -= eur_to_usd
        state["cash_usd"] += eur_to_usd * rate * (1 - fee)
        state["fees_eur"] = state.get("fees_eur", 0.0) + eur_to_usd * fee
        log("Converted EUR %.2f to USD %.2f (fee %.2f%%)." % (eur_to_usd, eur_to_usd * rate * (1 - fee), 100 * fee))
    if usd_to_eur:
        state["cash_usd"] -= usd_to_eur
        state["cash_eur"] += usd_to_eur / rate * (1 - fee)
        state["fees_eur"] = state.get("fees_eur", 0.0) + usd_to_eur / rate * fee
        log("Converted USD %.2f to EUR %.2f (fee %.2f%%)." % (usd_to_eur, usd_to_eur / rate * (1 - fee), 100 * fee))
    state["cash_eur"], state["cash_usd"] = round(state["cash_eur"], 2), round(state["cash_usd"], 2)


def run(day):
    os.makedirs(os.path.join(LIVE, "logs"), exist_ok=True)
    open(LOG, "w").close()
    rules = read_json(RULES)
    inst = read_json(C.INSTRUMENTS)
    state = read_json(STATE, {})
    state.setdefault("days", []); state.setdefault("deposits", []); state.setdefault("us_shadow", [])
    state.setdefault("cash_eur", state.pop("cash", 0.0)); state.setdefault("cash_usd", 0.0)
    state.setdefault("dax_units", 0.0); state.setdefault("qqq_units", 0.0)
    if day.isoformat() in state["days"]:
        log("%s was already simulated. Nothing to do." % day)
        return 0

    # ---- data
    eu_syms = inst["dax"] + [inst["dax_etf"]]
    # The DAX ETF first, patiently (it decides whether the market was open and drives the benchmark);
    # then the stocks at a steady pace with one retry, within a time cap, so Yahoo's throttling
    # can only make the day's universe smaller, never stall the run.
    import time as _time
    etf_res = C.yahoo(inst["dax_etf"], "5d", pause=5.0, tries=5)
    eu = {inst["dax_etf"]: C.yahoo_bars(etf_res, C.RO)}
    deadline, skipped = _time.time() + rules.get("eu_fetch_minutes", 20) * 60, list(inst["dax"])
    for attempt, pause in enumerate((4.0, 8.0, 12.0)):     # passes over the stocks still missing
        if attempt:
            if not skipped or _time.time() + 90 > deadline:
                break
            _time.sleep(90)                                   # let Yahoo's rate limit cool down
        still = []
        for s in skipped:
            res = C.yahoo(s, "5d", pause=pause, tries=1) if _time.time() < deadline else None
            if res is None:
                still.append(s)
            else:
                eu[s] = C.yahoo_bars(res, C.RO)
        skipped = still
    for s in skipped:
        eu[s] = {}
    eu_open = bool(eu.get(inst["dax_etf"], {}).get(day.isoformat()))
    us_syms = inst["ndx"] + [inst["ndx_etf"]]
    us = C.alpaca_bars(us_syms, day.isoformat() + "T00:00:00Z", (day + dt.timedelta(days=1)).isoformat() + "T00:00:00Z")
    us_open = len(us.get(inst["ndx_etf"], {}).get(day.isoformat(), [])) >= 70
    if not eu_open and not us_open:
        log("%s: both markets were closed. Nothing to do." % day)
        return 0
    rate = ecb_eurusd(day)
    log("%s  EUR/USD %.4f (ECB)  DAX %s, Nasdaq %s" % (day, rate, "open" if eu_open else ("no data (Yahoo unavailable)" if etf_res is None else "closed"),
                                                        "open" if us_open else "closed"))
    if skipped:
        log("Yahoo data missing for %d of %d DAX stocks today (throttled): %s" % (len(skipped), len(inst["dax"]), " ".join(skipped)))

    # ---- deposits (to the EUR balance) and benchmarks (bought at the deposit day's open)
    month = day.strftime("%Y-%m")
    have = sum(d["eur"] for d in state["deposits"] if d["month"] == month)
    if have < rules["monthly_eur"] - 0.005 and eu_open and us_open:
        eur = round(rules["monthly_eur"] - have, 2)
        dax_open = eu[inst["dax_etf"]][day.isoformat()][0][1]
        qqq_open = us[inst["ndx_etf"]][day.isoformat()][0][1]
        state["cash_eur"] = round(state["cash_eur"] + eur, 2)
        state["dax_units"] += eur / dax_open
        state["qqq_units"] += eur * rate / qqq_open
        state["deposits"].append({"month": month, "date": day.isoformat(), "eur": eur, "eurusd": rate,
                                  "dax_price": dax_open, "qqq_price": qqq_open})
        log("Deposit: EUR %.2f. Benchmarks bought the DAX ETF at EUR %.2f and QQQ at $%.2f." % (eur, dax_open, qqq_open))

    # ---- the US switch, from the US session's own last N sessions (decided before the day starts)
    look = [p for _, p in state["us_shadow"][-rules["us_lookback"]:]]
    gate_on = len(look) >= rules["us_lookback"] and sum(look) > 0

    # ---- currencies: while the US trades, keep usd_share of the account in USD; otherwise all in EUR.
    #      Converting only here (not per trade) pays the fee once, not on every trade.
    if gate_on:
        want_usd_eur = rules["usd_share"] * total_eur(state, rate)
        gap = want_usd_eur - state["cash_usd"] / rate
        if gap > rules["rebalance_min_eur"]:
            convert(state, rules, rate, eur_to_usd=min(gap, state["cash_eur"]))
    elif state["cash_usd"] > 0:
        convert(state, rules, rate, usd_to_eur=state["cash_usd"])

    trades_today, sess_rows = [], []

    # ---- Europe: DAX, every trading day, from the EUR balance
    if eu_open:
        c_eu = candidates({s: eu[s] for s in eu_syms}, day, "EU", 0.0)
        start = state["cash_eur"]
        state["cash_eur"], t_eu, _ = engine.trade_day(c_eu, start, session_rules(rules, "EU"))
        for t in t_eu:
            t.update(session="EU", currency="EUR", pnl_eur=t["pnl_usd"], entry_ro=t["entry_time"], exit_ro=t["exit_time"])
        trades_today += t_eu
        sess_rows.append(["EU", "traded", len(c_eu), len(t_eu), round(state["cash_eur"] - start, 2), ""])
        state["eu_missing_today"] = len(skipped)
        log("Europe (DAX): %d breakouts, %d trades, P&L EUR %+.2f" % (len(c_eu), len(t_eu), state["cash_eur"] - start))

    # ---- US: Nasdaq-100 from the USD balance, only while switched on; always simulated on a reference
    if us_open:
        c_us = candidates({s: us[s] for s in us_syms}, day, "US", 0.0)
        ref = 1000.0 * rate
        end_ref, _, _ = engine.trade_day([dict(c) for c in c_us], ref, session_rules(rules, "US"))
        shadow = round((end_ref - ref) / rate, 2)
        if gate_on and state["cash_usd"] > 0:
            start_usd = state["cash_usd"]
            state["cash_usd"], t_us, _ = engine.trade_day(c_us, start_usd, session_rules(rules, "US"))
            pnl_eur = round((state["cash_usd"] - start_usd) / rate, 2)
            for t in t_us:
                t.update(session="US", currency="USD", pnl_eur=round(t["pnl_usd"] / rate, 2),
                         entry_ro=ny_to_ro(day, t["entry_time"]), exit_ro=ny_to_ro(day, t["exit_time"]))
            trades_today += t_us
            sess_rows.append(["US", "traded", len(c_us), len(t_us), pnl_eur, shadow])
            log("US (Nasdaq-100): %d breakouts, %d trades, P&L EUR %+.2f" % (len(c_us), len(t_us), pnl_eur))
        else:
            sess_rows.append(["US", "sat out", len(c_us), 0, 0.0, shadow])
            log("US (Nasdaq-100): sat out (last %d sessions EUR %+.2f per EUR 1,000); today it would have made EUR %+.2f"
                % (len(look), sum(look), shadow))
        state["us_shadow"].append([day.isoformat(), shadow])

    for t in trades_today:
        append_csv(TRADES, ["date", "session", "symbol", "currency", "qty", "entry_time", "entry", "stop", "target",
                            "exit_time", "exit", "outcome", "pnl", "pnl_eur", "R"],
                   [day, t["session"], t["symbol"], t["currency"], t["qty"], t["entry_ro"], t["entry"], t["stop"], t["target"],
                    t["exit_ro"], t["exit"], t["outcome"], t["pnl_usd"], t["pnl_eur"], t["R"]])
        log("  %s %s %s %g @ %s -> %s %s (%s)  EUR %+.2f" % (t["session"], t["entry_ro"], t["symbol"], t["qty"], t["entry"],
                                                           t["exit_ro"], t["exit"], t["outcome"], t["pnl_eur"]))
    for r in sess_rows:
        append_csv(SESSIONS_CSV, ["date", "session", "status", "breakouts", "trades", "pnl_eur", "us_shadow_eur"], [day] + r)

    # ---- end of day values
    dax_close = eu[inst["dax_etf"]][day.isoformat()][-1][4] if eu_open else state.get("last_dax_close")
    qqq_close = us[inst["ndx_etf"]][day.isoformat()][-1][4] if us_open else state.get("last_qqq_close")
    state["last_dax_close"], state["last_qqq_close"] = dax_close, qqq_close
    deposited = sum(d["eur"] for d in state["deposits"])
    equity = round(total_eur(state, rate), 2)
    dax_val = state["dax_units"] * dax_close if dax_close else None
    qqq_val = state["qqq_units"] * qqq_close / rate if qqq_close else None
    append_csv(EQUITY, ["date", "equity_eur", "deposited_eur", "dax_eur", "nasdaq_eur", "trades", "cash_eur", "cash_usd"],
               [day, equity, round(deposited, 2), round(dax_val, 2) if dax_val else "", round(qqq_val, 2) if qqq_val else "",
                len(trades_today), state["cash_eur"], state["cash_usd"]])
    state["days"].append(day.isoformat())
    write_json(STATE, state)
    log("End of day: account EUR %.2f (EUR %.2f + USD %.2f) | deposited EUR %.2f | DAX EUR %s | Nasdaq-100 EUR %s"
        % (equity, state["cash_eur"], state["cash_usd"], deposited, "%.2f" % dax_val if dax_val else "-",
           "%.2f" % qqq_val if qqq_val else "-"))
    export(rules, state, day)
    return 0


def export(rules, state, day):
    os.makedirs(DASH, exist_ok=True)
    num = lambda v: None if v in ("", None) else float(v)
    for name, path, text in (("equity", EQUITY, ("date",)),
                             ("trades", TRADES, ("date", "session", "symbol", "currency", "entry_time", "exit_time", "outcome")),
                             ("sessions", SESSIONS_CSV, ("date", "session", "status"))):
        rows = []
        if os.path.exists(path):
            for r in csv.DictReader(open(path)):
                rows.append({k: (v if k in text else num(v)) for k, v in r.items()})
        write_json(os.path.join(DASH, name + ".json"), rows)
    look = [p for _, p in state["us_shadow"][-rules["us_lookback"]:]]
    write_json(os.path.join(DASH, "status.json"), [{
        "last_run_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "last_session": day.isoformat(),
        "monthly_eur": rules["monthly_eur"], "stop_pct": rules["stop_pct"], "target_pct": rules["target_pct"],
        "max_trades_per_session": rules["max_trades_per_session"], "fx_fee": rules["fx_fee"],
        "usd_share": rules["usd_share"], "fees_eur": round(state.get("fees_eur", 0.0), 2),
        "cash_eur": state["cash_eur"], "cash_usd": state["cash_usd"],
        "us_range_ro": ny_to_ro(day, C.SESSIONS["US"]["open"]) + "-" + ny_to_ro(day, C.SESSIONS["US"]["range_end"]),
        "us_last_entry_ro": ny_to_ro(day, C.SESSIONS["US"]["last_entry"]), "us_flat_ro": ny_to_ro(day, C.SESSIONS["US"]["flat_by"]),
        "eu_range_ro": C.SESSIONS["EU"]["open"] + "-" + C.SESSIONS["EU"]["range_end"],
        "eu_last_entry_ro": C.SESSIONS["EU"]["last_entry"], "eu_flat_ro": C.SESSIONS["EU"]["flat_by"],
        "us_lookback": rules["us_lookback"], "us_recent_eur": round(sum(look), 2),
        "us_next": "on" if len(look) >= rules["us_lookback"] and sum(look) > 0 else "off",
        "eur_deposited": round(sum(d["eur"] for d in state["deposits"]), 2),
    }])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in New York)")
    a = ap.parse_args()
    now = dt.datetime.now(C.NY)
    day = dt.date.fromisoformat(a.date) if a.date else now.date()
    if day == now.date() and now.time() < dt.time(16, 20):
        print("The US session for %s is not over yet (%s in New York). Run after 16:20 New York (23:20 Romanian time)."
              % (day, now.strftime("%H:%M")))
        return 1
    return run(day)


if __name__ == "__main__":
    sys.exit(main())
