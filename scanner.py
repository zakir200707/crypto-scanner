"""Solana memecoin scanner.

Pulls trending/boosted/high-volume Solana tokens from DexScreener and GeckoTerminal,
drops fakes, bot-wash volume, rugs and copycats, then scores the survivors on:
volume/MC, buyer dominance, pullback entry (20-40% off 24h high), floor holding, safety.

Usage:
    python scanner.py              # one scan
    python scanner.py --loop 15    # rescan every 15 min, beep when a new best pick appears
    python scanner.py --min-age 6  # allow tokens younger than 24h (riskier)

Not financial advice. Data is a snapshot; most small-cap memecoins go to zero.
"""
import argparse, json, os, sys, time, urllib.request
from datetime import datetime

DEX = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
RUG = "https://api.rugcheck.xyz/v1/tokens"
SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

# Hard filters
MIN_LIQ = 50_000
MIN_VOL_MC = 0.5
MIN_AVG_TRADE = 25        # USD; below this the "volume" is mostly bot spam
MIN_TXNS_24H = 500
MAX_TOP_HOLDER = 10       # % held by largest non-pool wallet
MIN_LP_LOCKED = 80        # %


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(8 * (i + 1))
                continue
            return None
        except Exception:
            time.sleep(2)
    return None


def gather_candidates():
    mints = set()
    for path in ["/token-boosts/latest/v1", "/token-boosts/top/v1",
                 "/token-profiles/latest/v1", "/community-takeovers/latest/v1"]:
        for t in get(DEX + path) or []:
            if t.get("chainId") == "solana":
                mints.add(t["tokenAddress"])
    for q in ["solana", "pump", "meme", "ai", "cat", "dog", "pumpswap"]:
        d = get(f"{DEX}/latest/dex/search?q={q}") or {}
        for p in d.get("pairs") or []:
            if p.get("chainId") == "solana":
                mints.add(p["baseToken"]["address"])
    for path in [f"/trending_pools?page={i}" for i in (1, 2, 3)] + \
                [f"/pools?sort=h24_volume_usd_desc&page={i}" for i in (1, 2, 3)]:
        d = get(GT + path) or {}
        for pool in d.get("data") or []:
            mints.add(pool["relationships"]["base_token"]["data"]["id"].removeprefix("solana_"))
        time.sleep(2.2)
    return [m for m in mints if m not in SKIP_MINTS]


def best_pairs(mints):
    """Highest-liquidity Solana pair per token."""
    best = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        for p in get(f"{DEX}/tokens/v1/solana/{','.join(chunk)}") or []:
            a = p["baseToken"]["address"]
            if a not in chunk:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if a not in best or liq > ((best[a].get("liquidity") or {}).get("usd") or 0):
                best[a] = p
        time.sleep(0.3)
    return list(best.values())


def ratio(tx, k):
    t = tx.get(k) or {}
    return t.get("buys", 0) / max(t.get("sells", 0), 1)


def stage1(p, min_age_h):
    """Cheap filters on DexScreener data. Returns (metrics, reject_reason)."""
    liq = (p.get("liquidity") or {}).get("usd") or 0
    mc = p.get("marketCap") or p.get("fdv") or 0
    vol, tx, pc = p.get("volume") or {}, p.get("txns") or {}, p.get("priceChange") or {}
    n24 = sum((tx.get("h24") or {}).get(k, 0) for k in ("buys", "sells"))
    n1 = sum((tx.get("h1") or {}).get(k, 0) for k in ("buys", "sells"))
    age_h = (time.time() * 1000 - (p.get("pairCreatedAt") or 0)) / 3.6e6
    m = {
        "symbol": p["baseToken"]["symbol"], "name": p["baseToken"]["name"],
        "mint": p["baseToken"]["address"], "pair": p["pairAddress"], "dex": p.get("dexId"),
        "url": p.get("url"), "price": float(p.get("priceUsd") or 0), "liq": liq, "mc": mc,
        "vol_mc": (vol.get("h24") or 0) / mc if mc else 0,
        "avg_trade_24h": (vol.get("h24") or 0) / max(n24, 1),
        "avg_trade_1h": (vol.get("h1") or 0) / max(n1, 1),
        "bs_5m": ratio(tx, "m5"), "bs_1h": ratio(tx, "h1"), "bs_6h": ratio(tx, "h6"), "bs_24h": ratio(tx, "h24"),
        "pc_5m": pc.get("m5"), "pc_1h": pc.get("h1"), "pc_6h": pc.get("h6"), "pc_24h": pc.get("h24"),
        "txns_24h": n24, "age_h": age_h,
    }
    if not mc or liq < MIN_LIQ:
        return m, "liquidity < $50K"
    if liq > mc * 1.5:
        return m, "liquidity > MC (fake pool)"
    if n24 < MIN_TXNS_24H:
        return m, "too few trades"
    if m["vol_mc"] < MIN_VOL_MC:
        return m, f"vol/MC {m['vol_mc']:.2f} < 0.5"
    if m["avg_trade_24h"] < MIN_AVG_TRADE or m["avg_trade_1h"] < MIN_AVG_TRADE:
        return m, f"avg trade ${min(m['avg_trade_24h'], m['avg_trade_1h']):.0f} (bot volume)"
    if age_h < min_age_h:
        return m, f"age {age_h:.0f}h < {min_age_h}h"
    if m["bs_1h"] < 1.0 or m["bs_6h"] < 1.0:
        return m, "sellers dominate"
    return m, None


def price_structure(m):
    """Drawdown from 24h high and floor check from GeckoTerminal hourly candles."""
    d = get(f"{GT}/pools/{m['pair']}/ohlcv/hour?limit=72") or {}
    c = sorted(((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
    if len(c) < 8:
        return "no candle history"
    px = m["price"]
    hi24 = max(x[2] for x in c[-24:])
    hi72 = max(x[2] for x in c)
    lo6 = min(x[3] for x in c[-6:])
    lo_prev = min(x[3] for x in c[-24:-6]) if len(c) > 24 else min(x[3] for x in c[:-6])
    m["from_24h_high"] = (px / hi24 - 1) * 100
    m["from_72h_high"] = (px / hi72 - 1) * 100
    m["above_6h_low"] = (px / lo6 - 1) * 100
    m["higher_low"] = lo6 >= lo_prev
    if m["from_72h_high"] > -5:
        return "at/near 72h high (top-buying risk)"
    if m["from_24h_high"] < -60:
        return f"{m['from_24h_high']:.0f}% off 24h high (death spiral)"
    return None


def safety(m):
    r = get(f"{RUG}/{m['mint']}/report")
    if not r:
        return "rugcheck unavailable"
    pools = set()
    for mk in r.get("markets") or []:
        pools.update({mk.get("pubkey"), mk.get("liquidityA"), mk.get("liquidityB")})
    holders = [h for h in r.get("topHolders") or [] if h.get("owner") not in pools and h.get("address") not in pools]
    lp_locked = max([((mk.get("lp") or {}).get("lpLockedPct") or 0) for mk in r.get("markets") or []] or [0])
    risks = r.get("risks") or []
    m["top_holder_pct"] = holders[0]["pct"] if holders else 0
    m["top10_pct"] = sum(h.get("pct", 0) for h in holders[:10])
    m["holders"] = r.get("totalHolders")
    m["insider_wallets"] = r.get("graphInsidersDetected") or 0
    m["lp_locked"] = lp_locked
    m["risks"] = [f"{x.get('level')}: {x.get('name')}" for x in risks]
    if r.get("rugged"):
        return "rugged"
    if r.get("mintAuthority") or r.get("freezeAuthority"):
        return "mint/freeze authority enabled"
    if lp_locked < MIN_LP_LOCKED:
        return f"LP only {lp_locked:.0f}% locked"
    if m["top_holder_pct"] > MAX_TOP_HOLDER:
        return f"top wallet holds {m['top_holder_pct']:.1f}%"
    danger = [x["name"] for x in risks if x.get("level") == "danger"]
    if danger:
        return "danger: " + ", ".join(danger)
    return None


def score(m):
    s, why = 0.0, []
    # Volume interest (20)
    s += min(m["vol_mc"] / 3, 1) * 20
    # Buyer dominance across windows (25)
    avg_bs = (m["bs_5m"] + m["bs_1h"] + m["bs_6h"]) / 3
    s += min(max(avg_bs - 1, 0) / 0.8, 1) * 20
    if m["bs_5m"] >= m["bs_1h"] >= m["bs_6h"] >= 1:
        s += 5; why.append("buyer pressure rising 6h->1h->5m")
    # Entry: pullback depth (20)
    dd = -m["from_24h_high"]
    if 20 <= dd <= 40:
        s += 20; why.append(f"{dd:.0f}% pullback (in 20-40% zone)")
    elif 12 <= dd < 20 or 40 < dd <= 50:
        s += 10
    # Floor holding (10)
    if m["higher_low"]:
        s += 6; why.append("higher low vs prior 18h")
    if 0 <= m["above_6h_low"] <= 35:
        s += 4
    # Real trade size (5)
    s += min(m["avg_trade_1h"] / 100, 1) * 5
    # Safety (20)
    s += 20
    if m["insider_wallets"] > 300: s -= 8
    elif m["insider_wallets"] > 100: s -= 4
    if any("copycat" in r.lower() for r in m["risks"]): s -= 8
    s -= min(len([r for r in m["risks"] if r.startswith("warn")]) * 2, 6)
    if m["top10_pct"] > 30: s -= 4
    # Age maturity: tokens that survived past 3 days are less likely to be day-one rugs
    if m["age_h"] > 72: s += 3
    m["score"] = round(s, 1)
    m["why"] = why
    return m


def scan(min_age_h, verbose=True):
    log = print if verbose else (lambda *a, **k: None)
    log(f"[{datetime.now():%H:%M:%S}] gathering candidates...")
    mints = gather_candidates()
    pairs = best_pairs(mints)
    log(f"  {len(mints)} tokens, {len(pairs)} with Solana pairs")
    passed, rejected = [], {}
    s1 = []
    for p in pairs:
        m, why = stage1(p, min_age_h)
        (rejected.__setitem__(m["symbol"], why) if why else s1.append(m))
    log(f"  {len(s1)} passed liquidity/volume/bot/age filters; checking charts + safety...")
    for m in sorted(s1, key=lambda x: -x["vol_mc"])[:25]:
        why = price_structure(m); time.sleep(2.2)
        if not why:
            why = safety(m); time.sleep(1)
        if why:
            rejected[m["symbol"]] = why
        else:
            passed.append(score(m))
    passed.sort(key=lambda x: -x["score"])
    return passed, rejected, s1


def fmt_money(x):
    return f"${x/1e6:.2f}M" if x >= 1e6 else f"${x/1e3:.0f}K"


def report(passed, rejected, s1):
    print("\n" + "=" * 78)
    if rejected:
        print("Rejected after deeper checks:")
        for m in s1:
            if m["symbol"] in rejected:
                print(f"  - {m['symbol']:<12} {rejected[m['symbol']]}")
    if not passed:
        print("\nNo token passed every filter right now. Best move: don't buy; rescan later.")
        return None
    print(f"\n{'#':<3}{'token':<13}{'score':>6}{'MC':>10}{'liq':>9}{'v/MC':>6}{'off24hHi':>9}{'b/s 5m/1h/6h':>16}{'age':>6}")
    for i, m in enumerate(passed[:10], 1):
        print(f"{i:<3}{m['symbol'][:12]:<13}{m['score']:>6}{fmt_money(m['mc']):>10}{fmt_money(m['liq']):>9}"
              f"{m['vol_mc']:>6.1f}{m['from_24h_high']:>8.0f}%"
              f"{m['bs_5m']:>6.2f}/{m['bs_1h']:.2f}/{m['bs_6h']:.2f}{m['age_h']:>5.0f}h")
    b = passed[0]
    print(f"\nBEST PICK: {b['name']} ({b['symbol']})  score {b['score']}/100")
    print(f"  mint  {b['mint']}\n  chart {b['url']}")
    print(f"  price ${b['price']:.6g} | MC {fmt_money(b['mc'])} | liq {fmt_money(b['liq'])} | "
          f"{b['from_24h_high']:.0f}% off 24h high | 24h {b['pc_24h']}%")
    print(f"  holders {b['holders']} | top wallet {b['top_holder_pct']:.1f}% | top10 {b['top10_pct']:.1f}% | "
          f"insider wallets {b['insider_wallets']} | LP locked {b['lp_locked']:.0f}%")
    if b["why"]:
        print("  + " + "; ".join(b["why"]))
    if b["risks"]:
        print("  ! " + "; ".join(b["risks"]))
    if b["score"] < 60:
        print("  NOTE: best score is under 60 - weak setup; consider skipping.")
    return b


def save(passed, rejected):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"scan_{datetime.now():%Y%m%d_%H%M}.json")
    with open(path, "w") as f:
        json.dump({"time": datetime.now().isoformat(), "passed": passed, "rejected": rejected}, f, indent=1)
    return path


def push(topic, b):
    """Phone alert via ntfy.sh (install the ntfy app and subscribe to the same topic)."""
    msg = (f"{b['name']} ({b['symbol']}) score {b['score']}\n"
           f"MC {fmt_money(b['mc'])} | liq {fmt_money(b['liq'])} | {b['from_24h_high']:.0f}% off 24h high\n"
           f"{'; '.join(b['risks']) or 'no rugcheck warnings'}\n{b['url']}")
    title = f"New pick: {b['symbol']}".encode("ascii", "ignore").decode()
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode(),
                                     headers={"Title": title, "Click": b["url"] or ""})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"push failed: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=float, help="rescan every N minutes")
    ap.add_argument("--min-age", type=float, default=24, help="minimum pair age in hours (default 24)")
    ap.add_argument("--ntfy", help="ntfy.sh topic for phone alerts on new picks")
    ap.add_argument("--state", help="file remembering the last alerted pick (for one-shot runs, e.g. GitHub Actions)")
    a = ap.parse_args()
    last_best = None
    if a.state and os.path.exists(a.state):
        last_best = open(a.state).read().strip() or None
    if a.state and a.loop is None:
        passed, rejected, s1 = scan(a.min_age)
        best = report(passed, rejected, s1)
        if best and best["mint"] != last_best and best["score"] >= 60:
            print("*** NEW BEST PICK ***")
            if a.ntfy:
                push(a.ntfy, best)
        with open(a.state, "w") as f:
            f.write(best["mint"] if best else "")
        return
    while True:
        passed, rejected, s1 = scan(a.min_age)
        best = report(passed, rejected, s1)
        print(f"\nsaved {save(passed, rejected)}")
        if a.loop is None:
            break
        if best and best["mint"] != last_best and best["score"] >= 60:
            print("\a*** NEW BEST PICK ***")
            if a.ntfy:
                push(a.ntfy, best)
            if sys.platform == "win32":
                import winsound
                winsound.Beep(880, 400); winsound.Beep(1320, 400)
        last_best = best["mint"] if best else None
        print(f"next scan in {a.loop:g} min (Ctrl+C to stop)")
        time.sleep(a.loop * 60)


if __name__ == "__main__":
    main()
