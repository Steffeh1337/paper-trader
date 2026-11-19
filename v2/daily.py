#!/usr/bin/env python3
"""v2 daily paper-trading run: DAX in the afternoon run, Nasdaq-100 in the night run, one account in EUR and USD.

Rules (v2/rules.json; trading windows from common guidance on intraday activity, switches tested on the last 60 days):
  money     EUR 1,000 deposited each month into the EUR balance
  EU        DAX 40 stocks + DAX ETF (EXS1) from the EUR balance. Switch: trades only if the DAX index rose during
            its opening range (10:00-10:15 Romanian time), known before any entry.
            Romanian time: range 10:00-10:15, entries until 12:00, flat 15:25 (before US data at 15:30)
  US        Nasdaq-100 stocks + QQQ from the USD balance. Switch: trades only if QQQ's previous close was above
            its 20-day average, known before the open.
            New York time: range 9:30-10:00, entries until 11:30, flat 15:55
            (Romanian time 16:30-17:00, 18:30, 22:55; an hour earlier when daylight-saving dates differ)
  currency  while the US switch is on, half the account is held in USD; otherwise everything is in EUR.
            Converting only when that changes pays Trading 212's 0.15% fee once, not on every trade.
  exits     stop-loss 1% below the entry, take-profit 2% above, fractional shares, up to 3 trades per
            session, each up to a third of its balance; same-bar breakouts: the most traded first
  sat out   a session that sits out is still simulated on a EUR 1,000 reference, shown on the dashboard

  python3 v2/daily.py --session EU    # after the DAX session (from 15:30 Romanian time)
  python3 v2/daily.py --session US    # after the US close (from 23:20 Romanian time)
Benchmarks: the same deposits held in the DAX ETF (EXS1) and in QQQ.
Data: Yahoo (DAX, slowly), Alpaca IEX (Nasdaq-100), ECB reference rate (EUR/USD).


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


def fetch_eu(inst, rules, only_etf=False):
    """DAX bars from Yahoo: the ETF first and patiently, then the stocks in slower passes within a time cap,
    so Yahoo's throttling can only shrink the day's list of stocks, never stall the run."""
    import time as _time
    etf_res = C.yahoo(inst["dax_etf"], "5d", pause=5.0, tries=5)
    eu = {inst["dax_etf"]: C.yahoo_bars(etf_res, C.RO)}
    skipped = [] if only_etf else list(inst["dax"])
    deadline = _time.time() + rules.get("eu_fetch_minutes", 20) * 60
    for attempt, pause in enumerate((4.0, 8.0, 12.0)):
        if attempt:
            if not skipped or _time.time() + 90 > deadline:
                break
            _time.sleep(90)                                   # let Yahoo's rate limit cool down
        still = []
        for sym in skipped:
            res = C.yahoo(sym, "5d", pause=pause, tries=1) if _time.time() < deadline else None
            if res is None:
                still.append(sym)
            else:
                eu[sym] = C.yahoo_bars(res, C.RO)
        skipped = still
    for sym in skipped:
        eu[sym] = {}
    return eu, etf_res is not None, skipped


def load_state():
    state = read_json(STATE, {})
    for k, v in (("days", []), ("eu_days", []), ("us_days", []), ("deposits", []), ("us_shadow", []), ("cash_usd", 0.0),
                 ("dax_units", 0.0), ("qqq_units", 0.0), ("pending_qqq_eur", 0.0), ("fees_eur", 0.0)):
        state.setdefault(k, v)
    state.setdefault("cash_eur", state.pop("cash", 0.0))
    # days run before the switch to two runs a day had both sessions
    for d in state["days"]:
        if d not in state["eu_days"]:
            state["eu_days"].append(d)
        if d not in state["us_days"]:
            state["us_days"].append(d)
    return state


def deposit(state, rules, day, rate, dax_price):
    month = day.strftime("%Y-%m")
    have = sum(d["eur"] for d in state["deposits"] if d["month"] == month)
    if have >= rules["monthly_eur"] - 0.005:
        return
    eur = round(rules["monthly_eur"] - have, 2)
    state["cash_eur"] = round(state["cash_eur"] + eur, 2)
    if dax_price:
        state["dax_units"] += eur / dax_price
    state["pending_qqq_eur"] = round(state.get("pending_qqq_eur", 0.0) + eur, 2)   # QQQ bought at the next US open
    state["deposits"].append({"month": month, "date": day.isoformat(), "eur": eur, "eurusd": rate, "dax_price": dax_price})
    log("Deposit: EUR %.2f. Benchmark bought the DAX ETF at EUR %s; QQQ is bought at the next US open." % (eur, dax_price))


def upsert_equity(day, state, rate, n_trades):
    rows = list(csv.DictReader(open(EQUITY))) if os.path.exists(EQUITY) else []
    rows = [r for r in rows if r["date"] != day.isoformat()]
    prev_trades = 0
    for r in list(csv.DictReader(open(EQUITY))) if os.path.exists(EQUITY) else []:
        if r["date"] == day.isoformat():
            prev_trades = int(float(r.get("trades") or 0))
    dax_val = state["dax_units"] * state["last_dax_close"] if state.get("last_dax_close") else None
    qqq_val = state["qqq_units"] * state["last_qqq_close"] / rate if state.get("last_qqq_close") and state["qqq_units"] else None
    deposited = sum(d["eur"] for d in state["deposits"])
    pending = state.get("pending_qqq_eur", 0.0)          # deposits not yet in QQQ count at face value
    rows.append({"date": day.isoformat(), "equity_eur": round(total_eur(state, rate), 2), "deposited_eur": round(deposited, 2),
                 "dax_eur": round(dax_val, 2) if dax_val else "",
                 "nasdaq_eur": round((qqq_val or 0) + pending, 2) if (qqq_val or pending) else "",
                 "trades": prev_trades + n_trades, "cash_eur": state["cash_eur"], "cash_usd": state["cash_usd"]})
    rows.sort(key=lambda r: r["date"])
    with open(EQUITY, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "equity_eur", "deposited_eur", "dax_eur", "nasdaq_eur", "trades", "cash_eur", "cash_usd"])
        w.writeheader()
        w.writerows(rows)
    return rows[-1]


def record(day, trades, sess_row):
    for t in trades:
        append_csv(TRADES, ["date", "session", "symbol", "currency", "qty", "entry_time", "entry", "stop", "target",
                            "exit_time", "exit", "outcome", "pnl", "pnl_eur", "R"],
                   [day, t["session"], t["symbol"], t["currency"], t["qty"], t["entry_ro"], t["entry"], t["stop"], t["target"],
                    t["exit_ro"], t["exit"], t["outcome"], t["pnl_usd"], t["pnl_eur"], t["R"]])
        log("  %s %s %s %g @ %s -> %s %s (%s)  EUR %+.2f" % (t["session"], t["entry_ro"], t["symbol"], t["qty"], t["entry"],
                                                           t["exit_ro"], t["exit"], t["outcome"], t["pnl_eur"]))
    append_csv(SESSIONS_CSV, ["date", "session", "status", "breakouts", "trades", "pnl_eur", "shadow_eur", "reason"], [day] + sess_row)


def eu_switch(day, etf_day_bars=None):
    """Europe trades only if the DAX rose during its opening range (10:00-10:15), known at 10:15.
    The index (^GDAXI) has a value every 5 minutes; if Yahoo refuses it, the DAX ETF's own bars are used."""
    bars = C.yahoo_bars(C.yahoo("^GDAXI", "5d", pause=5.0, tries=5), C.RO).get(day.isoformat(), [])
    rng, source = [b for b in bars if "10:00" <= b[0] < "10:15"], "DAX"
    if len(rng) < 2 and etf_day_bars:
        rng, source = [b for b in etf_day_bars if "10:00" <= b[0] < "10:15"], "DAX ETF"
    if len(rng) < 2:
        return None, "no DAX data for the opening range"
    up = rng[-1][4] > rng[0][1]
    return up, "%s %s in its opening range (%.2f -> %.2f)" % (source, "rose" if up else "fell", rng[0][1], rng[-1][4])


def us_switch(day):
    """The US trades only if QQQ's previous close was above its 20-day average, known before the open."""
    rows = C.alpaca_daily("QQQ", (day - dt.timedelta(days=45)).isoformat(), day.isoformat())
    closes = [c for d, c in rows if d < day.isoformat()]
    if len(closes) < 20:
        return False, "not enough QQQ history", None, None
    sma = sum(closes[-20:]) / 20
    up = closes[-1] > sma
    return up, "QQQ %s its 20-day average ($%.2f vs $%.2f)" % ("above" if up else "below", closes[-1], sma), closes[-1], sma


def run_eu(day):
    rules, inst, state = read_json(RULES), read_json(C.INSTRUMENTS), load_state()
    if day.isoformat() in state["eu_days"]:
        log("Europe %s was already simulated. Nothing to do." % day)
        return 0
    on, why = eu_switch(day)                           # first, while Yahoo still answers freely
    eu, etf_ok, skipped = fetch_eu(inst, rules)
    bars_etf = eu[inst["dax_etf"]].get(day.isoformat())
    if not bars_etf:
        log("%s: %s. Europe has nothing to do." % (day, "DAX closed (weekend or holiday)" if etf_ok else "no DAX data (Yahoo unavailable)"))
        return 0
    rate = ecb_eurusd(day)
    if skipped:
        log("Yahoo data missing for %d of %d DAX stocks today (throttled): %s" % (len(skipped), len(inst["dax"]), " ".join(skipped)))
    deposit(state, rules, day, rate, bars_etf[0][1])
    if on is None:
        on, why = eu_switch(day, bars_etf)
        on = bool(on)
    c_eu = candidates({s: eu[s] for s in inst["dax"] + [inst["dax_etf"]]}, day, "EU", 0.0)
    end_ref, _, _ = engine.trade_day([dict(c) for c in c_eu], 1000.0, session_rules(rules, "EU"))
    shadow = round(end_ref - 1000.0, 2)
    trades = []
    if on:
        start = state["cash_eur"]
        state["cash_eur"], trades, _ = engine.trade_day(c_eu, start, session_rules(rules, "EU"))
        for t in trades:
            t.update(session="EU", currency="EUR", pnl_eur=t["pnl_usd"], entry_ro=t["entry_time"], exit_ro=t["exit_time"])
        pnl = round(state["cash_eur"] - start, 2)
        log("Europe (DAX): %s -> traded. %d breakouts, %d trades, P&L EUR %+.2f" % (why, len(c_eu), len(trades), pnl))
        record(day, trades, ["EU", "traded", len(c_eu), len(trades), pnl, shadow, why])
    else:
        log("Europe (DAX): %s -> sat out. It would have made EUR %+.2f per EUR 1,000." % (why, shadow))
        record(day, [], ["EU", "sat out", len(c_eu), 0, 0.0, shadow, why])
    state["last_dax_close"] = [b for b in bars_etf if b[0] <= "15:25"][-1][4]
    state["eu_days"].append(day.isoformat())
    state["eu_today"] = {"date": day.isoformat(), "switch": "on" if on else "off", "why": why, "missing": len(skipped)}
    row = upsert_equity(day, state, rate, len(trades))
    write_json(STATE, state)
    log("After Europe: account EUR %s | deposited EUR %s | DAX EUR %s" % (row["equity_eur"], row["deposited_eur"], row["dax_eur"]))
    export(rules, state, day)
    return 0


def run_us(day):
    rules, inst, state = read_json(RULES), read_json(C.INSTRUMENTS), load_state()
    if day.isoformat() in state["us_days"]:
        log("US %s was already simulated. Nothing to do." % day)
        return 0
    us_syms = inst["ndx"] + [inst["ndx_etf"]]
    us = C.alpaca_bars(us_syms, day.isoformat() + "T00:00:00Z", (day + dt.timedelta(days=1)).isoformat() + "T00:00:00Z")
    qqq = us.get(inst["ndx_etf"], {}).get(day.isoformat(), [])
    if len(qqq) < 70:
        log("%s: Nasdaq closed (weekend or holiday). US has nothing to do." % day)
        return 0
    rate = ecb_eurusd(day)
    if day.isoformat() not in state["eu_days"]:          # a European holiday: the month's deposit lands here
        deposit(state, rules, day, rate, None)
    if state.get("pending_qqq_eur"):
        state["qqq_units"] += state["pending_qqq_eur"] * rate / qqq[0][1]
        log("Benchmark bought QQQ at $%.2f with EUR %.2f." % (qqq[0][1], state["pending_qqq_eur"]))
        state["pending_qqq_eur"] = 0.0
    on, why, qqq_prev, sma = us_switch(day)
    # currencies: while the US switch is on keep usd_share of the account in USD, otherwise all in EUR
    if on:
        gap = rules["usd_share"] * total_eur(state, rate) - state["cash_usd"] / rate
        if gap > rules["rebalance_min_eur"]:
            convert(state, rules, rate, eur_to_usd=min(gap, state["cash_eur"]))
    elif state["cash_usd"] > 0:
        convert(state, rules, rate, usd_to_eur=state["cash_usd"])
    c_us = candidates({s: us[s] for s in us_syms}, day, "US", 0.0)
    ref = 1000.0 * rate
    end_ref, _, _ = engine.trade_day([dict(c) for c in c_us], ref, session_rules(rules, "US"))
    shadow = round((end_ref - ref) / rate, 2)
    state["us_shadow"].append([day.isoformat(), shadow])
    trades = []
    if on and state["cash_usd"] > 0:
        start = state["cash_usd"]
        state["cash_usd"], trades, _ = engine.trade_day(c_us, start, session_rules(rules, "US"))
        pnl = round((state["cash_usd"] - start) / rate, 2)
        for t in trades:
            t.update(session="US", currency="USD", pnl_eur=round(t["pnl_usd"] / rate, 2),
                     entry_ro=ny_to_ro(day, t["entry_time"]), exit_ro=ny_to_ro(day, t["exit_time"]))
        log("US (Nasdaq-100): %s -> traded. %d breakouts, %d trades, P&L EUR %+.2f" % (why, len(c_us), len(trades), pnl))
        record(day, trades, ["US", "traded", len(c_us), len(trades), pnl, shadow, why])
    else:
        log("US (Nasdaq-100): %s -> sat out. It would have made EUR %+.2f per EUR 1,000." % (why, shadow))
        record(day, [], ["US", "sat out", len(c_us), 0, 0.0, shadow, why])
    state["last_qqq_close"] = qqq[-1][4]
    if day.isoformat() in state["eu_days"]:                  # the DAX's real close, now that it is in
        eu, ok, _ = fetch_eu(inst, rules, only_etf=True)
        bars_etf = eu[inst["dax_etf"]].get(day.isoformat())
        if bars_etf:
            state["last_dax_close"] = bars_etf[-1][4]
    state["us_days"].append(day.isoformat())
    if day.isoformat() not in state["days"]:
        state["days"].append(day.isoformat())
    state["us_today"] = {"date": day.isoformat(), "switch": "on" if on else "off", "why": why}
    state["qqq_prev_close"], state["qqq_sma20"] = qqq_prev, sma
    row = upsert_equity(day, state, rate, len(trades))
    write_json(STATE, state)
    log("End of day: account EUR %s (EUR %.2f + USD %.2f) | deposited EUR %s | DAX EUR %s | Nasdaq-100 EUR %s"
        % (row["equity_eur"], state["cash_eur"], state["cash_usd"], row["deposited_eur"], row["dax_eur"], row["nasdaq_eur"]))
    export(rules, state, day)
    return 0


def export(rules, state, day):
    os.makedirs(DASH, exist_ok=True)
    num = lambda v: None if v in ("", None) else float(v)
    for name, path, text in (("equity", EQUITY, ("date",)),
                             ("trades", TRADES, ("date", "session", "symbol", "currency", "entry_time", "exit_time", "outcome")),
                             ("sessions", SESSIONS_CSV, ("date", "session", "status", "reason"))):
        rows = []
        if os.path.exists(path):
            for r in csv.DictReader(open(path)):
                rows.append({k: (v if k in text else num(v)) for k, v in r.items()})
        write_json(os.path.join(DASH, name + ".json"), rows)
    eu_t, us_t = state.get("eu_today", {}), state.get("us_today", {})
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
        "eu_switch_date": eu_t.get("date"), "eu_switch": eu_t.get("switch"), "eu_why": eu_t.get("why"),
        "eu_missing_stocks": eu_t.get("missing"),
        "us_switch_date": us_t.get("date"), "us_switch": us_t.get("switch"), "us_why": us_t.get("why"),
        "eur_deposited": round(sum(d["eur"] for d in state["deposits"]), 2),
    }])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--session", choices=["EU", "US", "both"], default="both",
                    help="EU after the DAX session (from 15:30 Romanian time), US after the US close, both at night")
    a = ap.parse_args()
    os.makedirs(os.path.join(LIVE, "logs"), exist_ok=True)
    open(LOG, "w").close()
    now_ro, now_ny = dt.datetime.now(C.RO), dt.datetime.now(C.NY)
    if a.session in ("EU", "both"):
        day = dt.date.fromisoformat(a.date) if a.date else now_ro.date()
        if day == now_ro.date() and now_ro.time() < dt.time(15, 30):
            print("Europe's session for %s is not over yet (%s Romanian time). Run after 15:30." % (day, now_ro.strftime("%H:%M")))
            return 1
        rc = run_eu(day)
        if rc:
            return rc
    if a.session in ("US", "both"):
        day = dt.date.fromisoformat(a.date) if a.date else now_ny.date()
        if day == now_ny.date() and now_ny.time() < dt.time(16, 20):
            print("The US session for %s is not over yet (%s New York). Run after 16:20 New York (23:20 Romanian time)."
                  % (day, now_ny.strftime("%H:%M")))
            return 1
        return run_us(day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
