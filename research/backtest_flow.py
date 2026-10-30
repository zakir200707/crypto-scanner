"""Do buyer/seller flow, liquidity, socials, boosts or dev size pick winners among coins 10 min to 4h
past graduation? Uses the live watcher's own snapshots (research/export_snaps.py): every coin that
graduated in the window, dead ones included, about 2 days, polled every minute or so.

Signal at a snapshot; entry = the next snapshot's price + 2%, 60s+ later (time to see it and buy).
Stops fill at the first snapshot at or below the stop, so gaps cost what they really cost. Costs 3.7%.
Rules are ranked on the older half of the coins and judged on the newer half, against random entries.

  python research/backtest_flow.py results/snaps_cloud/snaps.jsonl.gz
"""

import gzip
import json
import random
import statistics
import sys
from collections import defaultdict

COST = 0.037
DELAY = 60
MIN_LIQ, MIN_V1H = 10_000, 5_000
AGES = {"10-30m": (600, 1800), "30-60m": (1800, 3600), "1-2h": (3600, 7200), "2-4h": (7200, 14400)}
MCS = {"under $100K": (0, 1e5), "$100-300K": (1e5, 3e5), "$300K+": (3e5, 1e12)}
EXITS = {"TP30/SL20/2h": (0.3, 0.2, 7200), "TP50/SL25/3h": (0.5, 0.25, 10800), "TP100/SL35/3h": (1.0, 0.35, 10800)}


def ratio(a, b):
    return a / max(1, b)


RULES = {
    "baseline: any time": lambda s: True,
    "1h buys >= 1.25x sells": lambda s: ratio(s["b1h"], s["s1h"]) >= 1.25,
    "1h buys >= 1.5x sells": lambda s: ratio(s["b1h"], s["s1h"]) >= 1.5,
    "1h buys <= 0.8x sells": lambda s: ratio(s["b1h"], s["s1h"]) <= 0.8,
    "5m buys >= 2x sells": lambda s: ratio(s["b5"], s["s5"]) >= 2 and s["b5"] >= 10,
    "liq >= 25% of MC": lambda s: s["liq"] >= 0.25 * s["mc"],
    "has socials": lambda s: s["socials"] >= 1,
    "2+ socials": lambda s: s["socials"] >= 2,
    "no socials": lambda s: s["socials"] == 0,
    "boosted": lambda s: s["boost"] > 0,
    "dev bought under 0.5 SOL": lambda s: (s.get("dev_sol") or 0) < 0.5,
    "dev bought 2+ SOL": lambda s: (s.get("dev_sol") or 0) >= 2,
    "1h volume >= 2x MC": lambda s: s["v1h"] >= 2 * s["mc"],
    "1h volume under 0.5x MC": lambda s: s["v1h"] < 0.5 * s["mc"],
    "down 30%+ in 1h, 5m buys > sells": lambda s: s["pc1h"] <= -30 and s["b5"] > s["s5"],
    "down 50%+ off high, 5m buys > sells": lambda s: s["dd"] >= 0.5 and s["b5"] > s["s5"],
    "within 10% of high": lambda s: s["dd"] <= 0.1,
    "up 5m, 1h buys >= 1.25x sells, socials": lambda s: s["pc5"] > 0 and ratio(s["b1h"], s["s1h"]) >= 1.25 and s["socials"] >= 1,
}


def load(path):
    c = defaultdict(list)
    for l in gzip.open(path, "rt", encoding="utf-8"):
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("price"):
            c[r["mint"]].append(r)
    out = []
    for h in c.values():
        h.sort(key=lambda r: r["t"])
        top = 0
        for s in h:
            top = max(top, s["price"])
            s["dd"] = 1 - s["price"] / top
        out.append(h)
    return sorted(out, key=lambda h: h[0]["grad"])


def trade(h, i, tp, sl, hold):
    t = h[i]["t"] + DELAY
    j = next((k for k in range(i + 1, len(h)) if h[k]["t"] >= t), None)
    if j is None:
        return None
    e = h[j]["price"] * 1.02
    stop, target = e * (1 - sl), e * (1 + tp)
    for k in range(j + 1, len(h)):
        p = h[k]["price"]
        if p <= stop:
            return p / e - 1 - COST
        if p >= target:
            return tp - COST
        if h[k]["t"] > h[j]["t"] + hold:
            return p / e - 1 - COST
    return h[-1]["price"] / e - 1 - COST


def tstat(v):
    return statistics.mean(v) / (statistics.stdev(v) / len(v) ** 0.5)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    coins = load(sys.argv[1])
    cut = coins[len(coins) // 2][0]["grad"]
    print(f"{len(coins)} graduated coins, {sum(map(len, coins))} snapshots")
    res = defaultdict(lambda: ([], []))
    rnd = random.Random(1)
    for h in coins:
        part = 0 if h[0]["grad"] < cut else 1
        for an, (a0, a1) in AGES.items():
            for mn, (m0, m1) in MCS.items():
                band = [i for i, s in enumerate(h) if a0 <= s["t"] - s["grad"] < a1 and m0 <= s["mc"] < m1
                        and s["liq"] >= MIN_LIQ and s["v1h"] >= MIN_V1H]
                if not band:
                    continue
                for rn, f in RULES.items():
                    hit = [i for i in band if f(h[i])]
                    if not hit:
                        continue
                    i = rnd.choice(hit) if rn.startswith("baseline") else hit[0]
                    for en, (tp, sl, hold) in EXITS.items():
                        v = trade(h, i, tp, sl, hold)
                        if v is not None:
                            res[(rn, an, mn, en)][part].append(v)
    rows = [(k, a, b) for k, (a, b) in res.items() if len(a) >= 15 and len(b) >= 15]
    json.dump({" | ".join(k): [a, b] for k, a, b in rows}, open("results/flow_backtest.json", "w"))
    pos = [r for r in rows if not r[0][0].startswith("baseline")]
    both = [r for r in pos if statistics.mean(r[1]) > 0 and statistics.mean(r[2]) > 0]
    print(f"{len(pos)} combos with 15+ trades in both halves; positive on train {sum(statistics.mean(r[1]) > 0 for r in pos)}, "
          f"positive on both {len(both)}")
    print("\nrandom entries, TP50/SL25/3h:")
    for k, a, b in sorted(rows, key=lambda r: r[0]):
        if k[0].startswith("baseline") and k[3] == "TP50/SL25/3h":
            print(f"  {k[1]:7s} {k[2]:12s} train n={len(a):3d} {statistics.mean(a):+.1%} | test n={len(b):3d} {statistics.mean(b):+.1%}")
    print("\nbest on train, then their test result:")
    for k, a, b in sorted(pos, key=lambda r: -statistics.mean(r[1]))[:12]:
        print(f"  {statistics.mean(a):+6.1%} n={len(a):3d} -> test {statistics.mean(b):+6.1%} n={len(b):3d} "
              f"win {sum(x > 0 for x in b) / len(b):.0%}  {' | '.join(k)}")
    print("\npositive on both halves, by combined t-stat:")
    for k, a, b in sorted(both, key=lambda r: tstat(r[1] + r[2]), reverse=True)[:15]:
        print(f"  t={tstat(a + b):4.1f} train {statistics.mean(a):+6.1%} n={len(a):3d} | test {statistics.mean(b):+6.1%} n={len(b):3d} "
              f"median {statistics.median(a + b):+6.1%}  {' | '.join(k)}")
    print("\neach rule pooled over all bands, TP50/SL25/3h, one trade per coin-band:")
    pool = defaultdict(lambda: ([], []))
    for (rn, an, mn, en), (a, b) in res.items():
        if en == "TP50/SL25/3h":
            pool[rn][0].extend(a)
            pool[rn][1].extend(b)
    for rn, (a, b) in sorted(pool.items(), key=lambda kv: -statistics.mean(kv[1][0] + kv[1][1])):
        print(f"  {statistics.mean(a + b):+6.1%} n={len(a + b):4d} (train {statistics.mean(a):+6.1%}, test {statistics.mean(b):+6.1%})  {rn}")


main()
