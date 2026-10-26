"""Score a copy-trading forward test (output of fwd_copy.py).

Entry: first DexScreener snapshot taken at least DELAY seconds after we SAW the wallet's buy
(our real detection delay plus a human reaction delay). Exits:
  mirror  - sell at the first snapshot after we see the same wallet sell (or at the end)
  TP/SL   - fixed take-profit / stop-loss / time limit
  trail   - trailing stop from the best price since entry
3% round-trip costs. One trade per coin per group (the group's first buy of that coin).

  python research/score_copy.py results/fwd_copy.json [delay_seconds]
"""

import json
import statistics
import sys
from collections import defaultdict

COST = 0.03


def entry(tr, t_seen, delay):
    for s in tr["snaps"]:
        if s["t"] >= t_seen + delay and s["price"] > 0:
            return s if s["t"] <= t_seen + delay + 120 else None
    return None


def exit_fixed(tr, e, tp, sl, hold):
    for s in tr["snaps"]:
        if s["t"] <= e["t"] or s["price"] <= 0:
            continue
        if s["t"] > e["t"] + hold:
            break
        r = s["price"] / e["price"]
        if r >= 1 + tp:
            return tp
        if r <= 1 - sl:
            return r - 1
    fut = [s for s in tr["snaps"] if e["t"] < s["t"] <= e["t"] + hold and s["price"] > 0]
    return fut[-1]["price"] / e["price"] - 1 if fut else None


def exit_trail(tr, e, trail, hold):
    peak = e["price"]
    last = None
    for s in tr["snaps"]:
        if s["t"] <= e["t"] or s["price"] <= 0:
            continue
        if s["t"] > e["t"] + hold:
            break
        if s["price"] <= peak * (1 - trail):
            return s["price"] / e["price"] - 1
        peak = max(peak, s["price"])
        last = s
    return last["price"] / e["price"] - 1 if last else None


def exit_mirror(tr, e, w, delay, hold=4 * 3600):
    sells = [ev for ev in tr["events"] if ev["w"] == w and ev["side"] == "sell" and ev["seen"] + delay > e["t"]]
    t_exit = sells[0]["seen"] + delay if sells else e["t"] + hold
    fut = [s for s in tr["snaps"] if e["t"] < s["t"] and s["price"] > 0]
    after = [s for s in fut if s["t"] >= t_exit]
    if after:
        return after[0]["price"] / e["price"] - 1
    return fut[-1]["price"] / e["price"] - 1 if fut else None


EXITS = {
    "mirror (sell when they sell)": lambda tr, e, w, d: exit_mirror(tr, e, w, d),
    "TP30/SL20/1h": lambda tr, e, w, d: exit_fixed(tr, e, 0.3, 0.2, 3600),
    "TP50/SL30/4h": lambda tr, e, w, d: exit_fixed(tr, e, 0.5, 0.3, 4 * 3600),
    "TP100/SL40/4h": lambda tr, e, w, d: exit_fixed(tr, e, 1.0, 0.4, 4 * 3600),
    "trail30/4h": lambda tr, e, w, d: exit_trail(tr, e, 0.3, 4 * 3600),
}


def trades(D, group, delay, min_sol=0.0, mc_range=(0, 1e18)):
    out = defaultdict(list)
    for mint, tr in D["tracked"].items():
        buys = [ev for ev in tr["events"] if ev["side"] == "buy" and (group == "all" or ev["label"] == group)
                and ev["sol"] >= min_sol]
        if not buys:
            continue
        b = min(buys, key=lambda ev: ev["seen"])
        e = entry(tr, b["seen"], delay)
        if not e or not (mc_range[0] <= e["mc"] < mc_range[1]):
            continue
        for name, f in EXITS.items():
            r = f(tr, e, b["w"], delay)
            if r is not None:
                out[name].append((r - COST, b["w"], mint, e["mc"]))
    return out


def show(title, res):
    print(f"\n{title}")
    for name, rows in res.items():
        r = [x[0] for x in rows]
        if r:
            print(f"  {name:30s} coins={len(r):4d} win={sum(v > 0 for v in r)/len(r):.2f} "
                  f"mean={statistics.mean(r):+.3f} median={statistics.median(r):+.3f}")


def main():
    D = json.load(open(sys.argv[1]))
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else 30
    n_buys = sum(1 for tr in D["tracked"].values() for ev in tr["events"] if ev["side"] == "buy")
    print(f"{len(D['tracked'])} coins, {n_buys} buys recorded; entry {delay:.0f}s after we saw the buy")
    for group in ("cand", "rest", "all"):
        show(f"== {group} wallets", trades(D, group, delay))
    show("== cand, buys >= 1 SOL", trades(D, "cand", delay, min_sol=1.0))
    for lo, hi in [(0, 1e5), (1e5, 1e6), (1e6, 1e18)]:
        show(f"== all wallets, entry MC ${lo/1e3:.0f}K-{hi/1e3:.0f}K", trades(D, "all", delay, mc_range=(lo, hi)))
    # per-wallet mirror results (to see if a few wallets carry everything)
    per = defaultdict(list)
    for r, w, m, mc in trades(D, "all", delay)["mirror (sell when they sell)"]:
        per[w].append(r)
    rows = sorted(((statistics.mean(v), len(v), w) for w, v in per.items() if len(v) >= 3), reverse=True)
    print("\nper wallet (mirror, 3+ coins): best and worst")
    for row in (rows[:8] + [None] + rows[-5:]) if len(rows) > 13 else rows:
        if row is None:
            print("  ...")
            continue
        m, n, w = row
        print(f"  {w[:8]} {D['labels'].get(w, ['?'])[0]:4s} coins={n:3d} mean={m:+.3f}")


if __name__ == "__main__":
    main()
