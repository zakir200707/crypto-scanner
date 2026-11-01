"""Are the scanner's picks losing because of fees, or because of the coins? Grades every coin that
passed the filters, from the first time it passed, on 15-minute candles, at several cost levels.
Also counts how many were real gems (2x inside 24h) and how many of those hit a -25% stop first.

Costs per round trip on a trade of size_usd, on top of the candle prices:
  perfect: no fees and a fill exactly at the candle open, which nobody gets
  no fees: no fees, entry 2% above the open as in the other scores
  jupiter: 0.1% a side, 1% sell slippage, $0.04 of network fees
  axiom:   0.95% a side, 1% sell slippage, $0.04 of network fees

  PYTHONPATH=research python research/score_fees.py [size_usd]
"""

import glob
import re
import statistics
import sys
from datetime import datetime

import score_exits as E


def passed():
    first = {}
    for f in sorted(glob.glob("results/scanlogs/*.txt")):
        for l in open(f, encoding="utf-8", errors="ignore"):
            m = re.search(r"PASSED (\w+) (\w+) (\w+) score=([\d.]+) price=(\S+) mc=(\d+)", l)
            if m and m.group(1) not in first:
                first[m.group(1)] = {"mint": m.group(1), "pair": m.group(2), "tier": m.group(3),
                                     "t": datetime.fromisoformat(l[:19] + "+00:00").timestamp()}
    return sorted(first.values(), key=lambda x: x["t"])


def stat(r):
    if not r:
        return "n=0"
    return f"n={len(r):3d} win={sum(x > 0 for x in r) / len(r):3.0%} mean={statistics.mean(r):+6.1%} median={statistics.median(r):+6.1%}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    size = float(sys.argv[1]) if len(sys.argv) > 1 else 5
    costs = {"perfect": (0.0, 0.0), "no fees": (0.02, 0.0), "jupiter": (0.02, 0.002 + 0.01 + 0.04 / size),
             "axiom": (0.02, 0.019 + 0.01 + 0.04 / size)}
    rows = []
    for a in passed():
        c = E.candles15(a["pair"])
        fut = [x for x in c if x[0] >= a["t"]]
        if len(fut) >= 4:
            rows.append({**a, "c": c, "t0": fut[0][0], "open": fut[0][1]})
    half = len(rows) // 2
    print(f"{len(rows)} coins with candles, trade size ${size:g}\n")
    for plan, kw in E.PLANS.items():
        print(plan)
        for name, (slip, cost) in costs.items():
            f = lambda g: [x + E.COST - cost for x in (E.run(r["c"], r["t0"], r["open"] * (1 + slip), **kw) for r in g) if x is not None]  # noqa: E731
            print(f"  {name:8s} {stat(f(rows))} | older {statistics.mean(f(rows[:half]) or [0]):+6.1%} | newer {statistics.mean(f(rows[half:]) or [0]):+6.1%}")
    gems, clean, dead = 0, 0, 0
    for r in rows:
        e = r["open"] * 1.02
        fut = [x for x in r["c"] if r["t0"] <= x[0] <= r["t0"] + 24 * 3600]
        hit = next((i for i, x in enumerate(fut) if x[2] >= e * 2), None)
        stop = next((i for i, x in enumerate(fut) if x[3] <= e * 0.75), None)
        gems += hit is not None
        clean += hit is not None and (stop is None or hit < stop)
        dead += min(x[3] for x in fut) <= e * 0.5
    print(f"\nreached 2x in 24h: {gems} of {len(rows)} ({gems / len(rows):.0%}); before a -25% stop: {clean} ({clean / len(rows):.0%})")
    print(f"fell 50% or more in 24h: {dead} ({dead / len(rows):.0%})")


if __name__ == "__main__":
    main()
