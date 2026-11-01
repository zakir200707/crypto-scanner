"""Did the "too late" filters added on 26 Oct (commit 870205a) remove the winners?

Part 1: every real alert, split at that commit, with its 1h and 2h move at alert time from candles.
Would the 1h filter have blocked the early winners?
Part 2: coins rejected only by the deeper checks since then, found by symbol, graded from the first
rejection like score_fees.py (the 1h filter's own rejects are not logged, so they cannot be graded).

Entry = open of the next 15-min candle + 2%. Costs = Jupiter on $5 (2%).

  PYTHONPATH=research python research/score_late.py
"""

import glob
import json
import re
import statistics
import sys
import time
from datetime import datetime
from urllib.parse import quote

import score_alerts_live as A
import score_exits as E
import score_scanner as S

CUT = datetime.fromisoformat("2026-10-26T17:58:09+00:00").timestamp()
COST = 0.02
PLANS = {"stop -25%, all at +50%": dict(sl=0.25, tp_all=0.5), "current plan": dict(sl=0.25, tp1=0.5, tp2=1.0),
         "stop -35%, all at +50%": dict(sl=0.35, tp_all=0.5)}
GROUPS = {"whales net sold": r"whales net sold", "pump in progress": r"pump in progress",
          "near 24h high": r"at/near 24h high", "already pumped": r"already pumped"}
PER_GROUP = 45


def graded(rows):
    """One line per plan, and how many reached 2x and fell 50% within 24h."""
    if not rows:
        print("  n=0")
        return
    half = len(rows) // 2
    for name, kw in PLANS.items():
        f = lambda g: [x + E.COST - COST for x in (E.run(r["c"], r["t0"], r["entry"], **kw) for r in g) if x is not None]  # noqa: E731
        v = f(rows)
        print(f"  {name:24s} n={len(v):3d} win={sum(x > 0 for x in v) / len(v):3.0%} mean={statistics.mean(v):+6.1%}"
              f" | older {statistics.mean(f(rows[:half]) or [0]):+6.1%} | newer {statistics.mean(f(rows[half:]) or [0]):+6.1%}")
    hi, lo = [], []
    for r in rows:
        fut = [x for x in r["c"] if r["t0"] <= x[0] <= r["t0"] + 24 * 3600]
        hi.append(max(x[2] for x in fut) / r["entry"])
        lo.append(min(x[3] for x in fut) / r["entry"])
    print(f"  24h: 1.5x {sum(h >= 1.5 for h in hi)}, 2x {sum(h >= 2 for h in hi)}, fell 50% {sum(l <= 0.5 for l in lo)} of {len(rows)}"
          f"; median high x{statistics.median(hi):.2f}")


def move(c, t, hours):
    past = [x for x in c if t - hours * 3600 <= x[0] < t]
    return past[-1][4] / past[0][1] - 1 if past and past[0][1] else None


def alerts():
    rows = []
    for a in A.alerts():
        c = E.candles15(a["pair"])
        fut = [x for x in c if x[0] >= a["t"]]
        if len(fut) >= 4:
            rows.append({**a, "c": c, "t0": fut[0][0], "entry": fut[0][1] * 1.02,
                         "h1": move(c, a["t"], 1), "h2": move(c, a["t"], 2)})
    return rows


def rejected():
    """{group: {symbol: first time}} since the commit, leaving out coins that were ever alerted or passed."""
    out = {g: {} for g in GROUPS}
    seen = set()
    for f in sorted(glob.glob("results/scanlogs/*.txt")):
        for l in open(f, encoding="utf-8", errors="ignore"):
            m = re.search(r"\*\*\* NEW \w+ PICK: (.*) \*\*\*", l)
            if m:
                seen.add(m.group(1).strip()[:12])
            m = re.match(r"(\S{19})\S* +- (.{12}) (.+)$", l.rstrip())
            if not m:
                continue
            t = datetime.fromisoformat(m.group(1) + "+00:00").timestamp()
            for g, pat in GROUPS.items():
                if t >= CUT and re.search(pat, m.group(3)):
                    out[g].setdefault(m.group(2).strip(), t)
    return {g: {s: t for s, t in v.items() if s not in seen} for g, v in out.items()}


def find_pair(sym):
    d = S.get("https://api.dexscreener.com/latest/dex/search?q=" + quote(sym)) or {}
    ps = [p for p in d.get("pairs") or [] if p.get("chainId") == "solana"
          and (p.get("baseToken", {}).get("symbol") or "").lower()[:12] == sym.lower()]
    ps.sort(key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0))
    return ps[0]["pairAddress"] if ps else None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    rows = alerts()
    before, after = [r for r in rows if r["t"] < CUT], [r for r in rows if r["t"] >= CUT]
    for name, g in (("alerts before 26 Oct 17:58", before), ("alerts after", after)):
        print(f"== {name}")
        graded(g)
    print("\n== alerts before the commit that the 1h filter (over +25%) would have blocked")
    graded([r for r in before if (r["h1"] or 0) > 0.25])
    print("== alerts before the commit that it would have kept")
    graded([r for r in before if (r["h1"] or 0) <= 0.25])
    print("\nper alert before the commit: 1h move, 2h move, 24h high")
    for r in before:
        fut = [x for x in r["c"] if r["t0"] <= x[0] <= r["t0"] + 24 * 3600]
        print(f"  {r['sym'][:12]:12s} {r['tier']:5s} 1h {(r['h1'] or 0):+5.0%} 2h {(r['h2'] or 0):+5.0%} high x{max(x[2] for x in fut) / r['entry']:.2f}")

    print("\nrejected coins since the commit, found by symbol")
    done = {}
    for g, coins in rejected().items():
        newest = sorted(coins.items(), key=lambda kv: -kv[1])[:PER_GROUP]
        rs = []
        for sym, t in sorted(newest, key=lambda kv: kv[1]):
            if sym not in done:
                done[sym] = find_pair(sym)
                time.sleep(0.4)
            if not done[sym]:
                continue
            c = E.candles15(done[sym])
            fut = [x for x in c if x[0] >= t]
            if len(fut) >= 4:
                rs.append({"sym": sym, "c": c, "t0": fut[0][0], "entry": fut[0][1] * 1.02})
        print(f"== {g}: {len(coins)} coins, graded {len(rs)}")
        graded(rs)
        json.dump([r["sym"] for r in rs], open(f"results/late_{g.replace(' ', '_').replace('/', '')}.json", "w"))


if __name__ == "__main__":
    main()
