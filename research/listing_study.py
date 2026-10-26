"""Event study: can a human profit from exchange listing announcements by buying a few minutes late?

Events: Upbit "KRW market addition" notices (Upbit notice API) and Binance futures-listing
announcements (Binance CMS API). Prices: 1-minute perpetual-futures candles from Binance, Bybit
or OKX for the token (whichever has data around the announcement, Binance last for Binance events
since its contract does not exist before the listing).

For each event: reference = last close before the announcement minute; entry = close of the
minute DELAY minutes after it (plus 0.3% slippage); exits at fixed horizons or TP/SL. 0.2% fees.

  python research/listing_study.py
"""

import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0"}
CACHE = "results/listings"
COST = 0.002 + 0.003  # fees + slippage on a liquid perp


def get(url):
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 429:
                return None  # unknown symbol etc.: retrying won't help
            time.sleep(5 * (i + 1))
        except Exception:
            time.sleep(2 + 3 * i)
    return None


def upbit_events(pages=41):
    ev = []
    for pg in range(1, pages + 1):
        d = get(f"https://api-manager.upbit.com/api/v1/announcements?os=web&page={pg}&per_page=20&category=trade")
        for n in ((d or {}).get("data") or {}).get("notices", []):
            t = n["title"]
            if "KRW" in t and "추가" in t and "마켓" in t:
                for sym in re.findall(r"\(([A-Z0-9]{2,12})\)", t):
                    ts = datetime.fromisoformat(n["first_listed_at"]).timestamp()
                    ev.append({"src": "upbit", "sym": sym, "t": ts, "title": t})
        time.sleep(0.3)
    return ev


def binance_events(pages=15):
    ev = []
    for pg in range(1, pages + 1):
        d = get(f"https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&catalogId=48&pageNo={pg}&pageSize=50")
        arts = (((d or {}).get("data") or {}).get("catalogs") or [{}])[0].get("articles", [])
        for a in arts:
            t = a["title"]
            if "Futures Will Launch" in t and "Perpetual" in t:
                for sym in re.findall(r"\b([A-Z0-9]{2,15})USDT\b", t):
                    if sym.startswith("1000"):
                        sym = sym[4:]
                    ev.append({"src": "binance_futures", "sym": sym, "t": a["releaseDate"] / 1000, "title": t})
        time.sleep(0.3)
    return ev


def candles(sym, t0, src):
    """1m candles from 15 min before to 24h after t0: [(ts, open, high, low, close)]."""
    path = os.path.join(CACHE, f"{src}_{sym}_{int(t0)}.json")
    if os.path.exists(path):
        return json.load(open(path))
    start, out = int((t0 - 900) * 1000), []
    venues = ["bybit", "okx", "binance"] if src == "binance_futures" else ["binance", "bybit", "okx"]
    for v in venues:
        rows = []
        for k in range(2):  # two pages of up to 1000 minutes cover 15 min before .. ~32h after
            s = start + k * 1000 * 60000
            if v == "binance":
                d = get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT&interval=1m&startTime={s}&limit=1000")
                rows += [(r[0] / 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in (d if isinstance(d, list) else [])]
            elif v == "bybit":
                d = get(f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}USDT&interval=1&start={s}&end={s + 999 * 60000}&limit=1000")
                rows += [(int(r[0]) / 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in ((d or {}).get("result") or {}).get("list", [])]
            else:
                rr = []
                after = s + 1000 * 60000
                for _ in range(10):  # OKX history pages of 100, walking back from `after`
                    d = get(f"https://www.okx.com/api/v5/market/history-candles?instId={sym}-USDT-SWAP&bar=1m&after={after}&limit=100")
                    page = (d or {}).get("data") or []
                    if not page:
                        break
                    rr += page
                    after = int(page[-1][0])
                    if after <= s:
                        break
                rows += [(int(r[0]) / 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rr if int(r[0]) >= s]
            time.sleep(0.15)
        rows = sorted(set(rows))
        if rows and rows[0][0] <= t0 - 60 and rows[-1][0] >= t0 + 3600:  # must cover before and after
            out = rows
            break
    os.makedirs(CACHE, exist_ok=True)
    json.dump(out, open(path, "w"))
    return out


def study(events):
    res = []
    for e in events:
        c = candles(e["sym"], e["t"], e["src"])
        if not c:
            continue
        tm = e["t"] - e["t"] % 60  # announcement minute
        before = [x for x in c if x[0] < tm]
        if not before:
            continue
        ref = before[-1][4]
        idx = {x[0]: x for x in c}

        def close_at(m):
            ts = tm + m * 60
            later = [x for x in c if x[0] >= ts]
            return later[0][4] if later else None

        row = {"src": e["src"], "sym": e["sym"], "t": e["t"], "ref": ref,
               "jump1": (close_at(0) or ref) / ref - 1, "c": c, "tm": tm}
        res.append(row)
    return res


def trade(r, delay, hold_min, tp=None, sl=None):
    c, tm = r["c"], r["tm"]
    ent = [x for x in c if x[0] >= tm + delay * 60]
    if not ent:
        return None
    e = ent[0][4] * 1.0  # close of the entry minute
    fut = [x for x in c if ent[0][0] < x[0] <= ent[0][0] + hold_min * 60]
    if not fut:
        return None
    for x in fut:
        if sl and x[3] <= e * (1 - sl):
            return -sl - COST
        if tp and x[2] >= e * (1 + tp):
            return tp - COST
    return fut[-1][4] / e - 1 - COST


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ev_path = os.path.join(CACHE, "_events.json")
    os.makedirs(CACHE, exist_ok=True)
    if os.path.exists(ev_path):
        events = json.load(open(ev_path))
    else:
        events = upbit_events() + binance_events()
        json.dump(events, open(ev_path, "w"))
    events = [e for e in events if e["t"] < time.time() - 86400]
    print(f"{sum(e['src'] == 'upbit' for e in events)} Upbit listing events, "
          f"{sum(e['src'] == 'binance_futures' for e in events)} Binance futures listing events", flush=True)
    rows = study(events)
    for src in ("upbit", "binance_futures"):
        R = [r for r in rows if r["src"] == src]
        if not R:
            continue
        print(f"\n===== {src}: {len(R)} events with minute prices around the announcement")
        print(f"  move in the announcement minute: median {statistics.median(r['jump1'] for r in R):+.1%}")
        for delay in (1, 3, 5, 10, 30):
            print(f"  -- buy {delay} min after the announcement")
            for label, hold, tp, sl in [("hold 30m", 30, None, None), ("hold 1h", 60, None, None), ("hold 4h", 240, None, None),
                                         ("hold 24h", 1440, None, None), ("TP20/SL10/4h", 240, 0.2, 0.1)]:
                v = [x for x in (trade(r, delay, hold, tp, sl) for r in R) if x is not None]
                if v:
                    print(f"     {label:13s} n={len(v):3d} win={sum(x > 0 for x in v)/len(v):.2f} "
                          f"mean={statistics.mean(v):+.3f} median={statistics.median(v):+.3f}")
    # time split for the most promising cell, so one lucky period can't carry the result
    for src in ("upbit", "binance_futures"):
        R = sorted((r for r in rows if r["src"] == src), key=lambda r: r["t"])
        if len(R) >= 20:
            h = len(R) // 2
            for name, part in (("older half", R[:h]), ("newer half", R[h:])):
                v = [x for x in (trade(r, 3, 60) for r in part) if x is not None]
                print(f"  {src} {name}: buy +3m, hold 1h: n={len(v)} mean={statistics.mean(v):+.3f} median={statistics.median(v):+.3f}")


if __name__ == "__main__":
    main()
