"""Backtest entry rules on graduated Pump.fun coins (5-minute candles from collect_grads.py).

Realistic for a human trading from phone alerts:
  - signal is known at the close of a 5-minute candle
  - entry = average of the NEXT candle's open and high (assumes we chase a bit), or the
    last close if nobody traded in the next 10 minutes
  - exits: take-profit / stop-loss / time limit, or a trailing stop; when target and stop
    fall inside the same candle the stop is assumed to come first
  - 3% round-trip costs; one trade per coin per rule (the first signal)
Coins are split by graduation time: rules are ranked on the older half (train) and
judged on the newer half (test). Every rule is compared with random entries on the
same coins.

  python research/backtest_grads.py
"""

import glob
import json
import os
import random
import sys
from collections import defaultdict

import numpy as np

COST = 0.03
SUPPLY = 1e9  # Pump.fun coins all have a 1B supply, so MC = price * 1e9
EXITS = {
    "TP30/SL20/4h": ("fixed", 0.3, 0.2, 4),
    "TP50/SL30/12h": ("fixed", 0.5, 0.3, 12),
    "TP100/SL40/24h": ("fixed", 1.0, 0.4, 24),
    "TP200/SL50/48h": ("fixed", 2.0, 0.5, 48),
    "trail30/48h": ("trail", None, 0.3, 48),
    "trail50/72h": ("trail", None, 0.5, 72),
    "hold72h (no stop)": ("fixed", 1e9, 1.0, 72),               # "spray and pray": let rare runners pay
    "ladder: 1/2 at 2x, 1/2 at 10x / 72h": ("ladder", None, None, 72),
}


def load(path="results/grads"):
    coins = []
    for f in glob.glob(os.path.join(path, "*.json")):
        if f.endswith("_index.json"):
            continue
        d = json.load(open(f))
        c = np.array(d["candles"], dtype=float)
        if len(c) < 3:
            coins.append({"mint": d["mint"], "grad": d["grad"], "n": len(c)})
            continue
        t, o, h, l, cl, v = c.T
        keep = t >= d["grad"] - 300
        t, o, h, l, cl, v = t[keep], o[keep], h[keep], l[keep], cl[keep], v[keep]
        # a 5-minute wick more than 5x beyond the candle body is bad data or a sandwich, not a fill we'd get
        h = np.minimum(h, np.maximum(o, cl) * 5)
        l = np.maximum(l, np.minimum(o, cl) / 5)
        if len(t) < 3:
            coins.append({"mint": d["mint"], "grad": d["grad"], "n": len(t)})
            continue
        coins.append(features({"mint": d["mint"], "grad": d["grad"], "t": t, "o": o, "h": h, "l": l, "c": cl, "v": v}))
    coins.sort(key=lambda x: x["grad"])
    return coins


def window_idx(t, span):
    """For each i, index of the first candle with t > t[i] - span."""
    return np.searchsorted(t, t - span, side="right")


def features(x):
    t, h, l, c, v = x["t"], x["h"], x["l"], x["c"], x["v"]
    n = len(t)
    x["n"] = n
    x["age"] = (t + 300 - x["grad"]) / 3600
    x["mc"] = c * SUPPLY
    x["hi_all"] = np.maximum.accumulate(h)                       # high since graduation (incl. this candle)
    x["hi_prev"] = np.concatenate([[h[0]], x["hi_all"][:-1]])     # high before this candle
    cv = np.concatenate([[0], np.cumsum(v)])
    w1, w4, w7 = window_idx(t, 3600), window_idx(t, 4 * 3600), window_idx(t, 7 * 3600)
    idx = np.arange(n)
    x["vol1h"] = cv[idx + 1] - cv[w1]
    prior = cv[w1] - cv[w7]                                       # volume 1h..7h ago
    x["volratio"] = np.where(prior > 0, x["vol1h"] / np.maximum(prior / 6, 1e-9), 0)
    # close 1h ago (last candle at or before t-1h); if none, first open
    j = np.searchsorted(t, t - 3600, side="right") - 1
    ref = np.where(j >= 0, c[np.maximum(j, 0)], x["o"][0])
    x["r1h"] = c / ref - 1
    # 4h high before this candle
    hi4 = np.empty(n)
    for i in range(n):
        a = w4[i]
        hi4[i] = h[a:i].max() if i > a else h[i]
    x["hi4_prev"] = hi4
    x["dd"] = c / x["hi_all"] - 1
    return x


def entry_price(x, i):
    if i + 1 < x["n"] and x["t"][i + 1] <= x["t"][i] + 600:
        return (x["o"][i + 1] + x["h"][i + 1]) / 2, i + 1
    return x["c"][i], i + 1


def simulate(x, i, exit_spec):
    kind, tp, sl, hold_h = exit_spec
    e, j = entry_price(x, i)
    if e <= 0:
        return None
    t_end = x["t"][i] + hold_h * 3600
    k_end = np.searchsorted(x["t"], t_end, side="right")
    if j >= x["n"] or j >= k_end:
        return x["c"][min(x["n"], k_end) - 1] / e - 1 - COST
    o, h, l, c = x["o"][j:k_end], x["h"][j:k_end], x["l"][j:k_end], x["c"][j:k_end]
    if kind == "fixed":
        hit_sl = np.nonzero(l <= e * (1 - sl))[0]
        hit_tp = np.nonzero(h >= e * (1 + tp))[0]
        a = hit_sl[0] if len(hit_sl) else 10 ** 9
        b = hit_tp[0] if len(hit_tp) else 10 ** 9
        if a == b == 10 ** 9:
            return c[-1] / e - 1 - COST
        if a <= b:  # stop first (also when both in the same candle)
            return min(o[a], e * (1 - sl)) / e - 1 - COST
        return tp - COST  # take-profit order fills at the target (gaps above it are not counted)
    if kind == "ladder":  # sell half at 2x, the other half at 10x, the rest at the time limit
        got, left = 0.0, 1.0
        for target in (2.0, 10.0):
            hit = np.nonzero(h >= e * target)[0]
            if len(hit):
                got += 0.5 * target
                left -= 0.5
            else:
                break
        return got + left * c[-1] / e - 1 - COST
    # trailing stop: stop level = (max high seen BEFORE this candle, starting at entry) * (1 - sl)
    peak = np.maximum.accumulate(np.concatenate([[e], h[:-1]]))
    peak = np.maximum(peak, e)
    stop = peak * (1 - sl)
    hit = np.nonzero(l <= stop)[0]
    if len(hit):
        k = hit[0]
        return min(o[k], stop[k]) / e - 1 - COST
    return c[-1] / e - 1 - COST


# ---------------------------------------------------------------- entry rules

MIN_ENTRY_MC = 1e4  # below this the pool is dead or the price data is garbage (fake 1000x moves)


def first(mask, x, min_age=0.0):
    ok = mask & (x["age"] >= min_age) & (x["mc"] >= MIN_ENTRY_MC)
    nz = np.nonzero(ok)[0]
    return nz[0] if len(nz) else None


def build_rules():
    R = {}
    for A in (0.05, 0.5, 2, 6):  # "spray and pray" entries: every coin, at a fixed age after graduation
        R[f"every coin at age>={A}h"] = (lambda x, A=A: first(np.ones(x["n"], bool), x, A))
    for A in (1, 3, 6, 12, 24):
        for M in (5e4, 1e5, 2e5, 5e5, 1e6):
            R[f"survivor age>={A}h mc>={M/1e3:.0f}K"] = (lambda x, A=A, M=M: first(x["mc"] >= M, x, A))
    for A in (0.5, 2, 6):
        for M in (1e5, 3e5, 1e6):
            for D in (0.3, 0.5, 0.7):
                for conf in (False, True):
                    def f(x, A=A, M=M, D=D, conf=conf):
                        m = (x["hi_all"] * SUPPLY >= M) & (x["dd"] <= -D)
                        if conf:
                            m &= np.concatenate([[False], x["c"][1:] > x["c"][:-1]])
                        return first(m, x, A)
                    R[f"dip age>={A}h peak>={M/1e3:.0f}K dd>={D:.0%}{' +green' if conf else ''}"] = f
    for A in (1, 3, 6, 12):
        for V in (1, 2, 4):
            for M in (5e4, 1e5, 3e5):
                R[f"4h-breakout age>={A}h vol>={V}x mc>={M/1e3:.0f}K"] = (
                    lambda x, A=A, V=V, M=M: first((x["c"] > x["hi4_prev"]) & (x["volratio"] >= V) & (x["mc"] >= M), x, A))
    for A in (1, 6, 24):
        for V in (2, 3, 5):
            for M in (5e4, 1e5, 3e5):
                R[f"quiet-accum age>={A}h vol>={V}x |r1h|<10% mc>={M/1e3:.0f}K"] = (
                    lambda x, A=A, V=V, M=M: first((x["volratio"] >= V) & (np.abs(x["r1h"]) < 0.1) & (x["mc"] >= M), x, A))
    for A in (2, 6, 12, 24):
        for M in (5e4, 1e5, 3e5):
            R[f"new-ATH age>={A}h mc>={M/1e3:.0f}K"] = (
                lambda x, A=A, M=M: first((x["c"] > x["hi_prev"]) & (x["mc"] >= M), x, A))
    for A in (0.5, 3, 12):
        for Rr in (0.2, 0.5, 1.0):
            for M in (5e4, 1e5, 3e5):
                R[f"momentum age>={A}h r1h>={Rr:.0%} mc>={M/1e3:.0f}K"] = (
                    lambda x, A=A, Rr=Rr, M=M: first((x["r1h"] >= Rr) & (x["mc"] >= M), x, A))
    return R


def random_baseline(coins, exit_spec, reps=3, seed=7):
    rnd = random.Random(seed)
    res = []
    for _ in range(reps):
        for x in coins:
            if x["n"] < 3:
                continue
            cand = np.nonzero((x["age"] >= 0.5) & (x["age"] <= 48) & (x["mc"] >= MIN_ENTRY_MC))[0]
            if len(cand):
                r = simulate(x, int(rnd.choice(cand)), exit_spec)
                if r is not None:
                    res.append(r)
    return np.array(res)


def evaluate(coins, rules):
    out = {}
    for name, rule in rules.items():
        idx = [(x, rule(x)) for x in coins if x["n"] >= 3]
        idx = [(x, i) for x, i in idx if i is not None]
        for en, spec in EXITS.items():
            r = [simulate(x, i, spec) for x, i in idx]
            out[(name, en)] = np.array([v for v in r if v is not None])
    return out


def stats(a):
    if len(a) == 0:
        return "n=0"
    return f"n={len(a):4d} win={np.mean(a > 0):.2f} mean={a.mean():+.3f} median={np.median(a):+.3f}"


def boot_ci(a, reps=2000, seed=3):
    if len(a) < 5:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    m = [rng.choice(a, len(a)).mean() for _ in range(reps)]
    return np.percentile(m, 2.5), np.percentile(m, 97.5)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    coins = load(sys.argv[1] if len(sys.argv) > 1 else "results/grads")
    live = [x for x in coins if x["n"] >= 3]
    print(f"{len(coins)} coins, {len(live)} with 3+ candles after graduation")
    half = len(coins) // 2
    train, test = coins[:half], coins[half:]
    rules = build_rules()
    print(f"{len(rules)} entry rules x {len(EXITS)} exits")
    print("\nRANDOM-ENTRY BASELINE (same coins, same exits)")
    for en, spec in EXITS.items():
        print(f"  {en:16s} train {stats(random_baseline(train, spec))} | test {stats(random_baseline(test, spec))}")
    tr = evaluate(train, rules)
    spray = {k: v for k, v in rules.items() if k.startswith("every coin")}
    te_s = evaluate(test, spray)
    print("\nSPRAY AND PRAY (buy every coin, tiny size) - heavy tails, so look at the CI")
    for k in sorted(te_s):
        if k[1] in ("hold72h (no stop)", "ladder: 1/2 at 2x, 1/2 at 10x / 72h", "trail50/72h"):
            a, b = tr[k], te_s[k]
            lo, hi = boot_ci(b)
            print(f"  {k[0]:22s} {k[1]:36s} TRAIN {stats(a)} 10x+={np.mean(a >= 9):.3f}\n"
                  f"  {'':59s} TEST  {stats(b)} 10x+={np.mean(b >= 9):.3f} 95%CI [{lo:+.3f},{hi:+.3f}]")
    ranked = sorted((k for k in tr if len(tr[k]) >= 40), key=lambda k: -tr[k].mean())
    print("\nTOP 25 ON TRAIN (n>=40) -> SAME RULE ON TEST")
    te = evaluate(test, {k[0]: rules[k[0]] for k in ranked[:25]})
    for k in ranked[:25]:
        a = te.get(k, np.array([]))
        lo, hi = boot_ci(a)
        print(f"  {k[0][:52]:52s} {k[1]:15s} TRAIN {stats(tr[k])}\n  {'':68s} TEST  {stats(a)}  95%CI [{lo:+.3f},{hi:+.3f}]")
    json.dump({f"{k[0]} | {k[1]}": [float(tr[k].mean()), len(tr[k])] for k in tr if len(tr[k])},
              open("results/backtest_train.json", "w"), indent=0)


if __name__ == "__main__":
    main()
