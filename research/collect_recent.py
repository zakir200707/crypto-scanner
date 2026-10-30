"""5-minute candles for this week's graduated coins that were still alive when the watcher last saw
them, in the same format as results/grads_gh, so research/backtest_gems.py can test coins hours to
days old on a second, separate period. Coin list: research/data/recent_coins.json.

  python research/collect_recent.py out/recent
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

GT = "https://api.geckoterminal.com/api/v2/networks/solana"
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def get(url):
    for i in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(20 * (i + 1))
            elif e.code == 404:
                return None
            else:
                time.sleep(5)
        except Exception:
            time.sleep(5)
    return None


coins = json.load(open("research/data/recent_coins.json"))
out = sys.argv[1]
os.makedirs(out, exist_ok=True)
mints = list(coins)
pairs = {}
for i in range(0, len(mints), 30):
    for p in get(DEX + ",".join(mints[i:i + 30])) or []:
        m = p["baseToken"]["address"]
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if m in coins and p.get("dexId") != "pumpfun" and liq >= pairs.get(m, ("", -1))[1]:
            pairs[m] = (p["pairAddress"], liq)
    time.sleep(0.3)
print(len(pairs), "pools of", len(mints))
done = 0
for m, (pair, liq) in pairs.items():
    g = get(f"{GT}/pools/{pair}/ohlcv/minute?aggregate=5&limit=1000") or {}
    k = sorted(((g.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
    json.dump({"mint": m, "grad": coins[m], "pair": pair, "liq": liq, "candles": k}, open(os.path.join(out, m + ".json"), "w"))
    done += 1
    if done % 25 == 0:
        print(done, flush=True)
    time.sleep(3)
print("saved", done)
