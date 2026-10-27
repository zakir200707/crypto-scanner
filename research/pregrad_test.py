"""Live test: buy a Pump.fun coin near the end of its bonding curve, sell into the post-graduation
BOOST buying (Pump.fun auto-buys for 5 min after graduation).

Coins come from PumpPortal (creates + migrations). Curve coins are priced on DexScreener every
FAST seconds once their market cap passes WATCH_MC (every SLOW seconds before that); after
graduation the PumpSwap pool is priced every FAST seconds for POST_MIN minutes.
research/score_pregrad.py turns this into trades with a human-sized delay.

  python research/pregrad_test.py results/pregrad.json 150
"""

import asyncio
import json
import os
import sys
import time
import urllib.request

import websockets

WS = "wss://pumpportal.fun/api/data"
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
FAST, SLOW = 10, 60
WATCH_MC = 15000
POST_MIN = 15
CURVE_MAX_H = 3


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


async def listen(coins):
    while True:
        try:
            async with websockets.connect(WS, ping_interval=30) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                async for raw in ws:
                    m = json.loads(raw)
                    mint = m.get("mint")
                    if not mint:
                        continue
                    if m.get("txType") == "create":
                        coins.setdefault(mint, {"born": time.time(), "sym": m.get("symbol"), "grad": None,
                                                "snaps": [], "last_poll": 0, "mc": 0})
                    elif m.get("txType") == "migrate" or m.get("pool") in ("pump-amm", "raydium"):
                        c = coins.setdefault(mint, {"born": None, "sym": m.get("symbol"), "grad": None,
                                                    "snaps": [], "last_poll": 0, "mc": 0})
                        c["grad"] = c["grad"] or time.time()
        except Exception as e:
            print("ws reconnect:", e, file=sys.stderr, flush=True)
            await asyncio.sleep(5)


def sweep(coins):
    now = time.time()
    due = []
    for m, c in list(coins.items()):
        if c["grad"]:
            if now - c["grad"] > POST_MIN * 60:
                continue
            gap = FAST
        else:
            if c["born"] and now - c["born"] > CURVE_MAX_H * 3600:
                continue
            gap = FAST if c["mc"] >= WATCH_MC else SLOW
        if now - c["last_poll"] >= gap - 1:
            due.append(m)
    for i in range(0, len(due), 30):
        chunk = due[i:i + 30]
        try:
            pairs = get(DEX + ",".join(chunk))
        except Exception:
            continue
        best = {}
        for p in pairs:
            a = p["baseToken"]["address"]
            key = (p.get("dexId") != "pumpfun", (p.get("liquidity") or {}).get("usd") or 0)
            if a in coins and (a not in best or key > best[a][0]):
                best[a] = (key, p)
        for a in chunk:
            coins[a]["last_poll"] = now
        for a, (_, p) in best.items():
            c = coins[a]
            mc = p.get("marketCap") or 0
            c["mc"] = mc
            if mc >= WATCH_MC or c["grad"]:
                c["snaps"].append({"t": round(now, 1), "price": float(p.get("priceUsd") or 0), "mc": mc, "dex": p.get("dexId")})


async def main():
    out, minutes = sys.argv[1], float(sys.argv[2])
    coins = {}
    task = asyncio.create_task(listen(coins))
    end, k = time.time() + minutes * 60, 0
    while time.time() < end:
        t = time.time()
        await asyncio.to_thread(sweep, coins)
        k += 1
        if k % 12 == 0:
            keep = {m: {x: c[x] for x in ("born", "sym", "grad", "snaps")} for m, c in coins.items() if c["snaps"]}
            json.dump(keep, open(out + ".tmp", "w"))
            os.replace(out + ".tmp", out)
            print(time.strftime("%H:%M:%S"), "coins", len(coins), "with snaps", len(keep),
                  "graduated", sum(1 for c in coins.values() if c["grad"]), flush=True)
        await asyncio.sleep(max(1, FAST - (time.time() - t)))
    task.cancel()
    keep = {m: {x: c[x] for x in ("born", "sym", "grad", "snaps")} for m, c in coins.items() if c["snaps"]}
    json.dump(keep, open(out, "w"))


if __name__ == "__main__":
    asyncio.run(main())
