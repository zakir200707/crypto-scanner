"""Add older graduations to results/grads/_index.json using Solana's own public RPC
(full history, but ~4 requests/s), for days PublicNode no longer serves.

  python research/extend_index.py --days 5
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import insiders  # noqa: E402

RPC = "https://api.mainnet-beta.solana.com"
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
INDEX = "results/grads/_index.json"
GAP = 0.28  # ~3.5 requests/s across all threads, under the 40-per-10s-per-method limit
_lock = threading.Lock()
_next = [0.0]


def _wait():
    with _lock:
        t = max(_next[0], time.time())
        _next[0] = t + GAP
    time.sleep(max(0, t - time.time()))


def rpc(method, params, tries=8):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(tries):
        _wait()
        try:
            req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            r = json.load(urllib.request.urlopen(req, timeout=40))
            if "result" in r:
                return r["result"]
        except Exception:
            time.sleep(3 * (i + 1))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=5)
    a = ap.parse_args()
    index = json.load(open(INDEX))
    oldest = min(v["grad"] for v in index.values())
    start = time.time() - a.days * 86400
    print(f"index has {len(index)} coins back to {time.strftime('%m-%d %H:%M', time.gmtime(oldest))}; extending to "
          f"{time.strftime('%m-%d %H:%M', time.gmtime(start))}", flush=True)
    sigs, before = [], None
    while True:
        p = {"limit": 1000}
        if before:
            p["before"] = before
        page = rpc("getSignaturesForAddress", [insiders.MIGRATOR, p])
        if not page:
            break
        before = page[-1]["signature"]
        sigs += [s for s in page if not s.get("err") and start <= (s.get("blockTime") or 0) < oldest]
        if page[-1]["blockTime"] < start:
            break
    print(f"{len(sigs)} older migration transactions", flush=True)
    new = {}
    fetch = lambda s: rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
    with ThreadPoolExecutor(4) as ex:
        txs = ex.map(fetch, sigs)
    for i, (s, tx) in enumerate(zip(sigs, txs)):
        for b in (tx or {}).get("meta", {}).get("postTokenBalances") or []:
            if b["mint"].endswith("pump") and b["mint"] not in index:
                new[b["mint"]] = min(new.get(b["mint"], 1e12), s["blockTime"])
        if (i + 1) % 250 == 0:
            print(f"  {i+1}/{len(sigs)} transactions, {len(new)} new coins", flush=True)
    mints = list(new)
    for i in range(0, len(mints), 30):
        pairs = insiders.get(DEX + ",".join(mints[i:i + 30])) or []
        for p in pairs:
            m = p["baseToken"]["address"]
            if m in new and p.get("dexId") != "pumpfun":
                v = index.setdefault(m, {"grad": new[m]})
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if liq >= v.get("liq", -1):
                    v.update(pair=p["pairAddress"], dex=p.get("dexId"), liq=liq,
                             mc_now=p.get("marketCap") or 0, sym=p["baseToken"].get("symbol"))
        for m in mints[i:i + 30]:
            index.setdefault(m, {"grad": new[m]})
        time.sleep(0.25)
    json.dump(index, open(INDEX, "w"))
    print(f"index now {len(index)} coins", flush=True)


if __name__ == "__main__":
    main()
