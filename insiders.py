"""Find wallets that keep buying Pump.fun coins early, before they pump.

1. Winners: coins that graduated from the Pump.fun curve (a ~15x move from launch), read
   from the history of Pump.fun's migration wallet.
2. For each winner, read its bonding-curve history from Solana (PublicNode RPC) and list
   the first buyers.
3. Wallets that were early in several winners become candidates. Each candidate's own
   recent history is read: which coins it bought, how many of those graduated, how long
   it holds, and how busy it is (bots trade constantly).

  python insiders.py --out results/insiders.json
"""

import argparse
import hashlib
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

RPC = "https://solana-rpc.publicnode.com"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MIGRATOR = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"  # signs every Pump.fun graduation
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
UA = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
EARLY = 150          # first N bonding-curve transactions count as "early"
WALLET_SIGS = 300    # how much of each candidate's own history to read
MAX_TX_H = 150       # busier wallets are treated as bots

# ---------------------------------------------------------------- base58 / PDA

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58d(s):
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def b58e(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


P = 2 ** 255 - 19
D = (-121665 * pow(121666, P - 2, P)) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def on_curve(b):
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= P:
        return False
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    return (x * x - x2) % P == 0


def bonding_curve(mint):
    prog = b58d(PUMP)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"bonding-curve" + b58d(mint) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58e(h)


# ---------------------------------------------------------------- data access

RATE = 25            # PublicNode blocks bursts; keep all threads together under this
_lock = threading.Lock()
_next = [0.0]


def _wait(pause=0.0):
    with _lock:
        now = time.time()
        _next[0] = max(_next[0], now + pause)
        t = _next[0]
        _next[0] = t + 1 / RATE
    time.sleep(max(0, t - time.time()))


def rpc(method, params, tries=6):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(tries):
        _wait()
        try:
            req = urllib.request.Request(RPC, data=body, headers=UA)
            r = json.load(urllib.request.urlopen(req, timeout=30))
            if "result" in r:
                return r["result"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _wait(pause=5 * (i + 1))  # slow everyone down
        except Exception:
            pass
        time.sleep(1 + i)
    return None


def get(url):
    for i in range(4):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20))
        except Exception:
            time.sleep(5 + 10 * i)
    return None


def all_sigs(addr, cap=20000):
    """Signatures for addr, oldest first (up to cap)."""
    out, before = [], None
    while len(out) < cap:
        p = {"limit": 1000}
        if before:
            p["before"] = before
        page = rpc("getSignaturesForAddress", [addr, p])
        if not page:
            break
        out += page
        before = page[-1]["signature"]
        if len(page) < 1000:
            break
    return out[::-1]


def txs(sigs, threads=6):
    with ThreadPoolExecutor(threads) as ex:
        return list(ex.map(lambda s: rpc("getTransaction", [s, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]), sigs))


def token_moves(tx, mint=None):
    """[(owner, mint, token_delta, sol_delta)] for every owner whose token balance changed."""
    if not tx or tx["meta"].get("err"):
        return []
    meta = tx["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    sol = {keys[i]: (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9 for i in range(len(keys))}
    bal = defaultdict(float)
    for sign, arr in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
        for b in arr:
            if mint and b["mint"] != mint:
                continue
            bal[(b.get("owner"), b["mint"])] += sign * float(b["uiTokenAmount"]["uiAmount"] or 0)
    return [(o, m, d, sol.get(o, 0)) for (o, m), d in bal.items() if abs(d) > 0]


# ---------------------------------------------------------------- steps

def graduations(start_ts, end_ts):
    """{mint: graduation time} for every Pump.fun coin graduated in [start_ts, end_ts),
    read from the history of Pump.fun's migration wallet."""
    sigs, before = [], None
    while True:
        p = {"limit": 1000}
        if before:
            p["before"] = before
        page = rpc("getSignaturesForAddress", [MIGRATOR, p]) or []
        if not page:
            break
        sigs += [s for s in page if not s.get("err") and start_ts <= (s.get("blockTime") or 0) < end_ts]
        before = page[-1]["signature"]
        if page[-1]["blockTime"] < start_ts:
            break
    out = {}
    for s, tx in zip(sigs, txs([s["signature"] for s in sigs], threads=12)):
        if not tx:
            continue
        for b in tx["meta"].get("postTokenBalances") or []:
            if b["mint"].endswith("pump"):
                out.setdefault(b["mint"], s["blockTime"])
    return out


def winners(before_ts, hours=24, limit=220):
    grads = graduations(before_ts - hours * 3600, before_ts)
    recent = sorted(grads.items(), key=lambda kv: -kv[1])[:limit]
    return {m: {"sym": m[:6], "grad_ts": t} for m, t in recent}


def early_buyers(mint, cache_dir="results/curves"):
    path = os.path.join(cache_dir, mint + ".json")
    if os.path.exists(path):
        return tuple(json.load(open(path)))
    curve, res = _early_buyers(mint)
    os.makedirs(cache_dir, exist_ok=True)
    json.dump([curve, res], open(path, "w"))
    return curve, res


def _early_buyers(mint):
    curve = bonding_curve(mint)
    sigs = [s for s in all_sigs(curve, cap=30000) if not s.get("err")]
    if not sigs:
        return curve, []
    t0 = sigs[0]["blockTime"]
    res = []
    for rank, (s, tx) in enumerate(zip(sigs[:EARLY], txs([s["signature"] for s in sigs[:EARLY]]))):
        for owner, _, d, sol in token_moves(tx, mint):
            if d > 0 and owner != curve:
                res.append({"wallet": owner, "rank": rank, "dt": s["blockTime"] - t0, "sol": -sol})
    return curve, res


def wallet_profile(w):
    sigs = rpc("getSignaturesForAddress", [w, {"limit": WALLET_SIGS}]) or []
    if not sigs:
        return None
    span_h = max(1e-3, (sigs[0]["blockTime"] - sigs[-1]["blockTime"]) / 3600)
    if len(sigs) / span_h > MAX_TX_H:  # trades nonstop: a bot, can't be copied by hand
        return {"wallet": w, "tx_per_h": len(sigs) / span_h, "span_h": span_h, "buys": {}, "sells": {}, "bot": True}
    buys, sells = {}, {}
    for s, tx in zip(sigs, txs([s["signature"] for s in sigs if not s.get("err")])):
        for owner, mint, d, sol in token_moves(tx):
            if owner != w or not mint.endswith("pump"):
                continue
            if d > 0:
                b = buys.setdefault(mint, {"t": s["blockTime"], "sol": 0.0, "tokens": 0.0})
                if s["blockTime"] <= b["t"]:  # first buy: market cap in SOL at entry (1B supply)
                    b["t"] = s["blockTime"]
                    b["mc_sol"] = -sol / d * 1e9 if d else None
                b["sol"] += -sol
                b["tokens"] += d
            else:
                x = sells.setdefault(mint, {"t": s["blockTime"], "sol": 0.0})
                x["t"] = max(x["t"], s["blockTime"])
                x["sol"] += sol
    return {"wallet": w, "tx_per_h": len(sigs) / span_h, "span_h": span_h, "buys": buys, "sells": sells}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/insiders.json")
    ap.add_argument("--before", type=float, default=time.time(), help="only use winners graduated before this unix time")
    ap.add_argument("--min-hits", type=int, default=3)
    ap.add_argument("--max-candidates", type=int, default=200)
    args = ap.parse_args()

    win = winners(args.before)
    print(f"{len(win)} training winners", flush=True)
    early = defaultdict(list)
    with ThreadPoolExecutor(3) as ex:
        for i, (mint, (curve, buyers)) in enumerate(zip(win, ex.map(early_buyers, win))):
            w = win[mint]
            w["curve"] = curve
            w["n_early"] = len({b["wallet"] for b in buyers})
            for b in buyers:
                early[b["wallet"]].append({**b, "mint": mint})
            if (i + 1) % 25 == 0:
                print(f"  early buyers read for {i+1}/{len(win)} winners", flush=True)

    hits = {wal: len({e["mint"] for e in ev}) for wal, ev in early.items()}
    cands = [wal for wal, n in sorted(hits.items(), key=lambda kv: -kv[1]) if n >= args.min_hits][:args.max_candidates]
    print(f"{len(cands)} wallets early in {args.min_hits}+ winners; profiling...", flush=True)
    profiles = {}
    with ThreadPoolExecutor(3) as ex:
        for i, (wal, p) in enumerate(zip(cands, ex.map(wallet_profile, cands))):
            if p:
                p["early_hits"] = hits[wal]
                p["early"] = early[wal]
                profiles[wal] = p
            if (i + 1) % 25 == 0:
                print(f"  profiled {i+1}/{len(cands)}", flush=True)

    # status of every coin the candidates bought: graduated (PumpSwap/Raydium pair exists) and current MC
    mints = sorted({m for p in profiles.values() for m in p["buys"]})
    status = {}
    for i in range(0, len(mints), 30):
        for pr in get(DEX + ",".join(mints[i:i + 30])) or []:
            m = pr["baseToken"]["address"]
            s = status.setdefault(m, {"grad": False, "mc": 0})
            s["grad"] = s["grad"] or pr.get("dexId") != "pumpfun"
            s["mc"] = max(s["mc"], pr.get("marketCap") or pr.get("fdv") or 0)
        time.sleep(0.3)
    json.dump({"winners": win, "profiles": profiles, "status": status, "before": args.before},
              open(args.out, "w", encoding="utf-8"))
    print("saved", args.out)


if __name__ == "__main__":
    main()
