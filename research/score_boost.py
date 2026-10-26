"""Score the BOOST-window test (output of boost_test.py).

Entry: first PumpSwap-pool snapshot at least DELAY seconds after graduation (alert + human
reaction), then fixed exits. 3% round-trip costs.

  python research/score_boost.py results/boost.json
"""

import json
import statistics
import sys

COST = 0.03


def path(c, delay):
    snaps = [s for s in c["snaps"] if s["price"] > 0]
    ent = [s for s in snaps if s["t"] >= c["mig"] + delay]
    if not ent or ent[0]["t"] > c["mig"] + delay + 60:
        return None, []
    return ent[0], [s for s in snaps if s["t"] > ent[0]["t"]]


def fixed(e, fut, tp, sl, hold):
    for s in fut:
        if s["t"] > e["t"] + hold:
            break
        r = s["price"] / e["price"]
        if tp and r >= 1 + tp:
            return tp
        if sl and r <= 1 - sl:
            return r - 1
    inside = [s for s in fut if s["t"] <= e["t"] + hold]
    return inside[-1]["price"] / e["price"] - 1 if inside else None


def at(e, fut, sec):
    later = [s for s in fut if s["t"] >= e["t"] + sec]
    return later[0]["price"] / e["price"] - 1 if later else None


def line(name, v):
    v = [x for x in v if x is not None]
    if not v:
        print(f"  {name:34s} n=0")
        return
    print(f"  {name:34s} n={len(v):3d} win={sum(x > 0 for x in v)/len(v):.2f} mean={statistics.mean(v):+.3f} "
          f"median={statistics.median(v):+.3f}")


def main():
    D = json.load(open(sys.argv[1]))
    print(f"{len(D)} graduations recorded")
    for delay in (30, 60, 90):
        rows = [path(c, delay) for c in D.values()]
        rows = [(e, f) for e, f in rows if e]
        print(f"\n== buy {delay}s after graduation ({len(rows)} coins)")
        for sec in (60, 120, 240, 300, 600, 1200):
            line(f"hold {sec // 60} min (gross)", [at(e, f, sec) for e, f in rows])
        line("best price within 5 min (gross)", [max([s["price"] for s in f if s["t"] <= e["t"] + 300] or [e["price"]]) / e["price"] - 1 for e, f in rows])
        line("sell at 4 min, after costs", [(lambda r: r - COST if r is not None else None)(at(e, f, 240)) for e, f in rows])
        for tp, sl, hold in [(0.2, 0.15, 300), (0.3, 0.2, 300), (0.5, 0.25, 600), (0.15, 0.1, 240)]:
            line(f"TP{tp:.0%}/SL{sl:.0%}/{hold // 60}m after costs",
                 [(lambda r: r - COST if r is not None else None)(fixed(e, f, tp, sl, hold)) for e, f in rows])


if __name__ == "__main__":
    main()
