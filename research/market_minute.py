"""Record a minute-by-minute dataset of established Solana memecoins (MC $500K-$100M, 3+ days
old, $50K+ liquidity) from DexScreener, so many entry rules can be backtested on it afterwards
with research/score_minute.py.

Universe: GeckoTerminal trending pools, DexScreener boosts/profiles, and DexScreener searches,
refreshed every 30 min. Every coin in the universe is snapshotted every 60 s.

  python research/market_minute.py out/minute.jsonl 330
"""

import json
import sys
import time
import urllib.request

DEX = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
SEARCHES = ["sol", "pump", "meme", "dog", "cat", "ai", "pepe", "trump", "frog", "elon", "inu", "coin", "moon", "baby",
            "bonk", "wif", "the", "official", "chill", "goat", "fart", "agent", "based", "king", "bull", "ape", "monkey",
            "penguin", "bird", "fish", "god", "girl", "boy", "man", "world", "money", "gold", "rich", "punk", "pixel",
            "game", "bot", "gpt", "cto", "community", "retard", "useless", "giga", "chad", "wojak", "doge", "shib",
            "popcat", "mew", "pnut", "moo", "hippo", "bear", "tiger", "lion", "duck", "rat", "hamster", "capy"]


def get(url):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(3 + 5 * i)
    return None


def ok(p):
    mc = p.get("marketCap") or p.get("fdv") or 0
    liq = (p.get("liquidity") or {}).get("usd") or 0
    age_d = (time.time() * 1000 - (p.get("pairCreatedAt") or time.time() * 1000)) / 8.64e7
    return p.get("chainId") == "solana" and 5e5 <= mc <= 1e8 and liq >= 5e4 and age_d >= 3 and liq <= mc


def universe():
    mints = set()
    for q in SEARCHES:
        for p in (get(f"{DEX}/latest/dex/search?q={q}") or {}).get("pairs") or []:
            if ok(p):
                mints.add(p["baseToken"]["address"])
        time.sleep(0.3)
    for path in ("/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1"):
        for x in get(DEX + path) or []:
            if x.get("chainId") == "solana":
                mints.add(x["tokenAddress"])
    for pg in range(1, 7):  # biggest Solana pools by 24h volume
        d = get(f"{GT}/pools?sort=h24_volume_usd_desc&page={pg}&include=base_token") or {}
        for p in d.get("data", []):
            mints.add(p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1])
        time.sleep(3)
    for dur in ("1h", "6h", "24h"):
        d = get(f"{GT}/trending_pools?duration={dur}&page=1&include=base_token") or {}
        for p in d.get("data", []):
            mints.add(p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1])
        time.sleep(3)
    # keep only coins whose main pair passes the filters
    keep, mints = set(), list(mints)
    for i in range(0, len(mints), 30):
        for p in get(f"{DEX}/tokens/v1/solana/{','.join(mints[i:i + 30])}") or []:
            if ok(p):
                keep.add(p["baseToken"]["address"])
        time.sleep(0.3)
    return sorted(keep)


def main():
    out, minutes = sys.argv[1], float(sys.argv[2])
    f = open(out, "a", encoding="utf-8")
    end, uni, refreshed = time.time() + minutes * 60, [], 0
    while time.time() < end:
        t0 = time.time()
        if t0 - refreshed > 1800:
            uni = sorted(set(uni) | set(universe()))[:600]
            refreshed = t0
            print(time.strftime("%H:%M:%S"), "universe", len(uni), flush=True)
        for i in range(0, len(uni), 30):
            best = {}
            for p in get(f"{DEX}/tokens/v1/solana/{','.join(uni[i:i + 30])}") or []:
                a = p["baseToken"]["address"]
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if a not in best or liq > ((best[a].get("liquidity") or {}).get("usd") or 0):
                    best[a] = p
            now = round(time.time())
            for a, p in best.items():
                tx, vol, pc = p.get("txns") or {}, p.get("volume") or {}, p.get("priceChange") or {}
                f.write(json.dumps({"t": now, "mint": a, "sym": p["baseToken"].get("symbol"),
                                    "price": float(p.get("priceUsd") or 0), "mc": p.get("marketCap") or 0,
                                    "liq": (p.get("liquidity") or {}).get("usd") or 0,
                                    "v5": vol.get("m5") or 0, "v1h": vol.get("h1") or 0, "v6h": vol.get("h6") or 0,
                                    "b5": (tx.get("m5") or {}).get("buys", 0), "s5": (tx.get("m5") or {}).get("sells", 0),
                                    "b1h": (tx.get("h1") or {}).get("buys", 0), "s1h": (tx.get("h1") or {}).get("sells", 0),
                                    "pc5": pc.get("m5") or 0, "pc1h": pc.get("h1") or 0, "pc6h": pc.get("h6") or 0,
                                    "pc24": pc.get("h24") or 0}) + "\n")
            time.sleep(0.2)
        f.flush()
        time.sleep(max(1, 60 - (time.time() - t0)))
    f.close()


if __name__ == "__main__":
    main()
