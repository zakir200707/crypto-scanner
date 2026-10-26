"""Side study: simple timing rules on large, established Solana memecoins (hourly candles).

Latency does not matter at this size, so this tests whether a human can do better than
buy-and-hold with trend / dip / breakout rules. Caveat: the coin list is today's biggest
memecoins (survivors), so absolute returns are flattering; compare each rule with
buy-and-hold on the same coins and the same hours.

  python research/largecap_study.py
"""

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

import numpy as np

GT = "https://api.geckoterminal.com/api/v2/networks/solana"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
CACHE = "results/largecap"
MAJORS = {"SOL", "WSOL", "USDC", "USDT", "JUP", "JTO", "PYTH", "RAY", "ORCA", "MSOL", "JITOSOL", "BSOL", "WBTC",
          "CBBTC", "ETH", "WETH", "JLP", "INF", "HNT", "RENDER", "W", "DRIFT", "KMNO", "CLOUD", "TNSR", "ZEUS",
          "PENGU", "TRUMP", "MELANIA", "USD1", "PYUSD", "USDS", "EURC", "SYRUPUSDC", "JUPSOL", "HSOL", "VSOL"}
COST = 0.01


def get(url):
    for i in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(15 * (i + 1))
                continue
            return None
        except Exception:
            time.sleep(5)
    return None


MEMES = ["BONK", "WIF", "POPCAT", "MEW", "BOME", "FARTCOIN", "PNUT", "GOAT", "MOODENG", "CHILLGUY", "GIGA",
         "MICHI", "RETARDIO", "WEN", "MYRO", "SLERF", "PONKE", "FWOG", "AI16Z", "ZEREBRO", "GRIFFAIN", "USELESS",
         "TROLL", "SPX", "ALCH", "PUMP", "BILLY", "SC", "LOCKIN", "MOTHER"]


def coins():
    os.makedirs(CACHE, exist_ok=True)
    idx = os.path.join(CACHE, "_pools.json")
    if os.path.exists(idx):
        return json.load(open(idx))
    pools = {}
    for sym in MEMES:  # DexScreener search finds the main pool; GeckoTerminal only serves candles
        d = get(f"https://api.dexscreener.com/latest/dex/search?q={sym}") or {}
        for p in d.get("pairs") or []:
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if (p.get("chainId") == "solana" and p["baseToken"]["symbol"].upper() == sym
                    and p["quoteToken"]["symbol"].upper() in ("SOL", "WSOL", "USDC", "USDT")
                    and liq > pools.get(sym, {}).get("liq", 2e5)):
                pools[sym] = {"pool": p["pairAddress"], "liq": liq, "fdv": p.get("fdv") or 0}
        time.sleep(0.5)
    json.dump(pools, open(idx, "w"))
    return pools


def candles(sym, pool):
    path = os.path.join(CACHE, sym.replace("/", "_") + ".json")
    if os.path.exists(path):
        return json.load(open(path))
    d = get(f"{GT}/pools/{pool}/ohlcv/hour?limit=1000") or {}
    c = sorted(((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
    json.dump(c, open(path, "w"))
    time.sleep(6)
    return c


def ema(x, n):
    a, out, m = 2 / (n + 1), np.empty(len(x)), x[0]
    for i, v in enumerate(x):
        m = a * v + (1 - a) * m
        out[i] = m
    return out


def run_rule(c, enter, tp=None, sl=None, hold=None, trail=None, exit_sig=None):
    """Trades on hourly closes: enter at next open; exits checked on later candles."""
    t, o, h, l, cl = c[:, 0], c[:, 1], c[:, 2], c[:, 3], c[:, 4]
    res, i, n = [], 72, len(c)
    while i < n - 1:
        if not enter(i):
            i += 1
            continue
        e, j = o[i + 1], i + 1
        peak, out = e, None
        k = j
        while k < n:
            if sl and l[k] <= e * (1 - sl):
                out = min(o[k], e * (1 - sl)); break
            if trail and l[k] <= peak * (1 - trail):
                out = min(o[k], peak * (1 - trail)); break
            if tp and h[k] >= e * (1 + tp):
                out = e * (1 + tp); break
            if hold and k - j >= hold:
                out = cl[k]; break
            if exit_sig and exit_sig(k):
                out = cl[k]; break
            peak = max(peak, h[k])
            k += 1
        if out is None:
            out, k = cl[-1], n - 1
        res.append(out / e - 1 - COST)
        i = k + 1
    return res


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    P = coins()
    print(f"{len(P)} large memecoins:", ", ".join(sorted(P)))
    data = {}
    for sym, v in P.items():
        c = np.array(candles(sym, v["pool"]), dtype=float)
        if len(c) >= 300:
            data[sym] = c
    print(f"{len(data)} with 300+ hourly candles")
    rules = {}
    for sym, c in data.items():
        cl = c[:, 4]
        e24, e72 = ema(cl, 24), ema(cl, 72)
        hi7 = np.array([cl[max(0, i - 168):i + 1].max() for i in range(len(cl))])
        hi14p = np.array([cl[max(0, i - 336):i].max() if i > 0 else cl[0] for i in range(len(cl))])
        bh = cl[-1] / cl[72] - 1
        rules.setdefault("buy & hold (same window)", []).append(bh)
        rules.setdefault("trend: EMA24>EMA72, exit on cross", []).extend(
            run_rule(c, lambda i: e24[i] > e72[i] and e24[i - 1] <= e72[i - 1], exit_sig=lambda k: e24[k] < e72[k]))
        for dd in (0.25, 0.35, 0.5):
            rules.setdefault(f"dip: -{dd:.0%} from 7d high, TP20/SL15/7d", []).extend(
                run_rule(c, lambda i, dd=dd: cl[i] <= hi7[i] * (1 - dd), tp=0.2, sl=0.15, hold=168))
        rules.setdefault("breakout: new 14d high, trail 20%", []).extend(
            run_rule(c, lambda i: cl[i] > hi14p[i], trail=0.2))
        rules.setdefault("breakout: new 14d high, TP30/SL12/7d", []).extend(
            run_rule(c, lambda i: cl[i] > hi14p[i], tp=0.3, sl=0.12, hold=168))
    print()
    for k, r in rules.items():
        if r:
            print(f"{k:42s} trades={len(r):4d} win={np.mean(np.array(r) > 0):.2f} mean={statistics.mean(r):+.3f} "
                  f"median={statistics.median(r):+.3f} total={sum(r):+.2f}")


if __name__ == "__main__":
    main()
