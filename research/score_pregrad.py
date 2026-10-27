"""Score the pre-graduation test (pregrad_test.py output).

Entry: first curve snapshot at least DELAY s after the coin's market cap first crossed the
threshold. Exit: if it graduated, the first PumpSwap price at least EXIT s after graduation;
if it never graduated, the last curve price we saw (up to 60 min after entry). 3% costs.

  python research/score_pregrad.py results/pregrad.json
"""

import json
import statistics
import sys

COST = 0.03


def trade(c, thr, delay, exit_after):
    curve = [s for s in c["snaps"] if s["dex"] == "pumpfun" and s["price"] > 0 and (not c["grad"] or s["t"] < c["grad"])]
    cross = next((s for s in curve if s["mc"] >= thr), None)
    if not cross:
        return None
    ent = next((s for s in curve if s["t"] >= cross["t"] + delay), None)
    if not ent:
        return None  # graduated before we could buy on the curve
    if c["grad"]:
        post = [s for s in c["snaps"] if s["dex"] != "pumpfun" and s["price"] > 0 and s["t"] >= c["grad"] + exit_after]
        if not post:
            return None
        return post[0]["price"] / ent["price"] - 1 - COST, True
    later = [s for s in curve if ent["t"] < s["t"] <= ent["t"] + 3600]
    return (later[-1]["price"] if later else ent["price"]) / ent["price"] - 1 - COST, False


def main():
    D = json.load(open(sys.argv[1]))
    print(f"{len(D)} coins with snapshots, {sum(1 for c in D.values() if c['grad'])} graduated while watched")
    for thr in (25000, 30000, 34000, 38000):
        for delay in (10, 30, 60):
            for exit_after in (30, 90, 180):
                r = [trade(c, thr, delay, exit_after) for c in D.values()]
                r = [x for x in r if x]
                if len(r) < 5:
                    continue
                v = [x[0] for x in r]
                print(f"  MC>=${thr/1e3:.0f}K, buy {delay:2d}s later, sell {exit_after:3d}s after graduation: n={len(v):3d} "
                      f"graduated={sum(x[1] for x in r)/len(r):.0%} win={sum(x > 0 for x in v)/len(v):.2f} "
                      f"mean={statistics.mean(v):+.3f} median={statistics.median(v):+.3f}")


if __name__ == "__main__":
    main()
