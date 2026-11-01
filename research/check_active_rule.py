"""Independent check of the one idea that survived research/backtest_dataset.py: coins under 1h old with heavy
trading, sold at +100% or -35%. Thresholds are fixed from that test, not fitted here. Run on our own October
data, where dead coins are kept, because the June candles are missing for most coins.

Watcher: results/snaps_cloud/snaps.jsonl.gz, entry = next snapshot 60s+ later + 2%, held up to 3h of what was
recorded (coins were followed about 5h). Candles: results/grads_gh, 5-minute, entry = next open + 2%, up to 48h.
Costs 3.7%. Baseline = a random moment in the same age band.

  python research/check_active_rule.py
"""

import glob
import gzip
import json
import random
import statistics
import sys
from collections import defaultdict

COST = 0.037
TP, SL = 1.0, 0.35
AGE = (600, 3600)
VOLS = (75_000, 150_000)


def show(name, v):
    if not v:
        print(f"  {name}: no trades")
        return
    half = len(v) // 2
    t = statistics.mean(v) / (statistics.stdev(v) / len(v) ** 0.5) if len(v) > 2 else 0
    print(f"  {name}: {statistics.mean(v):+.1%} n={len(v)} t={t:.1f} win {sum(x > 0 for x in v) / len(v):.0%} "
          f"median {statistics.median(v):+.0%} | older {statistics.mean(v[:half] or [0]):+.1%} newer {statistics.mean(v[half:]):+.1%}")


def snap_trade(h, i, hold=10800):
    j = next((k for k in range(i + 1, len(h)) if h[k]["t"] >= h[i]["t"] + 60), None)
    if j is None:
        return None
    e = h[j]["price"] * 1.02
    for k in range(j + 1, len(h)):
        p = h[k]["price"]
        if p <= e * (1 - SL):
            return p / e - 1 - COST
        if p >= e * (1 + TP):
            return TP - COST
        if h[k]["t"] > h[j]["t"] + hold:
            return p / e - 1 - COST
    return h[-1]["price"] / e - 1 - COST


def watcher(path):
    c = defaultdict(list)
    for l in gzip.open(path, "rt", encoding="utf-8"):
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("price"):
            c[r["mint"]].append(r)
    coins = sorted((sorted(h, key=lambda r: r["t"]) for h in c.values()), key=lambda h: h[0]["grad"])
    rnd = random.Random(1)
    base, hit = [], {x: [] for x in VOLS}
    for h in coins:
        band = [i for i, s in enumerate(h) if AGE[0] <= s["t"] - s["grad"] < AGE[1] and s.get("liq", 0) >= 10_000]
        if not band:
            continue
        v = snap_trade(h, rnd.choice(band))
        if v is not None:
            base.append(v)
        for x in VOLS:
            i = next((i for i in band if h[i]["v1h"] >= x), None)
            v = snap_trade(h, i) if i is not None else None
            if v is not None:
                hit[x].append(v)
    print(f"watcher snapshots: {len(coins)} graduated coins, dead ones included")
    show("random moment 10m-1h", base)
    for x in VOLS:
        show(f"1h volume >= ${x:,}", hit[x])


def candle_trade(k, i):
    if i + 1 >= len(k) or k[i + 1][1] <= 0:
        return None
    e = k[i + 1][1] * 1.02
    for x in k[i + 1:]:
        if x[0] > k[i + 1][0] + 48 * 3600:
            break
        if x[3] <= e * (1 - SL):
            return min(e * (1 - SL), x[1]) / e - 1 - COST
        if x[2] >= e * (1 + TP):
            return TP - COST
    return k[-1][4] / e - 1 - COST


def candles():
    coins = []
    for f in glob.glob("results/grads_gh/*.json"):
        d = json.load(open(f))
        if d.get("candles"):
            coins.append((d["grad"], sorted(d["candles"])))
    coins.sort(key=lambda c: c[0])
    rnd = random.Random(1)
    base, hit = [], {x: [] for x in VOLS}
    for grad, k in coins:
        band = [i for i, x in enumerate(k) if AGE[0] <= x[0] + 300 - grad < AGE[1]]
        if not band:
            continue
        v = candle_trade(k, rnd.choice(band))
        if v is not None:
            base.append(v)
        for x in VOLS:
            i = next((i for i in band if sum(c[5] for c in k if k[i][0] - 3600 < c[0] <= k[i][0]) >= x), None)
            v = candle_trade(k, i) if i is not None else None
            if v is not None:
                hit[x].append(v)
    print(f"5-minute candles, 25-26 Oct: {len(coins)} coins with any candles")
    show("random moment 10m-1h", base)
    for x in VOLS:
        show(f"1h volume >= ${x:,}", hit[x])


sys.stdout.reconfigure(encoding="utf-8")
watcher("results/snaps_cloud/snaps.jsonl.gz")
candles()
