"""Backtest entry rules on the minute dataset from market_minute.py (established memecoins).

Entry at the NEXT minute's price + 1% slippage; exits TP/SL/time on later minutes; 1% more costs.
At most one trade per coin per rule per 2 hours. Every rule is compared with random entries,
and with itself on the first vs second half of the recording.

  python research/score_minute.py out/minute.jsonl
"""

import collections
import json
import random
import statistics
import sys

COST = 0.02


def load(path):
    coins = collections.defaultdict(list)
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r["price"] > 0:
            coins[r["mint"]].append(r)
    for h in coins.values():
        h.sort(key=lambda r: r["t"])
        for i, r in enumerate(h):
            prev = [x["price"] for x in h[max(0, i - 60):i]]
            r["hi60"] = max(prev) if prev else r["price"]
            r["ret15"] = r["price"] / h[max(0, i - 15)]["price"] - 1
    return coins


RULES = {
    "volume spike, price calm": lambda r: r["v1h"] > 0 and r["v5"] >= 4 * r["v1h"] / 12 and 0 <= r["pc5"] < 10,
    "volume spike, price up": lambda r: r["v1h"] > 0 and r["v5"] >= 4 * r["v1h"] / 12 and r["pc5"] >= 10,
    "buyer surge (5m buys 3x hourly pace, buys>sells)": lambda r: r["b1h"] > 0 and r["b5"] >= 3 * r["b1h"] / 12 and r["b5"] > 1.5 * r["s5"],
    "breakout above 1h high with buyers": lambda r: r["price"] > r["hi60"] * 1.02 and r["b5"] > 1.3 * r["s5"],
    "dip in uptrend (24h +30%, 1h -10%)": lambda r: r["pc24"] > 30 and r["pc1h"] < -10,
    "big dip (1h -20%) on liquid coin": lambda r: r["pc1h"] < -20 and r["liq"] > 1e5,
    "momentum (15m +10%)": lambda r: r["ret15"] > 0.10,
}
EXITS = {"TP10/SL5/1h": (0.10, 0.05, 3600), "TP20/SL10/4h": (0.20, 0.10, 14400), "hold 1h": (9, 0.99, 3600),
         "hold 4h": (9, 0.99, 14400)}


def trade(h, i, tp, sl, hold):
    if i + 1 >= len(h):
        return None
    e = h[i + 1]["price"] * 1.01
    end_t = h[i + 1]["t"] + hold
    if h[-1]["t"] < end_t and not any(x["price"] >= e * (1 + tp) or x["price"] <= e * (1 - sl) for x in h[i + 2:]):
        return None  # not finished within the recording
    for x in h[i + 2:]:
        if x["t"] > end_t:
            return x["price"] / e - 1 - COST
        r = x["price"] / e
        if r >= 1 + tp:
            return tp - COST
        if r <= 1 - sl:
            return r - 1 - COST
    return h[-1]["price"] / e - 1 - COST


def run(coins, rule, tp, sl, hold):
    res = []
    for h in coins.values():
        nxt = 0
        for i, r in enumerate(h):
            if r["t"] >= nxt and rule(r):
                x = trade(h, i, tp, sl, hold)
                if x is not None:
                    res.append((r["t"], x))
                nxt = r["t"] + 7200
    return res


def line(name, res):
    if len(res) < 10:
        return f"  {name:52s} n={len(res)}"
    v = [x for _, x in res]
    res = sorted(res)
    h = len(res) // 2
    a, b = [x for _, x in res[:h]], [x for _, x in res[h:]]
    return (f"  {name:52s} n={len(v):4d} win={sum(x > 0 for x in v)/len(v):.2f} mean={statistics.mean(v):+.3f} "
            f"median={statistics.median(v):+.3f} | 1st half {statistics.mean(a):+.3f}, 2nd half {statistics.mean(b):+.3f}")


def main():
    coins = load(sys.argv[1])
    n = sum(len(h) for h in coins.values())
    span = (max(h[-1]["t"] for h in coins.values()) - min(h[0]["t"] for h in coins.values())) / 3600
    print(f"{len(coins)} coins, {n} minute snapshots over {span:.1f}h")
    rnd = random.Random(5)
    for en, (tp, sl, hold) in EXITS.items():
        print(f"\n=== exit {en}")
        base = run(coins, lambda r: rnd.random() < 0.01, tp, sl, hold)
        print(line("RANDOM entries (baseline)", base))
        for name, rule in RULES.items():
            print(line(name, run(coins, rule, tp, sl, hold)))


if __name__ == "__main__":
    main()
