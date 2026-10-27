"""How did the 15-minute scanner's picks actually do? Parses the GitHub Actions scan logs
(results/scanlogs/*.txt), takes the first time each coin was the BEST PICK (and whether it was
alerted), then follows its price with GeckoTerminal hourly candles and applies the alert plan:
stop -25%; sell half at +50%, then the rest at +100% or back at the entry price; 72h limit.

  python research/score_scanner.py
"""

import glob
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
CACHE = "results/scanlogs/candles"
COST = 0.03


def get(url):
    for i in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15 * (i + 1))
                continue
            return None
        except Exception:
            time.sleep(5)
    return None


def parse():
    picks = {}
    for f in sorted(glob.glob("results/scanlogs/*.txt")):
        lines = open(f, encoding="utf-8", errors="ignore").read().splitlines()
        alerted = {re.sub(r".*NEW \w+ PICK: ", "", l).replace(" ***", "").strip() for l in lines if "NEW " in l and " PICK: " in l}
        for i, l in enumerate(lines):
            m = re.search(r"BEST PICK: (.*) \((.*)\)\s+score ([\d.]+)/100", l)
            if not m:
                continue
            ts = datetime.fromisoformat(l[:19] + "+00:00").timestamp()
            blk = "\n".join(lines[i:i + 6])
            mint = re.search(r"mint\s+(\w+)", blk)
            pair = re.search(r"dexscreener\.com/solana/(\w+)", blk)
            price = re.search(r"price \$([\d.e+-]+)", blk)
            if not (mint and pair and price):
                continue
            sym = m.group(2)
            if mint.group(1) not in picks:
                picks[mint.group(1)] = {"sym": sym, "t": ts, "score": float(m.group(3)), "pair": pair.group(1),
                                        "price": float(price.group(1)), "alerted": False}
            if sym in alerted:
                picks[mint.group(1)]["alerted"] = True
    return picks


def candles(pair_lower):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, pair_lower + ".json")
    if os.path.exists(path):
        return json.load(open(path))
    d = get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_lower}") or {}
    p = (d.get("pairs") or [None])[0]
    out = []
    if p:
        g = get(f"{GT}/pools/{p['pairAddress']}/ohlcv/hour?limit=200") or {}
        out = sorted(((g.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
        time.sleep(6)
    json.dump(out, open(path, "w"))
    return out


def plan(c, t, entry):
    """Alert plan on hourly candles after time t. Returns (return, how)."""
    fut = [x for x in c if x[0] >= t]
    if not fut:
        return None, "no data"
    got, left, stage = 0.0, 1.0, 0
    for ts, o, h, l, cl, v in fut:
        if ts > t + 72 * 3600:
            break
        if stage == 0:
            if l <= entry * 0.75:
                return 0.75 - 1 - COST, "stop"
            if h >= entry * 1.5:
                got, left, stage = 0.75, 0.5, 1
        if stage == 1:
            if h >= entry * 2:
                return got + left * 2 - 1 - COST, "+100%"
            if l <= entry:
                return got + left * 1.0 - 1 - COST, "half +50%, rest back at entry"
    last = [x for x in fut if x[0] <= t + 72 * 3600][-1][4]
    return got + left * last / entry - 1 - COST, "72h"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    picks = parse()
    print(f"{len(picks)} different coins were the scanner's best pick; {sum(p['alerted'] for p in picks.values())} were alerted")
    rows = []
    for mint, p in picks.items():
        c = candles(p["pair"])
        entry = p["price"] * 1.02  # we pay a bit more than the logged price
        r, how = plan(c, p["t"], entry)
        fut = [x for x in c if p["t"] <= x[0] <= p["t"] + 24 * 3600]
        mx = max([x[2] for x in fut] or [0]) / entry
        mn = min([x[3] for x in fut] or [0]) / entry
        if r is not None:
            rows.append({**p, "ret": r, "how": how, "max24": mx, "min24": mn})
    for label, sel in (("ALERTED picks", [r for r in rows if r["alerted"]]), ("all best picks", rows),
                       ("best picks with score >= 60", [r for r in rows if r["score"] >= 60])):
        if not sel:
            continue
        v = [r["ret"] for r in sel]
        print(f"\n{label}: n={len(v)} win={sum(x > 0 for x in v)/len(v):.2f} mean={statistics.mean(v):+.3f} "
              f"median={statistics.median(v):+.3f}  | went +50% within 24h: {sum(r['max24'] >= 1.5 for r in sel)/len(sel):.0%}, "
              f"fell 25% within 24h: {sum(r['min24'] <= 0.75 for r in sel)/len(sel):.0%}")
    print()
    for r in sorted(rows, key=lambda r: r["t"]):
        print(f"  {datetime.utcfromtimestamp(r['t']):%d %H:%M} {r['sym'][:12]:12s} score {r['score']:5.1f} {'ALERT' if r['alerted'] else '     '} "
              f"{r['ret']:+.0%} ({r['how']}) max24 x{r['max24']:.2f} min24 x{r['min24']:.2f}")


if __name__ == "__main__":
    main()
