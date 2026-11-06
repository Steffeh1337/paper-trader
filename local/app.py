#!/usr/bin/env python3
"""Trading desk: a local live dashboard for manual day trading (Windows, macOS, Linux).

Run:   py local\\app.py          (Windows)     python3 local/app.py   (macOS/Linux)
Open:  http://localhost:8050

Standard-library Python only, nothing to install. Market data comes from Alpaca's
free IEX feed with the keys in the environment variables APCA_API_KEY_ID and
APCA_API_SECRET_KEY (set once on your PC; never put them in a file).

What it serves:
  /                      the dashboard page (local/index.html)
  /api/watch?symbols=..  live price, change, opening range, breakout signal, stop, 2R target
  /api/bars?symbol=..    today's 5-minute bars for the chart
  /api/news?symbols=..   latest headlines (Alpaca news)
  /api/movers            biggest movers today among the 600 liquid stocks (sim/universe_alpaca.json)
  /api/earnings          today's earnings calendar (Nasdaq; may be unavailable)
  /api/papertrader       the paper trader's latest results (sim/dashboard/*.json; run "git pull" first)

Information only: it never places orders. Not financial advice.
"""
import datetime as dt
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = int(os.environ.get("TRADING_DESK_PORT", "8050"))
DATA = "https://data.alpaca.markets"
OPEN_MIN, LAST_ENTRY_MIN, CLOSE_MIN = 9 * 60 + 30, 15 * 60 + 30, 16 * 60
UA = "trading-desk/1.0 (personal use)"

# ------------------------------------------------------------------ New York time (no tz database needed)

def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def ny_offset(utc):
    """-4 in US daylight time (2nd Sunday of March 2:00 to 1st Sunday of November 2:00), else -5."""
    start = dt.datetime.combine(_nth_sunday(utc.year, 3, 2), dt.time(7))   # 2:00 EST = 7:00 UTC
    end = dt.datetime.combine(_nth_sunday(utc.year, 11, 1), dt.time(6))    # 2:00 EDT = 6:00 UTC
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def ny_now():
    u = dt.datetime.now(dt.timezone.utc)
    return u + dt.timedelta(hours=ny_offset(u))


def to_ny(iso):
    u = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return u + dt.timedelta(hours=ny_offset(u))


# ------------------------------------------------------------------ Alpaca

def keys():
    k, s = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if not k or not s:
        raise RuntimeError("Alpaca keys missing: set APCA_API_KEY_ID and APCA_API_SECRET_KEY (see local/README.md)")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def get_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA, "Accept": "application/json"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def alpaca(path, **params):
    return get_json(DATA + path + "?" + urllib.parse.urlencode(params), keys())


_cache = {}
_lock = threading.Lock()


def cached(key, seconds, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < seconds:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (now, value)
    return value


def snapshots(symbols):
    out = {}
    for i in range(0, len(symbols), 100):
        out.update(alpaca("/v2/stocks/snapshots", symbols=",".join(symbols[i:i + 100]), feed="iex") or {})
    return out


def session_day():
    """The trading day to show: today in New York (bars appear once the market opens)."""
    return ny_now().date()


def today_bars(symbols):
    """Today's regular-session 5-minute bars per symbol, as [ny 'HH:MM', o, h, l, c, v]."""
    day = session_day()
    off = ny_offset(dt.datetime.combine(day, dt.time(12)))
    start = dt.datetime.combine(day, dt.time(9, 30)) - dt.timedelta(hours=off)
    out = {s: [] for s in symbols}
    token = None
    while True:
        p = {"symbols": ",".join(symbols), "timeframe": "5Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "feed": "iex", "limit": 10000}
        if token:
            p["page_token"] = token
        doc = alpaca("/v2/stocks/bars", **p)
        for s, rows in (doc.get("bars") or {}).items():
            for b in rows:
                t = to_ny(b["t"])
                m = t.hour * 60 + t.minute
                if t.date() == day and OPEN_MIN <= m < CLOSE_MIN:
                    out.setdefault(s, []).append([t.strftime("%H:%M"), b["o"], b["h"], b["l"], b["c"], b["v"]])
        token = doc.get("next_page_token")
        if not token:
            return out


# ------------------------------------------------------------------ the opening-range rule (variant B)

def orb(bars, last_price):
    """Opening range = 9:30-9:45 bars; signal = first 5-minute CLOSE above its high, closing by 15:30.
    Stop = range midpoint (rounded down to the cent), target = entry + 2 x (entry - stop)."""
    now = ny_now()
    m_now = now.hour * 60 + now.minute
    rng = [b for b in bars if b[0] < "09:45"]
    if m_now < OPEN_MIN or not bars:
        return {"status": "before the open"}
    if len(rng) < 3 and m_now < OPEN_MIN + 15:
        hi = max(b[2] for b in rng) if rng else None
        lo = min(b[3] for b in rng) if rng else None
        return {"status": "building the range", "range_high": hi, "range_low": lo}
    if not rng:
        return {"status": "no opening-range data"}
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    mid = math.floor((hi + lo) / 2 * 100) / 100
    res = {"range_high": hi, "range_low": lo, "stop": mid}
    for b in bars:
        if b[0] < "09:45":
            continue
        h, mi = map(int, b[0].split(":"))
        close_min = h * 60 + mi + 5
        if close_min > LAST_ENTRY_MIN:
            break
        if close_min > m_now:          # the bar is still forming
            break
        if b[4] > hi:
            entry = round(b[4], 2)
            target = round(entry + 2 * (entry - mid), 2)
            res.update(status="triggered", signal_time="%02d:%02d" % divmod(close_min, 60), entry=entry,
                       target=target, risk_per_share=round(entry - mid, 2))
            # what happened next (a bar touching both counts as the stop, as in the backtest)
            for later in bars:
                if later[0] <= b[0]:
                    continue
                if later[3] <= mid:
                    res.update(status="stopped out", exit_time=later[0])
                    break
                if later[2] >= target:
                    res.update(status="target hit", exit_time=later[0])
                    break
            return res
    if m_now >= LAST_ENTRY_MIN:
        res["status"] = "no breakout today"
    elif last_price is not None and last_price < lo:
        res["status"] = "below the range"
    else:
        res["status"] = "waiting"
        if last_price:
            res["distance_pct"] = round(100 * (hi / last_price - 1), 2)
    return res


# ------------------------------------------------------------------ endpoints

def api_watch(symbols):
    def work():
        snaps = snapshots(symbols)
        bars = today_bars(symbols)
        rows = []
        for s in symbols:
            sn = snaps.get(s) or {}
            last = (sn.get("latestTrade") or {}).get("p")
            prev = (sn.get("prevDailyBar") or {}).get("c")
            day = sn.get("dailyBar") or {}
            b = bars.get(s, [])
            rows.append({"symbol": s, "price": last, "prev_close": prev,
                         "change_pct": round(100 * (last / prev - 1), 2) if last and prev else None,
                         "open": b[0][1] if b else None, "day_high": max((x[2] for x in b), default=day.get("h")),
                         "day_low": min((x[3] for x in b), default=day.get("l")),
                         "trade_time": (sn.get("latestTrade") or {}).get("t"), "orb": orb(b, last)})
        spy = next((r for r in rows if r["symbol"] == "SPY"), None)
        market = None
        if spy and spy["price"] and spy["open"]:
            market = "SPY above its open" if spy["price"] >= spy["open"] else "SPY below its open: the routine says no new trades"
        return {"as_of_ny": ny_now().strftime("%Y-%m-%d %H:%M:%S"), "market": market, "rows": rows}
    return cached(("watch", tuple(symbols)), 4, work)


def api_bars(symbol):
    def work():
        b = today_bars([symbol]).get(symbol, [])
        last = (snapshots([symbol]).get(symbol) or {}).get("latestTrade", {}).get("p")
        return {"symbol": symbol, "bars": b, "orb": orb(b, last), "price": last}
    return cached(("bars", symbol), 4, work)


def api_news(symbols):
    def work():
        doc = alpaca("/v1beta1/news", symbols=",".join(symbols), limit=30, sort="desc")
        return {"news": [{"headline": (lambda h: h if len(h) <= 180 else h[:177] + "...")(n.get("headline") or ""), "source": n.get("source"), "url": n.get("url"),
                          "symbols": n.get("symbols"), "created_at": n.get("created_at")}
                         for n in doc.get("news", [])]}
    return cached(("news", tuple(symbols)), 60, work)


def api_movers():
    def work():
        try:
            universe = json.load(open(os.path.join(ROOT, "sim", "universe_alpaca.json")))
        except OSError:
            return {"error": "sim/universe_alpaca.json not found"}
        snaps = snapshots(universe)
        today = session_day().isoformat()
        rows = []
        for s, sn in snaps.items():
            t = (sn.get("latestTrade") or {})
            prev = (sn.get("prevDailyBar") or {}).get("c")
            if not t.get("p") or not prev or not t.get("t") or to_ny(t["t"]).date().isoformat() != today:
                continue
            rows.append({"symbol": s, "price": t["p"], "change_pct": round(100 * (t["p"] / prev - 1), 2)})
        rows.sort(key=lambda r: r["change_pct"])
        return {"as_of_ny": ny_now().strftime("%Y-%m-%d %H:%M"), "count": len(rows),
                "up": rows[::-1][:12], "down": rows[:12]}
    return cached("movers", 120, work)


def api_earnings():
    def work():
        day = session_day().isoformat()
        try:
            doc = get_json("https://api.nasdaq.com/api/calendar/earnings?date=" + day, timeout=15)
            rows = (((doc or {}).get("data") or {}).get("rows")) or []
            return {"date": day, "rows": [{"symbol": r.get("symbol"), "name": r.get("name"), "time": r.get("time"),
                                           "market_cap": r.get("marketCap")} for r in rows]}
        except Exception as e:  # the calendar is a convenience; the rest works without it
            return {"date": day, "error": "Earnings calendar unavailable (%s). Check the earnings calendar in your broker's app." % type(e).__name__}
    return cached("earnings", 1800, work)


def api_papertrader():
    out = {}
    for name in ("equity", "trades", "status"):
        try:
            out[name] = json.load(open(os.path.join(ROOT, "sim", "dashboard", name + ".json")))
        except (OSError, ValueError):
            out[name] = None
    return out


# ------------------------------------------------------------------ server

def symbols_param(q, default):
    raw = (q.get("symbols") or [default])[0]
    syms = []
    for s in raw.upper().replace(" ", "").split(","):
        if s and len(s) <= 10 and all(ch.isalnum() or ch in ".-" for ch in s) and s not in syms:
            syms.append(s)
    return syms[:30]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/watch":
                return self.send(200, api_watch(symbols_param(q, "SPY,QQQ,AAPL,MSFT,NVDA,AMD,AMZN")))
            if u.path == "/api/bars":
                return self.send(200, api_bars(symbols_param(q.get("symbol") and {"symbols": q["symbol"]} or {}, "SPY")[0]))
            if u.path == "/api/news":
                return self.send(200, api_news(symbols_param(q, "SPY,QQQ,AAPL,MSFT,NVDA,AMD,AMZN")))
            if u.path == "/api/movers":
                return self.send(200, api_movers())
            if u.path == "/api/earnings":
                return self.send(200, api_earnings())
            if u.path == "/api/papertrader":
                return self.send(200, api_papertrader())
            return self.send(404, {"error": "not found"})
        except urllib.error.HTTPError as e:
            return self.send(502, {"error": "Alpaca answered HTTP %d" % e.code})
        except Exception as e:
            return self.send(500, {"error": str(e)})


def main():
    try:
        keys()
    except RuntimeError as e:
        print(e)
        return 1
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Trading desk running: open http://localhost:%d   (Ctrl+C to stop)" % PORT)
    if "--open" in sys.argv:
        import webbrowser
        threading.Timer(1.0, webbrowser.open, ["http://localhost:%d" % PORT]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
