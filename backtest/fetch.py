#!/usr/bin/env python3
"""Backtest step 1: download Yahoo's 60 days of 5-minute bars once per stock.

Universe: trader/universe.txt (the skill's Nasdaq Trader list of US common
stocks), narrowed with one Yahoo spark pass to stocks priced $5-$150 with some
trading volume today. Each chart is saved as the raw Yahoo answer (gzip), the
format the skill's tools read with --offline.

  python3 backtest/fetch.py
"""
import concurrent.futures as cf
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "backtest", "cache", "charts")
UA = "paper-trader-backtest/1.0 (personal research)"
MIN_PRICE, MAX_PRICE, MIN_VOLUME = 5.0, 150.0, 200_000
PAUSE = 1.0   # seconds between requests


def get(path, tries=5):
    """One polite request: on 429 (too many requests) wait and try again later."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request("https://query2.finance.yahoo.com%s" % path,
                                         headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
            time.sleep(PAUSE)
            return body
        except urllib.error.HTTPError as e:
            last = e
            time.sleep(60 * (i + 1) if e.code == 429 else 3)
        except (urllib.error.URLError, OSError) as e:
            last = e
            time.sleep(5)
    raise RuntimeError("%s: %s" % (path, last))


def main():
    os.makedirs(CACHE, exist_ok=True)
    syms = [l.split("|")[0] for l in open(os.path.join(ROOT, "trader", "universe.txt")) if l.strip()]
    ysym = lambda s: s.replace(".", "-")
    keep = []
    for i in range(0, len(syms), 20):
        batch = syms[i:i + 20]
        try:
            doc = json.loads(get("/v7/finance/spark?symbols=%s&range=5d&interval=1d" % ",".join(map(ysym, batch))))
        except RuntimeError as e:
            print("spark failed:", e, file=sys.stderr)
            continue
        for r in (doc.get("spark") or {}).get("result") or []:
            try:
                meta = r["response"][0]["meta"]
            except (KeyError, IndexError, TypeError):
                continue
            px, vol = meta.get("regularMarketPrice") or 0, meta.get("regularMarketVolume") or 0
            if MIN_PRICE <= px <= MAX_PRICE and vol >= MIN_VOLUME:
                keep.append(r["symbol"])
    print("universe %d, kept %d by price $%.0f-$%.0f and volume >= %d" % (len(syms), len(keep), MIN_PRICE, MAX_PRICE, MIN_VOLUME), flush=True)

    def one(sym):
        out = os.path.join(CACHE, sym + ".json.gz")
        if os.path.exists(out):
            return sym, "cached"
        try:
            body = get("/v8/finance/chart/%s?range=60d&interval=5m" % sym)
        except RuntimeError as e:
            return sym, "failed %s" % e
        with gzip.open(out, "wb") as f:
            f.write(body)
        return sym, "ok"

    done = 0
    with cf.ThreadPoolExecutor(1) as ex:
        for sym, status in ex.map(one, keep):
            done += 1
            if status.startswith("failed"):
                print(sym, status, file=sys.stderr)
            if done % 200 == 0:
                print("charts %d of %d" % (done, len(keep)), flush=True)
    json.dump(keep, open(os.path.join(ROOT, "backtest", "cache", "symbols.json"), "w"))
    print("done", flush=True)


if __name__ == "__main__":
    main()
