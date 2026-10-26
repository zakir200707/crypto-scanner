"""Live test of the post-graduation BOOST window.

Since 2026 Pump.fun spends ~20% of migration liquidity on automatic market buys during the
first 5 minutes after a coin graduates (tokens bought are burned). This records, for every
graduation as it happens, the price of the coin's PumpSwap pool every POLL seconds for
TRACK_MIN minutes, so we can measure what a human buying 30-60s after graduation gets.

  python research/boost_test.py results/boost.json 90
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
POLL = 10
TRACK_MIN = 30


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


async def listen(coins):
    while True:
        try:
            async with websockets.connect(WS, ping_interval=30) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                async for raw in ws:
                    m = json.loads(raw)
                    mint = m.get("mint")
                    if mint and mint not in coins:
                        coins[mint] = {"mig": time.time(), "pool": m.get("pool"), "snaps": []}
                        print(time.strftime("%H:%M:%S"), "graduated", mint, flush=True)
        except Exception as e:
            print("ws reconnect:", e, file=sys.stderr, flush=True)
            await asyncio.sleep(5)


def sweep(coins):
    now = time.time()
    live = [m for m, c in coins.items() if now - c["mig"] < TRACK_MIN * 60]
    for i in range(0, len(live), 30):
        try:
            pairs = get(DEX + ",".join(live[i:i + 30]))
        except Exception as e:
            print("dex error", e, file=sys.stderr)
            continue
        for p in pairs:
            a = p["baseToken"]["address"]
            if a in coins and p.get("dexId") != "pumpfun":  # only the new pool, never the frozen curve price
                tx = (p.get("txns") or {}).get("m5") or {}
                coins[a]["snaps"].append({"t": round(now, 1), "dex": p.get("dexId"), "pair": p["pairAddress"],
                                          "price": float(p.get("priceUsd") or 0), "mc": p.get("marketCap") or 0,
                                          "liq": (p.get("liquidity") or {}).get("usd") or 0,
                                          "b5": tx.get("buys", 0), "s5": tx.get("sells", 0),
                                          "v5": (p.get("volume") or {}).get("m5") or 0})


async def main():
    out, minutes = sys.argv[1], float(sys.argv[2])
    coins = {}
    task = asyncio.create_task(listen(coins))
    end = time.time() + minutes * 60
    n = 0
    while time.time() < end:
        t = time.time()
        await asyncio.to_thread(sweep, coins)
        n += 1
        if n % 6 == 0:
            tmp = out + ".tmp"
            json.dump(coins, open(tmp, "w"))
            os.replace(tmp, out)
        await asyncio.sleep(max(1, POLL - (time.time() - t)))
    task.cancel()
    json.dump(coins, open(out, "w"))


if __name__ == "__main__":
    asyncio.run(main())
