"""How copyable is a wallet? Reads each wallet's recent transactions (PublicNode RPC) and reports,
per coin it traded: when it first bought, how much SOL, the market cap it bought at, when it first
sold, and what it got back. A wallet that sells most coins within a minute cannot be copied by a
person tapping a phone alert.

  python research/kol_profile.py research/data/axiom_kols.txt results/kol_profile.json [sigs_per_wallet]
"""

import json
import os
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import insiders as I  # noqa: E402

I.RATE = 20
SKIP = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def profile(w, n, max_pages=20):
    # busy traders fire many failed transactions (Cented: 50 in one second), so page back until
    # we have n successful ones
    sigs, before, failed = [], None, 0
    for _ in range(max_pages):
        p = {"limit": 1000, **({"before": before} if before else {})}
        page = I.rpc("getSignaturesForAddress", [w, p]) or []
        if not page:
            break
        ok = [s for s in page if not s.get("err")]
        failed += len(page) - len(ok)
        sigs += ok
        before = page[-1]["signature"]
        if len(sigs) >= n or len(page) < 1000:
            break
    sigs = sigs[:n]
    print(f"  {w[:6]}: {len(sigs)} ok tx, {failed} failed tx skipped", flush=True)
    coins = defaultdict(lambda: {"buys": [], "sells": []})
    for s, tx in zip(sigs, I.txs([s["signature"] for s in sigs])):
        for owner, mint, d, sol in I.token_moves(tx):
            if owner != w or mint in SKIP or not tx:
                continue
            # sol is the wallet's SOL change (wrapped-SOL routes show 0; those trades are skipped below)
            (coins[mint]["buys"] if d > 0 else coins[mint]["sells"]).append(
                {"t": s["blockTime"], "tokens": abs(d), "sol": abs(sol)})
    span = (sigs[0]["blockTime"] - sigs[-1]["blockTime"]) / 3600 if len(sigs) > 1 else 0
    return {"wallet": w, "n_tx": len(sigs), "span_h": span, "coins": coins}


def summary(label, p, sol_usd):
    rows = []
    for mint, c in p["coins"].items():
        b = [x for x in c["buys"] if x["sol"] > 0.001]
        if not b:
            continue  # sells of coins bought before our window, or wrapped-SOL routes
        t0 = min(x["t"] for x in b)
        sol_in = sum(x["sol"] for x in b)
        tok_in = sum(x["tokens"] for x in b)
        first = min(b, key=lambda x: x["t"])
        mc = first["sol"] / first["tokens"] * 1e9 * sol_usd if first["tokens"] else 0  # Pump.fun 1B supply
        s = [x for x in c["sells"] if x["t"] >= t0]
        first_sell = min((x["t"] for x in s), default=None)
        sol_out = sum(x["sol"] for x in s)
        tok_out = sum(x["tokens"] for x in s)
        rows.append({"mint": mint, "t": t0, "sol_in": sol_in, "mc_usd": mc,
                     "hold_s": first_sell - t0 if first_sell else None,
                     "closed": tok_out >= 0.95 * tok_in, "ret": sol_out / sol_in - 1 if tok_out >= 0.95 * tok_in else None})
    holds = [r["hold_s"] for r in rows if r["hold_s"] is not None]
    closed = [r["ret"] for r in rows if r["ret"] is not None]
    pct = lambda xs, f: sum(f(x) for x in xs) / len(xs) if xs else 0  # noqa: E731
    out = {
        "label": label, "wallet": p["wallet"], "tx": p["n_tx"], "span_h": round(p["span_h"], 1),
        "coins": len(rows), "coins_per_h": round(len(rows) / p["span_h"], 1) if p["span_h"] else None,
        "median_buy_sol": round(statistics.median([r["sol_in"] for r in rows]), 2) if rows else None,
        "median_entry_mc": round(statistics.median([r["mc_usd"] for r in rows])) if rows else None,
        "median_first_sell_s": statistics.median(holds) if holds else None,
        "sold_within_60s": round(pct(holds, lambda h: h <= 60), 2),
        "sold_within_5m": round(pct(holds, lambda h: h <= 300), 2),
        "closed_coins": len(closed), "win_rate": round(pct(closed, lambda r: r > 0), 2),
        "median_ret": round(statistics.median(closed), 3) if closed else None,
        "mean_ret": round(statistics.mean(closed), 3) if closed else None,
        "rows": rows,
    }
    return out


def main():
    wfile, out_path = sys.argv[1], sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 500
    sol_usd = float(I.get("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT")["price"])
    labels = {l.split()[0]: l.split()[1] for l in open(wfile) if l.strip()}
    res = []
    for w, label in labels.items():
        s = summary(label, profile(w, n), sol_usd)
        res.append(s)
        print(f"{label:10s} {s['coins']:4d} coins in {s['span_h']:5.1f}h ({s['coins_per_h']}/h) | buy {s['median_buy_sol']} SOL "
              f"at MC ${s['median_entry_mc']} | first sell median {s['median_first_sell_s']}s, <60s {s['sold_within_60s']:.0%}, "
              f"<5m {s['sold_within_5m']:.0%} | closed {s['closed_coins']} win {s['win_rate']:.0%} "
              f"median {s['median_ret']} mean {s['mean_ret']}", flush=True)
    json.dump(res, open(out_path, "w"))


if __name__ == "__main__":
    main()
