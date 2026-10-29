"""Score the scanner's real phone alerts from the moment they were sent, on 15-minute candles,
and compare sell plans. score_scanner.py used the first time a coin was the best pick (often
hours before the alert and at a different price); this uses the "*** NEW ... PICK" line's time.

Entry = open of the first 15-min candle after the alert, plus 2%. 3% costs.
Also prints the live sell signals the scanner sent, as a check against reality.

  python research/score_alerts_live.py
"""

import glob
import re
import statistics
import sys
from datetime import datetime

import score_exits as E


def alerts():
    out = []
    for f in sorted(glob.glob("results/scanlogs/*.txt")):
        lines = open(f, encoding="utf-8", errors="ignore").read().splitlines()
        for i, l in enumerate(lines):
            m = re.search(r"\*\*\* NEW (\w+) PICK: (.*) \*\*\*", l)
            if not m:
                continue
            sym = m.group(2).strip()
            t = datetime.fromisoformat(l[:19] + "+00:00").timestamp()
            # the matching BEST PICK / EARLY block printed earlier in the same run
            blk = None
            for j in range(i, -1, -1):
                if re.search(r"(BEST PICK|EARLY GEM|EARLY PICK).*\(" + re.escape(sym) + r"\)", lines[j]):
                    blk = "\n".join(lines[j:j + 8])
                    break
            if not blk:
                continue
            pair = re.search(r"dexscreener\.com/solana/(\w+)", blk)
            bs = re.search(r"(\d+) buyers vs (\d+) sellers", blk)
            if pair:
                out.append({"sym": sym, "tier": m.group(1).lower(), "t": t, "pair": pair.group(1),
                            "bs": int(bs.group(1)) / max(1, int(bs.group(2))) if bs else 0})
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    al = alerts()
    rows = []
    for a in al:
        c = E.candles15(a["pair"])
        fut = [x for x in c if x[0] >= a["t"]]
        if len(fut) < 4:
            print(f"  (no candles for {a['sym']})")
            continue
        rows.append({**a, "c": c, "entry": fut[0][1] * 1.02, "t0": fut[0][0]})
    print(f"{len(rows)} alerts with 15-min candles (of {len(al)})\n")
    half = len(rows) // 2
    for gname, gf in (("all alerts", lambda r: True), ("alerts with buyers >= 1.25x sellers", lambda r: r["bs"] >= 1.25),
                      ("alerts with buyers < 1.25x sellers", lambda r: r["bs"] < 1.25)):
        print(f"== {gname}")
        for pname, kw in E.PLANS.items():
            parts = [[x for x in (E.run(r["c"], r["t0"], r["entry"], **kw) for r in part if gf(r)) if x is not None]
                     for part in (rows[:half], rows[half:])]
            v = parts[0] + parts[1]
            if v:
                f = lambda v: f"n={len(v):2d} avg={statistics.mean(v) if v else 0:+.0%}"  # noqa: E731
                print(f"  {pname:58s} ALL {f(v)} win={sum(x > 0 for x in v) / len(v):.0%} | older {f(parts[0])} | newer {f(parts[1])}")
        print()
    print("per alert (current plan):")
    for r in rows:
        x = E.run(r["c"], r["t0"], r["entry"], **E.PLANS["now: stop -25%, half at +50%, rest +100% or back to entry"])
        fut = [c for c in r["c"] if r["t0"] <= c[0] <= r["t0"] + 24 * 3600]
        print(f"  {datetime.utcfromtimestamp(r['t']):%d %H:%M} {r['sym'][:12]:12s} {r['tier']:5s} b/s {r['bs']:.2f} -> {x:+.0%}  "
              f"24h high x{max(c[2] for c in fut) / r['entry']:.2f} low x{min(c[3] for c in fut) / r['entry']:.2f}")


if __name__ == "__main__":
    main()
