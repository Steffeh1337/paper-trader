#!/usr/bin/env python3
"""Backtest step 1 (Alpaca): 5-minute bars for the most-traded US stocks.

Keys come from the environment (APCA_API_KEY_ID, APCA_API_SECRET_KEY) and are
never printed or written. Free plan: the IEX feed (one exchange's trades), so
volumes are a slice of the market's, but prices and breakouts track it.

1. Universe: trader/universe.txt (top 600). Liquidity is judged on SELECTION_MONTH, before
   the test period, so the stock list does not know what happened later.
   Keep stocks with a median close of $5-$150, rank by median daily IEX dollar
   volume, keep the top N.
2. 5-minute bars from START to END for those stocks and SPY, saved per stock in
   the Yahoo chart JSON shape the skill's tools read (gzip).

  python3 backtest/alpaca_fetch.py
"""
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "backtest", "cache", "alpaca")
BASE = "https://data.alpaca.markets/v2/stocks/bars"
SELECTION = ("2026-01-02T00:00:00Z", "2026-01-31T00:00:00Z")
START, END = "2026-02-01T00:00:00Z", "2026-11-05T00:00:00Z"
TOP_N = 600
PAUSE = 0.35
WORKERS = 6    # parallel downloads: about 100 requests a minute, under the free plan's 200


def headers():
    return {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
            "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"], "Accept": "application/json"}


def get(params):
    url = BASE + "?" + urllib.parse.urlencode(params)
    for i in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers()), timeout=60) as r:
                body = json.load(r)
            time.sleep(PAUSE)
            return body
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(30 * (i + 1))
                continue
            if e.code >= 500:
                time.sleep(5)
                continue
            raise RuntimeError("HTTP %d: %s" % (e.code, e.read()[:200]))
        except (urllib.error.URLError, OSError):
            time.sleep(5)
    raise RuntimeError("gave up on %s" % params.get("symbols", "")[:60])


def bars(symbols, timeframe, start, end):
    """All bars for these symbols, following the pages."""
    out = defaultdict(list)
    token = None
    while True:
        p = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "end": end,
             "feed": "iex", "adjustment": "split", "limit": 10000}
        if token:
            p["page_token"] = token
        doc = get(p)
        for s, rows in (doc.get("bars") or {}).items():
            out[s].extend(rows)
        token = doc.get("next_page_token")
        if not token:
            return out


def save(sym, rows):
    ts, o, h, l, c, v = [], [], [], [], [], []
    for b in rows:
        t = int(datetime.strptime(b["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
        if t % 300:
            continue
        ts.append(t); o.append(b["o"]); h.append(b["h"]); l.append(b["l"]); c.append(b["c"]); v.append(b["v"])
    doc = {"chart": {"result": [{"meta": {"symbol": sym.replace(".", "-"), "dataGranularity": "5m",
                                          "regularMarketTime": 4102444800},
                                 "timestamp": ts,
                                 "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v}]}}],
                     "error": None}}
    with gzip.open(os.path.join(OUT, sym + ".json.gz"), "wt") as f:
        json.dump(doc, f, separators=(",", ":"))


def main():
    os.makedirs(OUT, exist_ok=True)
    syms = [l.split("|")[0] for l in open(os.path.join(ROOT, "trader", "universe.txt")) if l.strip()]
    stats = []
    for i in range(0, len(syms), 200):
        batch = syms[i:i + 200]
        try:
            daily = bars(batch, "1Day", *SELECTION)
        except RuntimeError as e:
            print("daily batch failed:", e, file=sys.stderr)
            continue
        for s, rows in daily.items():
            if len(rows) < 10:
                continue
            closes = sorted(r["c"] for r in rows)
            dollars = sorted(r["c"] * r["v"] for r in rows)
            stats.append((dollars[len(dollars) // 2], closes[len(closes) // 2], s))
    keep = [s for d, c, s in sorted(stats, reverse=True) if 5 <= c <= 150][:TOP_N]
    json.dump(keep, open(os.path.join(ROOT, "backtest", "cache", "alpaca_symbols.json"), "w"))
    print("January 2026: %d stocks with data, kept the top %d priced $5-$150 by IEX dollar volume" % (len(stats), len(keep)), flush=True)

    todo = [s for s in keep + ["SPY"] if not os.path.exists(os.path.join(OUT, s + ".json.gz"))]

    def one(sym):
        try:
            got = bars([sym], "5Min", START, END)
        except RuntimeError as e:
            return sym, "failed %s" % e
        if got.get(sym):
            save(sym, got[sym])
            return sym, "ok"
        return sym, "no bars"

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for i, (sym, status) in enumerate(ex.map(one, todo)):
            if status != "ok":
                print(sym, status, file=sys.stderr)
            if (i + 1) % 25 == 0:
                print("5-minute bars: %d of %d stocks" % (i + 1, len(todo)), flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
