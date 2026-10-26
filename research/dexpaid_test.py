"""Live test: do Solana coins that just paid DexScreener (profile / boost) or got a community
takeover listing go up afterwards?

Polls DexScreener's latest token profiles, boosts and community takeovers every minute. Each new
Solana coin is priced every 30s for TRACK_MIN minutes from the moment we first saw the event.

  python research/dexpaid_test.py results/dexpaid.json 150
"""

import json
import os
import sys
import time
import urllib.request

FEEDS = {"profile": "https://api.dexscreener.com/token-profiles/latest/v1",
         "boost": "https://api.dexscreener.com/token-boosts/latest/v1",
         "cto": "https://api.dexscreener.com/community-takeovers/latest/v1"}
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
TRACK_MIN = 120


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main():
    out, minutes = sys.argv[1], float(sys.argv[2])
    coins, seen0 = {}, set()
    # ignore whatever is already listed at start: only events that happen while we watch count
    for kind, url in FEEDS.items():
        for x in get(url) or []:
            seen0.add((kind, x.get("tokenAddress")))
    end, k = time.time() + minutes * 60, 0
    while time.time() < end:
        t0 = time.time()
        if k % 2 == 0:
            for kind, url in FEEDS.items():
                try:
                    for x in get(url) or []:
                        a = x.get("tokenAddress")
                        if x.get("chainId") != "solana" or not a or (kind, a) in seen0:
                            continue
                        seen0.add((kind, a))
                        if a not in coins:
                            coins[a] = {"t": time.time(), "kind": kind, "amount": x.get("amount") or x.get("totalAmount"),
                                        "snaps": []}
                            print(time.strftime("%H:%M:%S"), kind, a, flush=True)
                except Exception as e:
                    print("feed error", kind, e, file=sys.stderr, flush=True)
        live = [m for m, c in coins.items() if time.time() - c["t"] < TRACK_MIN * 60]
        for i in range(0, len(live), 30):
            try:
                pairs = get(DEX + ",".join(live[i:i + 30]))
            except Exception:
                continue
            best = {}
            for p in pairs:
                a = p["baseToken"]["address"]
                key = (p.get("dexId") != "pumpfun", (p.get("liquidity") or {}).get("usd") or 0)
                if a in coins and (a not in best or key > best[a][0]):
                    best[a] = (key, p)
            now = round(time.time())
            for a, (_, p) in best.items():
                coins[a]["snaps"].append({"t": now, "price": float(p.get("priceUsd") or 0), "mc": p.get("marketCap") or 0,
                                          "liq": (p.get("liquidity") or {}).get("usd") or 0,
                                          "age_h": (now * 1000 - (p.get("pairCreatedAt") or now * 1000)) / 3.6e6})
        k += 1
        if k % 4 == 0:
            json.dump(coins, open(out + ".tmp", "w"))
            os.replace(out + ".tmp", out)
        time.sleep(max(1, 30 - (time.time() - t0)))
    json.dump(coins, open(out, "w"))


if __name__ == "__main__":
    main()
