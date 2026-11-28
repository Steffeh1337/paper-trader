"""Data for the v3 strategy: 5-minute bars in Romanian time, and each instrument's settings.

Preferred: OANDA's practice API (CFD prices for DE30_EUR and NAS100_USD around the clock, bid and ask),
which covers the 09:59 HOD/LOD and the evening windows. Its key is injected by the session's network
proxy (or OANDA_TOKEN). Without it: DAX from Yahoo's cash index (10:00-18:30 Romanian time) and the
Nasdaq-100 through QQQ from Alpaca (pre-market included).
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "v2"))
import common as C   # noqa: E402

OANDA = "https://api-fxpractice.oanda.com/v3/instruments/%s/candles"


def cfg(rules, inst, data_kind):
    I = rules["instruments"][inst]
    p = I["qqq"] if data_kind == "alpaca" else I["points"]
    hodlod = I["hodlod"] and data_kind == "oanda"          # the 09:59 HOD/LOD needs prices from before 10:00
    return {"windows": [tuple(w) for w in I["windows"]], "buffer": p["buffer"], "min_risk": p["min_risk"],
            "spread": p["spread"], "rr": rules["rr"], "max_trades": rules["max_trades_per_instrument"],
            "flat_by": rules["flat_by"], "rule_1615": I["rule_1615"], "hodlod": hodlod,
            "max_notional": 1e12}


def oanda_ro(instrument, start_day, end_day):
    """{date: [[HH:MM Romanian, o, h, l, c], ...]}, spread} from OANDA M5 bid/ask candles, or None."""
    tok = os.environ.get("OANDA_TOKEN", "injected-by-proxy")
    out, spreads = {}, []
    t0 = dt.datetime.combine(start_day, dt.time(0), C.RO).astimezone(dt.timezone.utc)
    t1 = dt.datetime.combine(end_day, dt.time(23, 59), C.RO).astimezone(dt.timezone.utc)
    cur = t0
    while cur < t1:
        q = {"granularity": "M5", "price": "BA", "from": cur.strftime("%Y-%m-%dT%H:%M:%SZ"), "count": 2000}
        req = urllib.request.Request(OANDA % instrument + "?" + urllib.parse.urlencode(q),
                                     headers={"Authorization": "Bearer " + tok, "User-Agent": C.UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                doc = json.load(r)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        cs = doc.get("candles") or []
        if not cs:
            break
        last = None
        for c in cs:
            t = dt.datetime.strptime(c["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
            last = t
            if t > t1:
                break
            b, a = c["bid"], c["ask"]
            mid = [(float(b[k]) + float(a[k])) / 2 for k in ("o", "h", "l", "c")]
            spreads.append(float(a["c"]) - float(b["c"]))
            tr = t.astimezone(C.RO)
            out.setdefault(tr.date().isoformat(), []).append([tr.strftime("%H:%M")] + mid)
        if last is None or len(cs) < 2000:
            break
        cur = last + dt.timedelta(minutes=5)
        time.sleep(0.3)
    spreads.sort()
    return {"bars": out, "spread": spreads[len(spreads) // 2] if spreads else None}


def alpaca_ro(symbol, start, end):
    """{date: [[HH:MM Romanian, o, h, l, c], ...]} from Alpaca IEX 5-minute bars (pre-market included)."""
    raw = C.alpaca_bars([symbol], start + "T00:00:00Z", end + "T00:00:00Z", workers=1)[symbol]
    out = {}
    for d, rows in raw.items():
        day = dt.date.fromisoformat(d)
        for r in rows:
            t = dt.datetime.combine(day, dt.time(*map(int, r[0].split(":"))), C.NY).astimezone(C.RO)
            out.setdefault(t.date().isoformat(), []).append([t.strftime("%H:%M")] + r[1:5])
    for d in out:
        out[d].sort()
    return out


def yahoo_dax_ro(rng="5d"):
    res = C.yahoo("^GDAXI", rng, pause=3.0, tries=4)
    return {d: [r[:5] for r in rows] for d, rows in C.yahoo_bars(res, C.RO).items()}


def red_folder(day, path):
    """High-impact events on the day (Romanian date) from the ForexFactory weekly calendar feed, cached in path.
    Returns a list of (currency, HH:MM Romanian, title), or None if the calendar is unavailable."""
    data = None
    try:
        req = urllib.request.Request("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers={"User-Agent": C.UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
    except (urllib.error.URLError, OSError, ValueError):
        if os.path.exists(path):
            data = json.load(open(path))
    if data is None:
        return None
    days = [dt.datetime.fromisoformat(e["date"]).astimezone(C.RO).date() for e in data]
    if not days or not (min(days) - dt.timedelta(days=1) <= day <= max(days) + dt.timedelta(days=1)):
        return None                                        # the feed's week does not cover this day
    out = []
    for e in data:
        if e.get("impact") != "High":
            continue
        t = dt.datetime.fromisoformat(e["date"]).astimezone(C.RO)
        if t.date() == day:
            out.append((e["country"], t.strftime("%H:%M"), e["title"]))
    return out
