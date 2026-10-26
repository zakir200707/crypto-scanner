"""Forward copy-trading test: follow wallets live and record what would happen if we copied them.

Polls each wallet's newest transactions (PublicNode RPC), records every Pump.fun-style
token buy and sell as soon as we see it, and snapshots each bought coin's price on
DexScreener every cycle for TRACK_H hours. research/score_copy.py turns this into returns
for "buy when they buy" with fixed exits or "sell when they sell".

  python research/fwd_copy.py research/data/gmgn_wallets.txt results/fwd_copy.json 180
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import insiders as I  # noqa: E402

I.RATE = 20
TRACK_H = 4
CYCLE = 20  # seconds
NOT_COINS = {"So11111111111111111111111111111111111111112",   # wrapped SOL
             "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
             "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT


def main():
    wfile, out, minutes = sys.argv[1], sys.argv[2], float(sys.argv[3])
    labels = {}
    for line in open(wfile):
        p = line.split()
        if p:
            labels[p[0]] = p[1:] or ["?"]
    wallets = list(labels)
    end = time.time() + minutes * 60
    last, noisy, tracked = {}, {}, {}
    with ThreadPoolExecutor(8) as ex:
        for w, s in zip(wallets, ex.map(lambda w: I.rpc("getSignaturesForAddress", [w, {"limit": 1}]), wallets)):
            last[w] = s[0]["signature"] if s else None
    print(f"following {len(wallets)} wallets", flush=True)

    def poll(w):
        p = {"limit": 25}
        if last.get(w):
            p["until"] = last[w]
        return w, I.rpc("getSignaturesForAddress", [w, p]) or []

    cycle = 0
    while time.time() < end:
        t0 = time.time()
        cycle += 1
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(poll, list(wallets)))
        new = []
        for w, sigs in res:
            if not sigs:
                continue
            last[w] = sigs[0]["signature"]
            if len(sigs) >= 25:  # 25+ transactions in one cycle: a bot, stop following
                noisy[w] = noisy.get(w, 0) + 1
                if noisy[w] >= 2 and w in wallets:
                    wallets.remove(w)
                    print("dropping bot", w[:6], flush=True)
                continue
            new += [(w, s) for s in sigs if not s.get("err")]
        seen = time.time()
        for (w, s), tx in zip(new, I.txs([s["signature"] for _, s in new], threads=8)):
            for owner, mint, d, sol in I.token_moves(tx):
                if owner != w or mint in NOT_COINS:
                    continue
                side = "buy" if d > 0 else "sell"
                tr = tracked.get(mint)
                if side == "buy" and tr is None:
                    tr = tracked[mint] = {"first_t": s["blockTime"] or seen, "events": [], "snaps": []}
                if tr is None:
                    continue  # a sell of something we never saw them buy
                tr["events"].append({"w": w, "label": labels[w][0], "side": side, "t": s["blockTime"],
                                     "seen": seen, "sol": -sol, "tokens": d})
        live = [m for m, tr in tracked.items() if time.time() - tr["first_t"] < TRACK_H * 3600]
        for i in range(0, len(live), 30):
            best = {}
            for p in I.get(I.DEX + ",".join(live[i:i + 30])) or []:
                a = p["baseToken"]["address"]
                if a in tracked and (p.get("liquidity") or {}).get("usd", 0) >= (best.get(a, {}).get("liquidity") or {}).get("usd", -1):
                    best[a] = p
            now = round(time.time())
            for a, p in best.items():
                tracked[a]["snaps"].append({"t": now, "price": float(p.get("priceUsd") or 0), "mc": p.get("marketCap") or 0,
                                            "liq": (p.get("liquidity") or {}).get("usd") or 0, "dex": p.get("dexId")})
        if cycle % 3 == 0 or time.time() >= end:
            tmp = out + ".tmp"
            json.dump({"labels": labels, "tracked": tracked}, open(tmp, "w"))
            os.replace(tmp, out)
            buys = sum(1 for tr in tracked.values() for e in tr["events"] if e["side"] == "buy")
            print(time.strftime("%H:%M:%S"), f"coins {len(tracked)}, buys {buys}, live {len(live)}, wallets {len(wallets)}", flush=True)
        time.sleep(max(1, CYCLE - (time.time() - t0)))
    json.dump({"labels": labels, "tracked": tracked}, open(out, "w"))


if __name__ == "__main__":
    main()
