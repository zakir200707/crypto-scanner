"""Which sell plan suits the scanner's picks? Re-scores every best pick (score_scanner.parse) on
15-minute GeckoTerminal candles, which settle most "did the stop or the target come first" cases
that hourly candles leave open (inside one candle the stop is still assumed to come first).
Each plan is reported on the older and newer half of the picks, alerted picks and picks with
1.25x+ more buying than selling wallets separately. 3% costs, entry 2% above the logged price.

  python research/score_exits.py
"""

import json
import os
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import pick_features as pf  # noqa: E402
import score_scanner as S  # noqa: E402

CACHE = "results/scanlogs/candles15"
COST = 0.03
PLANS = {
    "now: stop -25%, half at +50%, rest +100% or back to entry": dict(sl=0.25, tp1=0.5, tp2=1.0),
    "stop -35%, half at +50%, rest +100% or back to entry": dict(sl=0.35, tp1=0.5, tp2=1.0),
    "stop -25%, all at +30%": dict(sl=0.25, tp_all=0.3),
    "stop -25%, all at +50%": dict(sl=0.25, tp_all=0.5),
    "stop -35%, all at +50%": dict(sl=0.35, tp_all=0.5),
    "no stop, sell after 24h": dict(sl=None, hold=24),
    "stop -25%, sell after 24h": dict(sl=0.25, hold=24),
}


def candles15(pair):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, pair + ".json")
    if os.path.exists(path):
        return json.load(open(path))
    d = S.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair}") or {}
    p = (d.get("pairs") or [None])[0]
    out = []
    if p:
        g = S.get(f"{S.GT}/pools/{p['pairAddress']}/ohlcv/minute?aggregate=15&limit=1000") or {}
        out = sorted(((g.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
        time.sleep(6)
    json.dump(out, open(path, "w"))
    return out


def run(c, t, entry, sl=None, tp1=None, tp2=None, tp_all=None, hold=72):
    fut = [x for x in c if t <= x[0] <= t + hold * 3600]
    if len(fut) < 4:
        return None
    got, left = 0.0, 1.0
    for ts, o, h, l, cl, v in fut:
        if sl and l <= entry * (1 - sl):
            # with half already sold, the rest goes at the entry price (hit on the way down first)
            return (got + left - 1 if left < 1 else -sl) - COST
        if tp_all and h >= entry * (1 + tp_all):
            return tp_all - COST
        if tp1 and left == 1.0 and h >= entry * (1 + tp1):
            got, left = 0.5 * (1 + tp1), 0.5
        if tp1 and left == 0.5:
            if h >= entry * (1 + tp2):
                return got + left * (1 + tp2) - 1 - COST
            if l <= entry:
                return got + left - 1 - COST
    return got + left * fut[-1][4] / entry - 1 - COST


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    picks = sorted(S.parse().values(), key=lambda p: p["t"])
    feats = {f["sym"][:12].strip(): f for f in pf.blocks().values()}
    rows = []
    for p in picks:
        c = candles15(p["pair"])
        rows.append({**p, "c": c, "bs": feats.get(p["sym"][:12].strip(), {}).get("buy_ratio", 0)})
    rows = [r for r in rows if len([x for x in r["c"] if x[0] >= r["t"]]) >= 4]
    half = len(rows) // 2
    print(f"{len(rows)} picks with 15-min candles (of {len(picks)}); halves split at pick {half}\n")
    groups = {"all best picks": lambda r: True, "alerted": lambda r: r["alerted"],
              "buyers >= 1.25x sellers": lambda r: r["bs"] >= 1.25}
    for gname, gf in groups.items():
        print(f"== {gname}")
        for pname, kw in PLANS.items():
            parts = []
            for part in (rows[:half], rows[half:]):
                v = [x for x in (run(r["c"], r["t"], r["price"] * 1.02, **kw) for r in part if gf(r)) if x is not None]
                parts.append(v)
            allv = parts[0] + parts[1]
            if not allv:
                continue
            fmt = lambda v: f"n={len(v):2d} avg={statistics.mean(v) if v else 0:+.0%}"  # noqa: E731
            print(f"  {pname:58s} ALL {fmt(allv)} win={sum(x > 0 for x in allv) / len(allv):.0%} | older {fmt(parts[0])} | newer {fmt(parts[1])}")
        print()


if __name__ == "__main__":
    main()
