"""Shared pieces of the v2 strategy: data download, sessions and opening-range candidates.

Sessions (first 5-minute close above the opening-range high, stop 1% below the entry,
target 2% above, max 3 trades a session):
  EU  DAX stocks / DAX ETF  range 10:00-10:15 Romanian time, entries to 12:00, flat 15:25
  US  Nasdaq-100 / QQQ      range 9:30-10:00 New York (16:30-17:00 Romanian time),
                            entries to 11:30 New York, flat 15:55 New York
"""
import concurrent.futures as cf
import datetime as dt
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

RO = ZoneInfo("Europe/Bucharest")
NY = ZoneInfo("America/New_York")
UA = "paper-trader-sim/1.0 (personal research)"
HERE = os.path.dirname(os.path.abspath(__file__))
INSTRUMENTS = os.path.join(HERE, "instruments.json")

SESSIONS = {
    # Europe (DAX), Romanian time: 15-minute opening range after Xetra's 9:00 CET open, entries only in the
    # first two hours (the active morning, before the midday lull), flat at 15:25, before US data at 15:30
    # and the US open at 16:30.
    "EU": {"tz": RO, "open": "10:00", "range_end": "10:15", "last_entry": "12:00", "flat_by": "15:25"},
    # US (Nasdaq-100), New York time: 30-minute opening range (9:30-10:00), entries until 11:30 (the active
    # first two hours, before the lunch lull), flat at 15:55. In Romanian time: 16:30-17:00, 18:30, 22:55
    # (one hour earlier in the weeks when US and EU daylight-saving dates differ).
    "US": {"tz": NY, "open": "09:30", "range_end": "10:00", "last_entry": "11:30", "flat_by": "15:55"},
}


# ------------------------------------------------------------------ data

def yahoo(symbol, rng="5d", pause=3.0, tries=4):
    """Raw Yahoo 5-minute chart result, slowly and politely (it rate-limits)."""
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=5m" % (urllib.parse.quote(symbol), rng)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
                res = json.load(r)["chart"]["result"][0]
            time.sleep(pause)
            return res
        except urllib.error.HTTPError as e:
            if attempt + 1 < tries:
                time.sleep(30 * (attempt + 1) if e.code == 429 else 5)
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError):
            if attempt + 1 < tries:
                time.sleep(5)
    return None


def yahoo_bars(res, tz):
    """{date: [[HH:MM local, o, h, l, c, v], ...]} from a Yahoo result."""
    out = {}
    if not res:
        return out
    q = res["indicators"]["quote"][0]
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c) or ts % 300:
            continue
        t = dt.datetime.fromtimestamp(ts, tz)
        out.setdefault(t.date().isoformat(), []).append([t.strftime("%H:%M"), o, h, l, c, v or 0])
    return out


def alpaca_bars(symbols, start, end, workers=6):
    """{symbol: {date: [[HH:MM New York, o, h, l, c, v], ...]}} from Alpaca (IEX), 5-minute bars."""
    hdr = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"], "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]}

    def one(sym):
        out, token = {}, None
        for _ in range(500):
            p = {"symbols": sym, "timeframe": "5Min", "start": start, "end": end, "feed": "iex", "limit": 10000,
                 "adjustment": "split"}
            if token:
                p["page_token"] = token
            doc = None
            for attempt in range(5):
                try:
                    req = urllib.request.Request("https://data.alpaca.markets/v2/stocks/bars?" + urllib.parse.urlencode(p), headers=hdr)
                    with urllib.request.urlopen(req, timeout=60) as r:
                        doc = json.load(r)
                    break
                except urllib.error.HTTPError as e:
                    time.sleep(30 if e.code == 429 else 5)
                except (urllib.error.URLError, OSError):
                    time.sleep(5)
            if doc is None:
                break
            for b in (doc.get("bars") or {}).get(sym, []):
                t = dt.datetime.strptime(b["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).astimezone(NY)
                out.setdefault(t.date().isoformat(), []).append([t.strftime("%H:%M"), b["o"], b["h"], b["l"], b["c"], b["v"]])
            token = doc.get("next_page_token")
            time.sleep(0.35)
            if not token:
                break
        return sym, out

    res = {}
    with cf.ThreadPoolExecutor(workers) as ex:
        for sym, out in ex.map(one, symbols):
            res[sym] = out
    return res


# ------------------------------------------------------------------ candidates

def add_minutes(hhmm, m):
    h, mi = map(int, hhmm.split(":"))
    return "%02d:%02d" % divmod(h * 60 + mi + m, 60)


def candidate(symbol, day_bars, session, kind):
    """The day's opening-range breakout for one instrument, or None."""
    S = SESSIONS[session]
    bars = [b for b in day_bars if S["open"] <= b[0] < S["flat_by"] or b[0] == S["flat_by"]]
    rng = [b for b in bars if S["open"] <= b[0] < S["range_end"]]
    if len(rng) < 3:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    turnover = sum(b[4] * b[5] for b in rng)
    for i, b in enumerate(bars):
        if b[0] < S["range_end"]:
            continue
        close_t = add_minutes(b[0], 5)
        if close_t > S["last_entry"]:
            return None
        if b[4] > hi:
            after = [[x[0], x[1], x[2], x[3], x[4]] for x in bars[i + 1:]]
            if not after:
                return None
            return {"symbol": symbol, "kind": kind, "session": session, "time": close_t, "entry_ref": b[4],
                    "or_high": hi, "or_low": lo, "exp_R": 0.0, "hist_R": None, "turnover": turnover, "bars": after}
    return None
