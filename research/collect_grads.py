"""Build an unbiased dataset of graduated Pump.fun coins with 5-minute price history.

Every coin that graduated in the window (read from Pump.fun's migration wallet, so no
survivor bias), its PumpSwap pool (DexScreener), and up to 1000 five-minute candles from
graduation onward (GeckoTerminal). Resumable: coins already saved are skipped.

  python research/collect_grads.py --days 4 --skip-recent-h 8 --max 1600
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import insiders  # noqa: E402  (graduation reader + PublicNode RPC client)

GT = "https://api.geckoterminal.com/api/v2/networks/solana"
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
OUT = "results/grads"
GT_GAP = 2.2  # free tier allows ~30 calls/min


def get(url, tries=5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(20 * (i + 1))
                continue
            if e.code == 404:
                return None
        except Exception:
            time.sleep(3)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=4)
    ap.add_argument("--skip-recent-h", type=float, default=8, help="leave out coins graduated too recently to judge")
    ap.add_argument("--max", type=int, default=1600)
    ap.add_argument("--index", help="use this prepared index instead of building one")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    out = a.out
    os.makedirs(out, exist_ok=True)
    now = time.time()
    index_path = a.index or os.path.join(out, "_index.json")
    if os.path.exists(index_path):
        index = json.load(open(index_path))
    else:
        grads = insiders.graduations(now - a.days * 86400, now - a.skip_recent_h * 3600)
        print(f"{len(grads)} graduations found", flush=True)
        index = {m: {"grad": t} for m, t in grads.items()}
        mints = list(index)
        for i in range(0, len(mints), 30):
            for p in get(DEX + ",".join(mints[i:i + 30])) or []:
                m = p["baseToken"]["address"]
                if m in index and p.get("dexId") != "pumpfun":
                    liq = (p.get("liquidity") or {}).get("usd") or 0
                    if liq >= index[m].get("liq", -1):
                        index[m].update(pair=p["pairAddress"], dex=p.get("dexId"), liq=liq,
                                        mc_now=p.get("marketCap") or 0, sym=p["baseToken"].get("symbol"))
            time.sleep(0.25)
        json.dump(index, open(index_path, "w"))
    todo = [m for m, v in index.items() if v.get("pair") and not os.path.exists(os.path.join(out, m + ".json"))]
    random.seed(1)
    random.shuffle(todo)
    done = len(index) - len(todo)
    todo = todo[:max(0, a.max - done)]
    print(f"{len(index)} coins indexed, {sum(1 for v in index.values() if v.get('pair'))} with a pool; fetching {len(todo)}", flush=True)
    for i, m in enumerate(todo):
        v = index[m]
        t0 = time.time()
        d = get(f"{GT}/pools/{v['pair']}/ohlcv/minute?aggregate=5&limit=1000&before_timestamp={int(v['grad'] + 1000 * 300)}")
        candles = sorted(((d or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or [])
        if d is None:
            continue  # failed after retries; a rerun picks it up
        json.dump({"mint": m, **v, "candles": candles}, open(os.path.join(out, m + ".json"), "w"))
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(todo)}", flush=True)
        time.sleep(max(0, GT_GAP - (time.time() - t0)))


if __name__ == "__main__":
    main()
