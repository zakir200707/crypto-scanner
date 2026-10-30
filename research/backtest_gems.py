"""Do any "older gem" setups beat random entries? Coins 2h+ past graduation, on 5-minute candles.
Train: 953 graduated Pump.fun coins of 25-26 Oct (results/grads_gh, dead coins included).
Test: this week's coins that were alive 20+ min after graduation (results/recent_cloud/files/recent).

Signal at a candle close; entry = next candle's open + 2%; one trade per coin per rule. A stop fills
at the stop or at the candle's open if it gapped through, and comes first when a candle hits both.
Costs 3.7% (Axiom 0.95% a side, 1% slippage, $0.04 of fees on $5). Rules are ranked on the train
period and judged on the test period; each is compared with random entries in the same band.

  python research/backtest_gems.py
"""

import glob
import json
import random
import statistics
import sys
from collections import defaultdict

import numpy as np

COST = 0.037
STEP = 300
H = 12               # 5-minute candles per hour
MIN_V1H = 5000       # USD traded in the last hour, or a small order moves the price
AGES = {"2-12h": (2, 12), "12-48h": (12, 48), "48h+": (48, 1e9)}
MCS = {"$30-300K": (3e4, 3e5), "$300K+": (3e5, 1e12)}
EXITS = {"TP30/SL20/6h": (0.3, 0.2, 6), "TP50/SL25/24h": (0.5, 0.25, 24), "TP100/SL35/48h": (1.0, 0.35, 48),
         "SL25/24h": (None, 0.25, 24)}


def load():
    coins = []
    for f in glob.glob("results/grads_gh/*.json") + glob.glob("results/recent_cloud/files/recent/*.json"):
        d = json.load(open(f))
        k = d.get("candles") or []
        if len(k) < 30:
            coins.append({"grad": d["grad"], "n": 0})
            continue
        k = sorted(k)
        t0 = k[0][0]
        n = (k[-1][0] - t0) // STEP + 1
        idx = {x[0]: x for x in k}
        o, h, l, c, v = [np.zeros(n) for _ in range(5)]
        last = k[0][1]
        for i in range(n):
            x = idx.get(t0 + i * STEP)
            if x:
                o[i], h[i], l[i], c[i], v[i] = x[1], x[2], x[3], x[4], x[5]
                last = x[4]
            else:
                o[i] = h[i] = l[i] = c[i] = last
        coins.append({"grad": d["grad"], "t0": t0, "n": n, "o": o, "h": h, "l": l, "c": c, "v": v})
    return sorted(coins, key=lambda x: x["grad"])


def roll(a, w):
    s = np.concatenate(([0.0], np.cumsum(a)))
    out = np.zeros(len(a))
    out[w - 1:] = s[w:] - s[:-w]
    return out


def feats(x):
    c, h, l, v, n = x["c"], x["h"], x["l"], x["v"], x["n"]
    age = (x["t0"] + np.arange(n) * STEP + STEP - x["grad"]) / 3600
    ath = np.maximum.accumulate(h)
    prev_ath = np.concatenate(([h[0]], ath[:-1]))
    v1 = roll(v, H)
    prior = np.zeros(n)                  # average hourly volume of the 6 hours before the last hour
    if n > 7 * H:
        v6 = roll(v, 6 * H)
        prior[7 * H - 1:] = v6[6 * H - 1:n - H] / 6
    volx = v1 / np.maximum(prior, 1)
    volx[:7 * H] = 0
    ch1 = np.zeros(n)
    ch1[H:] = c[H:] / c[:-H] - 1
    low_since = np.zeros(n)              # lowest price since the all-time high
    lo, a = l[0], h[0]
    for i in range(n):
        if h[i] >= a:
            a, lo = h[i], l[i]
        lo = min(lo, l[i])
        low_since[i] = lo
    rng = {}
    for w in (6, 12, 24):                # highest price of the previous w hours
        m = np.full(n, np.inf)
        if n > w * H:
            m[w * H:] = np.lib.stride_tricks.sliding_window_view(h, w * H).max(axis=1)[:n - w * H]
        rng[w] = m
    return dict(age=age, mc=c * 1e9, v1=v1, volx=volx, ch1=ch1, dd=1 - c / ath, reb=c / low_since - 1,
                ath_new=(c > prev_ath), rng=rng, c=c)


def rules(f):
    r = {}
    for dd in ((0.2, 0.4), (0.4, 0.6), (0.6, 0.8)):
        for vx in (2, 4):
            r[f"pullback {dd[0]:.0%}-{dd[1]:.0%} off high, 1h up, vol {vx}x"] = (
                (f["dd"] >= dd[0]) & (f["dd"] < dd[1]) & (f["ch1"] > 0) & (f["volx"] >= vx))
    for w in (6, 12, 24):
        for vx in (2, 4):
            r[f"breakout above {w}h range, vol {vx}x"] = (f["c"] > f["rng"][w]) & (f["volx"] >= vx)
    for rb in (0.5, 1.0):
        for vx in (2, 4):
            r[f"second wave: 70%+ off high, +{rb:.0%} off low, vol {vx}x"] = (
                (f["dd"] >= 0.7) & (f["reb"] >= rb) & (f["volx"] >= vx))
    for vx in (2, 4):
        r[f"new all-time high, vol {vx}x"] = f["ath_new"] & (f["volx"] >= vx)
    for vx in (3, 6):
        r[f"quiet accumulation: flat 1h, vol {vx}x"] = (abs(f["ch1"]) < 0.1) & (f["volx"] >= vx)
    r["baseline: any time"] = np.ones(len(f["c"]), bool)
    return r


def trade(x, i, tp, sl, hold):
    j = i + 1
    if j >= x["n"]:
        return None
    e = x["o"][j] * 1.02
    if e <= 0:
        return None
    end = min(x["n"], j + hold * H)
    stop = e * (1 - sl)
    target = e * (1 + tp) if tp else None
    for k in range(j, end):
        if x["l"][k] <= stop:
            return min(stop, x["o"][k]) / e - 1 - COST
        if target and x["h"][k] >= target:
            return tp - COST
    return x["c"][end - 1] / e - 1 - COST


def tstat(v):
    return statistics.mean(v) / (statistics.stdev(v) / len(v) ** 0.5)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    coins = load()
    print(f"{len(coins)} coins, {sum(c['n'] > 0 for c in coins)} with 30+ candles")
    cut = 1793059200  # 27 Oct 00:00 UTC, between the two collection periods
    res = defaultdict(lambda: ([], []))
    rnd = random.Random(1)
    for x in coins:
        if not x["n"]:
            continue
        f = feats(x)
        part = 0 if x["grad"] < cut else 1
        rs = rules(f)
        ok = f["v1"] >= MIN_V1H
        for an, (a0, a1) in AGES.items():
            ma = ok & (f["age"] >= a0) & (f["age"] < a1)
            if not ma.any():
                continue
            for mn, (m0, m1) in MCS.items():
                band = ma & (f["mc"] >= m0) & (f["mc"] < m1)
                if not band.any():
                    continue
                for rn, m in rs.items():
                    hit = np.flatnonzero(m & band)
                    if not len(hit):
                        continue
                    i = rnd.choice(list(hit)) if rn.startswith("baseline") else int(hit[0])
                    for en, (tp, sl, hold) in EXITS.items():
                        v = trade(x, i, tp, sl, hold)
                        if v is not None:
                            res[(rn, an, mn, en)][part].append(v)
    pool = defaultdict(lambda: ([], []))
    for (rn, an, mn, en), (a, b) in res.items():
        pool[(rn, en)][0].extend(a)
        pool[(rn, en)][1].extend(b)
    pr = [(k, a, b) for k, (a, b) in pool.items() if len(a) >= 3 and len(b) >= 3]
    print(f"each rule pooled over ages and sizes: {len(pr)} rule/exit pairs, "
          f"{sum(statistics.mean(a) > 0 and statistics.mean(b) > 0 for k, a, b in pr)} positive in both periods")
    for k, a, b in sorted(pr, key=lambda r: -statistics.mean(r[1] + r[2]))[:40]:
        v = a + b
        print(f"  {statistics.mean(v):+6.1%} t={tstat(v):5.1f} win {sum(x > 0 for x in v) / len(v):3.0%} "
              f"train {statistics.mean(a):+6.1%} n={len(a):3d} test {statistics.mean(b):+6.1%} n={len(b):3d}  {' | '.join(k)}")
    rows = [(k, a, b) for k, (a, b) in res.items() if len(a) >= 10 and len(b) >= 10]
    json.dump({" | ".join(k): [a, b] for k, a, b in rows}, open("results/gems_backtest.json", "w"))
    pos = [r for r in rows if not r[0][0].startswith("baseline")]
    print(f"{len(pos)} rule/band/exit combos with 10+ trades in both periods")
    print(f"positive on train: {sum(statistics.mean(r[1]) > 0 for r in pos)}; "
          f"positive on both: {sum(statistics.mean(r[1]) > 0 and statistics.mean(r[2]) > 0 for r in pos)}")
    print("\nrandom entries (baseline), mean per trade:")
    for k, a, b in sorted(rows, key=lambda r: r[0]):
        if k[0].startswith("baseline") and k[3] == "TP50/SL25/24h":
            print(f"  {k[1]:7s} {k[2]:10s} train n={len(a):3d} {statistics.mean(a):+.1%} | test n={len(b):3d} {statistics.mean(b):+.1%}")
    print("\nbest on train, then their test result:")
    for k, a, b in sorted(pos, key=lambda r: -statistics.mean(r[1]))[:15]:
        print(f"  {statistics.mean(a):+6.1%} n={len(a):3d} -> test {statistics.mean(b):+6.1%} n={len(b):3d} "
              f"win {sum(x > 0 for x in b) / len(b):.0%}  {' | '.join(k)}")
    print("\npositive on both periods, by combined t-stat:")
    both = [r for r in pos if statistics.mean(r[1]) > 0 and statistics.mean(r[2]) > 0]
    for k, a, b in sorted(both, key=lambda r: tstat(r[1] + r[2]), reverse=True)[:20]:
        print(f"  t={tstat(a + b):4.1f} train {statistics.mean(a):+6.1%} n={len(a):3d} | test {statistics.mean(b):+6.1%} n={len(b):3d} "
              f"median {statistics.median(a + b):+6.1%}  {' | '.join(k)}")


main()
