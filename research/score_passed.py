"""Grade every coin that passed the scanner's filters (the PASSED log lines), not only the alerted
ones, from the first time it passed, on 15-minute candles. Entry = open of the next candle + 2%.
Costs are what a small Axiom trade really pays: 0.95% a side, 1% sell slippage, and about $0.04
of priority fees and tips on the size given (default $5), so about 3.7% round trip.

  PYTHONPATH=research python research/score_passed.py [size_usd]
"""

import glob
import json
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
                first[m.group(1)] = {"mint": m.group(1), "pair": m.group(2), "tier": m.group(3), "score": float(m.group(4)),
                                     "price": float(m.group(5)), "mc": int(m.group(6)),
                                     "t": datetime.fromisoformat(l[:19] + "+00:00").timestamp()}
    return sorted(first.values(), key=lambda x: x["t"])


def stat(r):
    if not r:
        return "n=0"
    return (f"n={len(r):3d} win={sum(x > 0 for x in r) / len(r):.0%} mean={statistics.mean(r):+.1%} "
            f"median={statistics.median(r):+.1%}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    size = float(sys.argv[1]) if len(sys.argv) > 1 else 5
    cost = 0.0095 * 2 + 0.01 + 0.04 / size + 0.0 - E.COST  # run() already takes E.COST off
    rows = []
    for a in passed():
        c = E.candles15(a["pair"])
        fut = [x for x in c if x[0] >= a["t"]]
        if len(fut) < 4:
            print(f"  (no candles: {a['mint'][:8]})")
            continue
        rows.append({**a, "c": c, "entry": fut[0][1] * 1.02, "t0": fut[0][0]})
    json.dump([{k: v for k, v in r.items() if k != "c"} for r in rows], open("results/scanner_passed.json", "w"))
    print(f"{len(rows)} coins with candles; cost per trade {E.COST + cost:.1%}\n")
    half = len(rows) // 2
    for name, kw in E.PLANS.items():
        print(name)
        for gname, g in (("all", rows), ("early", [r for r in rows if r["tier"] == "early"]),
                         ("main", [r for r in rows if r["tier"] == "main"]), ("older half", rows[:half]), ("newer half", rows[half:])):
            print(f"  {gname:11s}", stat([E.run(r["c"], r["t0"], r["entry"], **kw) - cost for r in g]))


main()
