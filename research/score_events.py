"""Score event tests (news_test.py / dexpaid_test.py output): enter at the first price DELAY
seconds after the event (or coin launch), then fixed exits; 3% costs. Groups are compared.

  python research/score_events.py results/news.json news
  python research/score_events.py results/dexpaid.json dexpaid
"""

import json
import statistics
import sys

COST = 0.03
DELAY = 60


def entry(c):
    for s in c["snaps"]:
        if s["t"] >= c["t"] + DELAY and s["price"] > 0:
            return s if s["t"] <= c["t"] + DELAY + 90 else None
    return None


def fixed(c, e, tp, sl, hold):
    fut = [s for s in c["snaps"] if e["t"] < s["t"] <= e["t"] + hold and s["price"] > 0]
    for s in fut:
        r = s["price"] / e["price"]
        if r >= 1 + tp:
            return tp
        if r <= 1 - sl:
            return r - 1
    return fut[-1]["price"] / e["price"] - 1 if fut else None


def report(name, coins):
    rows = [(c, entry(c)) for c in coins]
    rows = [(c, e) for c, e in rows if e]
    print(f"\n== {name}: {len(coins)} coins, {len(rows)} with a price {DELAY}s after the event")
    if not rows:
        return
    mx = [max([s["price"] for s in c["snaps"] if s["t"] > e["t"]] or [e["price"]]) / e["price"] for c, e in rows]
    print(f"  reached 2x after entry: {sum(m >= 2 for m in mx)/len(mx):.1%}   5x: {sum(m >= 5 for m in mx)/len(mx):.1%}   "
          f"10x: {sum(m >= 10 for m in mx)/len(mx):.1%}")
    for label, tp, sl, hold in [("TP50/SL30/30m", 0.5, 0.3, 1800), ("TP100/SL40/60m", 1.0, 0.4, 3600),
                                ("TP200/SL50/120m", 2.0, 0.5, 7200), ("hold 60m", 1e9, 1.0, 3600)]:
        r = [fixed(c, e, tp, sl, hold) for c, e in rows]
        r = [x - COST for x in r if x is not None]
        if r:
            print(f"  {label:16s} n={len(r):4d} win={sum(x > 0 for x in r)/len(r):.2f} mean={statistics.mean(r):+.3f} "
                  f"median={statistics.median(r):+.3f}")


def main():
    D = json.load(open(sys.argv[1]))
    kind = sys.argv[2]
    if kind == "news":
        coins = list(D["coins"].values())
        report("NEWS-MATCHED new coins", [c for c in coins if c["kw"]])
        report("  ...matched a Google Trends search", [c for c in coins if c["kw"] and (c["kw_src"] or "").startswith("trends")])
        report("BASELINE: random sample of other new coins", [c for c in coins if not c["kw"]])
        top = sorted((c for c in coins if c["kw"]), key=lambda c: -max([s["mc"] for s in c["snaps"]] or [0]))[:8]
        print("\n  best news coins:", ", ".join(f"{c['sym']}<-{c['kw']} (max MC ${max([s['mc'] for s in c['snaps']] or [0])/1e3:.0f}K)" for c in top))
    else:
        coins = list(D.values())
        for k in ("profile", "boost", "cto"):
            report(f"DexScreener {k}", [c for c in coins if c["kind"] == k])


if __name__ == "__main__":
    main()
