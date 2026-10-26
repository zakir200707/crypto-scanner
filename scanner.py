"""Solana memecoin scanner.

Pulls trending/boosted/high-volume Solana tokens from DexScreener and GeckoTerminal,
drops fakes, bot-wash volume, rugs and copycats, then scores the survivors on:
volume/MC, buyer dominance, pullback entry (20-40% off 24h high), floor holding, safety,
and smart money: whale ($300+) net flow, wallets quietly accumulating, and "smart wallets"
the scanner has seen buy other tokens early before they doubled (learned over time in wallets.json).

Usage:
    python scanner.py              # one scan
    python scanner.py --loop 15    # rescan every 15 min, beep when a new best pick appears
    python scanner.py --min-age 6  # allow tokens younger than 24h (riskier)

Not financial advice. Data is a snapshot; most small-cap memecoins go to zero.
"""
import argparse, json, math, os, sys, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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

# Early-gem tier (younger/smaller coins: bigger upside, many more rugs)
EARLY_MIN_LIQ = 15_000
EARLY_MIN_AGE = 2         # hours; need some candles to judge
EARLY_MAX_MC = 3_000_000
EARLY_MIN_VOL_MC = 1.0
EARLY_MIN_TXNS = 300
ALERT_MAIN = 60
ALERT_EARLY = 65          # higher bar because the tier is riskier
REPORT_HOUR_UTC = 9       # daily "still alive" summary (9:00 UK winter time)

# Smart money
WHALE_USD = 300           # a "whale" trade on a sub-$5M memecoin
LEARN_TOKENS = 8          # extra trending tokens per scan whose whale buyers get recorded
SMART_WIN_X = 2.0         # a wallet "wins" if the token later trades at 2x its entry
SMART_LOSS_X = 0.5        # ...and "loses" if it later trades at half its entry
TRACK_DAYS = 7
GT_GAP = 4.0             # seconds between GeckoTerminal calls
DB_VERSION = 2            # bump to wipe wallet memory when grading rules change

# Pre-pump detection
MAX_RUNUP_MAIN = 300      # % above 24h low: beyond this the move already happened
MAX_RUNUP_EARLY = 400
MAX_PUMP_1H = 25          # % up in the last hour: the pump is already under way, an alert would be late
MAX_DRIFT = 10            # % the price may move while the scan runs before the alert is dropped
MAX_WHALE_EXIT = 0.2      # whales net selling more than this share of liquidity in 6h: reject
BUY_LIMIT = 1.10          # alerts say "don't buy above" this multiple of the alert price
RPC = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
RADAR_WALLETS = 30        # smart wallets whose holdings get checked each scan
RPC_GAP = 2.0             # public RPC throttles heavy calls hard
BOT_ACCOUNTS = 300        # wallets holding this many tokens are sniper bots, not smart money


_last_gt = [0.0]


def get(url, tries=3):
    for i in range(tries):
        if url.startswith(GT):  # GeckoTerminal free tier: stay under its rate limit
            time.sleep(max(0.0, _last_gt[0] + GT_GAP - time.time()))
            _last_gt[0] = time.time()
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


def rpc(method, params):
    time.sleep(RPC_GAP)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r).get("result")
    except Exception:
        return None


def radar(db):
    """Smart-wallet radar: diff each proven wallet's token holdings against last scan.
    Coins they just picked up - before the coin trends - come back as {mint: n_wallets}."""
    smart = sorted((w for w, r in db["wallets"].items() if is_smart(r)),
                   key=lambda w: -db["wallets"][w]["w"])[:RADAR_WALLETS]
    snaps, hits = db.setdefault("holdings", {}), {}
    for w in smart:
        res = rpc("getTokenAccountsByOwner", [w, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}])
        if res is None:
            continue
        accts = res.get("value") or []
        if len(accts) > BOT_ACCOUNTS:
            db["wallets"][w]["bot"] = True
            snaps.pop(w, None)
            continue
        held = {a["account"]["data"]["parsed"]["info"]["mint"] for a in accts
                if float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) > 0}
        if w in snaps:
            for mint in held - set(snaps[w]) - SKIP_MINTS:
                hits[mint] = hits.get(mint, 0) + 1
        snaps[w] = sorted(held)
    for w in list(snaps):
        if w not in smart:
            del snaps[w]
    return hits


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
    # Go well beyond the front page: trending over several windows, deep volume and
    # transaction rankings, and freshly created pools, so quiet risers get caught too.
    gt_paths = [f"/trending_pools?duration={d}" for d in ("1h", "6h", "24h")] + \
               [f"/pools?sort=h24_volume_usd_desc&page={i}" for i in range(1, 5)] + \
               [f"/pools?sort=h24_tx_count_desc&page={i}" for i in (1, 2)] + \
               [f"/new_pools?page={i}" for i in (1, 2)]
    for path in gt_paths:
        d = get(GT + path) or {}
        for pool in d.get("data") or []:
            mints.add(pool["relationships"]["base_token"]["data"]["id"].removeprefix("solana_"))
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
    if not mc or liq < EARLY_MIN_LIQ:
        return m, "liquidity too low"
    if liq > mc * 1.5:
        return m, "liquidity > MC (fake pool)"
    if m["avg_trade_24h"] < MIN_AVG_TRADE or m["avg_trade_1h"] < MIN_AVG_TRADE:
        return m, f"avg trade ${min(m['avg_trade_24h'], m['avg_trade_1h']):.0f} (bot volume)"
    if m["bs_1h"] < 1.0 or m["bs_6h"] < 1.0:
        return m, "sellers dominate"
    if (m["pc_1h"] or 0) > MAX_PUMP_1H:
        return m, f"already up {m['pc_1h']:.0f}% in the last hour (alert would be late)"
    if liq >= MIN_LIQ and age_h >= min_age_h:
        m["tier"] = "main"
        if n24 < MIN_TXNS_24H:
            return m, "too few trades"
        if m["vol_mc"] < MIN_VOL_MC:
            return m, f"vol/MC {m['vol_mc']:.2f} < 0.5"
        return m, None
    # Early gem tier: younger or smaller coins, held to a higher activity bar
    m["tier"] = "early"
    if age_h < EARLY_MIN_AGE:
        return m, f"age {age_h:.1f}h < {EARLY_MIN_AGE}h"
    if mc > EARLY_MAX_MC:
        return m, "too big for early tier"
    if n24 < EARLY_MIN_TXNS:
        return m, "too few trades"
    if m["vol_mc"] < EARLY_MIN_VOL_MC:
        return m, f"vol/MC {m['vol_mc']:.2f} < {EARLY_MIN_VOL_MC}"
    return m, None


def price_structure(m):
    """Drawdown from 24h high and floor check from GeckoTerminal candles
    (hourly for mature coins, 15-minute for young ones)."""
    young = m["age_h"] < 30
    tf = "minute?aggregate=15&limit=96" if young else "hour?limit=72"
    per_h = 4 if young else 1
    d = get(f"{GT}/pools/{m['pair']}/ohlcv/{tf}") or {}
    c = sorted(((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
    if len(c) < 8:
        return "no candle history"
    px = m["price"]
    hi24 = max(x[2] for x in c[-24 * per_h:])
    hi72 = max(x[2] for x in c)
    lo6 = min(x[3] for x in c[-6 * per_h:])
    prev = c[-24 * per_h:-6 * per_h] or c[:-6 * per_h] or c[:1]
    lo_prev = min(x[3] for x in prev)
    lo24 = min(x[3] for x in c[-24 * per_h:])
    m["from_24h_high"] = (px / hi24 - 1) * 100
    m["from_72h_high"] = (px / hi72 - 1) * 100
    m["from_24h_low"] = (px / lo24 - 1) * 100 if lo24 else 0
    m["above_6h_low"] = (px / lo6 - 1) * 100
    m["higher_low"] = lo6 >= lo_prev
    # Volume picking up while price hasn't moved yet = buying before the breakout
    recent, prior = c[-2 * per_h:], c[-14 * per_h:-2 * per_h]
    prior_avg = sum(x[5] for x in prior) / len(prior) if prior else 0
    m["vol_accel"] = (sum(x[5] for x in recent) / len(recent)) / prior_avg if prior_avg else 0
    m["pc_2h"] = (px / recent[0][1] - 1) * 100 if recent[0][1] else 0
    if m["vol_accel"] >= 5 and m["pc_2h"] > 15:
        return f"pump in progress (volume {m['vol_accel']:.0f}x, price +{m['pc_2h']:.0f}% in 2h)"
    max_run = MAX_RUNUP_EARLY if m.get("tier") == "early" else MAX_RUNUP_MAIN
    if m["from_24h_low"] > max_run:
        return f"already pumped +{m['from_24h_low']:.0f}% from 24h low"
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
    # program-owned accounts (AMMs like the Pump.fun AMM, lockers) aren't real holders
    pools.update(a for a, k in (r.get("knownAccounts") or {}).items()
                 if (k.get("type") or "").upper() in ("AMM", "LOCKER"))
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


def trades(pair, min_usd=0):
    """Recent trades (max 300, last 24h) with the wallet behind each one."""
    url = f"{GT}/pools/{pair}/trades" + (f"?trade_volume_in_usd_greater_than={min_usd}" if min_usd else "")
    out = []
    for t in (get(url) or {}).get("data") or []:
        a = t["attributes"]
        buy = a["kind"] == "buy"
        out.append({
            "w": a["tx_from_address"], "buy": buy, "usd": float(a.get("volume_in_usd") or 0),
            "price": float((a["price_to_in_usd"] if buy else a["price_from_in_usd"]) or 0),
            "ts": datetime.fromisoformat(a["block_timestamp"].replace("Z", "+00:00")).timestamp(),
        })
    return out


def record_entries(db, mint, whale_trades):
    """Remember each whale's first buy price so we can later tell if they were early.
    Wallets that sell within an hour of buying are flipping bots, not conviction buyers."""
    pos = db["positions"].setdefault(mint, {"t": time.time(), "entries": {}})
    sells = {}
    for t in whale_trades:
        if not t["buy"]:
            sells.setdefault(t["w"], []).append(t["ts"])
    for t in sorted(whale_trades, key=lambda x: x["ts"]):
        flipped = any(0 <= s - t["ts"] <= 3600 for s in sells.get(t["w"], []))
        if t["buy"] and t["price"] and not flipped and t["w"] not in pos["entries"] and len(pos["entries"]) < 300:
            pos["entries"][t["w"]] = t["price"]


def is_smart(rec):
    return rec["w"] >= 2 and rec["w"] >= rec["l"] and not rec.get("bot")


def learn(db, prices):
    """Grade recorded whale entries against today's price: 2x = win, half = loss."""
    now = time.time()
    missing = [m for m in db["positions"] if m not in prices]
    for i in range(0, len(missing), 30):
        for p in get(f"{DEX}/tokens/v1/solana/{','.join(missing[i:i + 30])}") or []:
            a = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if a in missing and (a not in prices or liq > prices[a][1]):
                prices[a] = (float(p.get("priceUsd") or 0), liq)
        time.sleep(0.3)
    for mint in list(db["positions"]):
        pos = db["positions"][mint]
        px = prices.get(mint, (0, 0))[0]
        for w, entry in list(pos["entries"].items()):
            rec = db["wallets"].setdefault(w, {"w": 0, "l": 0})
            if px >= entry * SMART_WIN_X:
                rec["w"] += 1; del pos["entries"][w]
            elif (px and px <= entry * SMART_LOSS_X) or (not px and now - pos["t"] > 86400):
                rec["l"] += 1; del pos["entries"][w]
        if now - pos["t"] > TRACK_DAYS * 86400 or not pos["entries"]:
            del db["positions"][mint]
    # keep the file small: forget wallets that only ever lose
    db["wallets"] = {w: r for w, r in db["wallets"].items() if r["w"] or r["l"] < 3}


def flow(m, db):
    """Wallet-level order flow: real unique buyers, whale accumulation, smart wallets."""
    recent = trades(m["pair"])
    whales = trades(m["pair"], WHALE_USD)
    if not recent:
        return "no trade data"
    now = time.time()
    buyers = {t["w"] for t in recent if t["buy"]}
    sellers = {t["w"] for t in recent if not t["buy"]}
    buy_vol = {}
    for t in recent:
        if t["buy"]:
            buy_vol[t["w"]] = buy_vol.get(t["w"], 0) + t["usd"]
    total_buy = sum(buy_vol.values()) or 1
    m["uniq_buyers"], m["uniq_sellers"] = len(buyers), len(sellers)
    m["wallet_bs"] = len(buyers) / max(len(sellers), 1)
    m["top_buyer_share"] = max(buy_vol.values(), default=0) / total_buy

    def net(since):
        b = sum(t["usd"] for t in whales if t["buy"] and t["ts"] >= since)
        s = sum(t["usd"] for t in whales if not t["buy"] and t["ts"] >= since)
        return b, s
    wb6, ws6 = net(now - 6 * 3600)
    wb24, ws24 = net(0)
    m["whale_net_6h"], m["whale_net_24h"] = wb6 - ws6, wb24 - ws24
    m["whale_buy_24h"], m["whale_sell_24h"] = wb24, ws24

    whale_sellers = {t["w"] for t in whales if not t["buy"]} | sellers
    whale_buys = {}
    for t in whales:
        if t["buy"]:
            whale_buys[t["w"]] = whale_buys.get(t["w"], 0) + 1
    m["accumulators"] = sum(1 for w, n in whale_buys.items() if n >= 2 and w not in whale_sellers)
    smart = [w for w in whale_buys if w not in whale_sellers and is_smart(db["wallets"].get(w, {"w": 0, "l": 0}))]
    m["smart_buyers"] = len(smart)
    m["smart_wallets"] = smart[:5]
    record_entries(db, m["mint"], whales)

    if m["whale_net_6h"] < -MAX_WHALE_EXIT * m["liq"]:
        return f"whales net sold ${-m['whale_net_6h']:,.0f} in 6h ({-m['whale_net_6h'] / m['liq']:.0%} of liquidity)"
    if ws24 > 2 * wb24 and ws24 > 2000:
        return f"whales dumping (${ws24:,.0f} sold vs ${wb24:,.0f} bought, 24h)"
    if m["top_buyer_share"] > 0.4 and len(buyers) > 5:
        return f"one wallet is {m['top_buyer_share']:.0%} of recent buying (fake pump)"
    if m["wallet_bs"] < 0.9:
        return f"more unique sellers than buyers ({len(sellers)} vs {len(buyers)})"
    return None


def score(m):
    s, why = 0.0, []
    # Volume interest (15)
    s += min(m["vol_mc"] / 3, 1) * 15
    # Buyer dominance: txn ratios + real unique wallets (20)
    avg_bs = (m["bs_5m"] + m["bs_1h"] + m["bs_6h"]) / 3
    s += min(max(avg_bs - 1, 0) / 0.8, 1) * 8
    s += min(max(m["wallet_bs"] - 1, 0) / 0.8, 1) * 7
    if m["bs_5m"] >= m["bs_1h"] >= m["bs_6h"] >= 1:
        s += 5; why.append("buyer pressure rising 6h->1h->5m")
    # Entry (15): early in the move beats a dip after a huge run
    dd = -m["from_24h_high"]
    if 20 <= dd <= 40:
        s += 7; why.append(f"{dd:.0f}% pullback (in 20-40% zone)")
    elif 12 <= dd < 20 or 40 < dd <= 50:
        s += 3
    run = m["from_24h_low"]
    if run < 60:
        s += 8; why.append(f"only +{run:.0f}% from 24h low (early)")
    elif run < 120:
        s += 4
    elif run > 200:
        s -= 5
    # Pre-pump (8 + 16): quiet accumulation and smart-wallet radar
    if m["vol_accel"] >= 1.5 and abs(m["pc_2h"]) < 15:
        s += 8; why.append(f"volume {m['vol_accel']:.1f}x prior hours while price flat (quiet accumulation)")
    if m.get("radar"):
        s += min(m["radar"], 2) * 8
        why.append(f"{m['radar']} smart wallet(s) just bought (radar)")
    # Floor holding (10)
    if m["higher_low"]:
        s += 6; why.append("higher low vs prior 18h")
    if 0 <= m["above_6h_low"] <= 35:
        s += 4
    # Smart money (25)
    if m["whale_net_6h"] > 0:
        s += min(m["whale_net_6h"] / max(m["liq"] * 0.05, 1), 1) * 8
        why.append(f"whales net bought ${m['whale_net_6h']:,.0f} in 6h")
    elif m["whale_net_6h"] < 0:  # big money leaving outweighs small buyers arriving
        s -= min(-m["whale_net_6h"] / max(m["liq"] * 0.05, 1), 1) * 10
    if m["accumulators"]:
        s += min(m["accumulators"], 4) * 1.5
        why.append(f"{m['accumulators']} whale wallets accumulating (2+ buys, no sells)")
    if m["smart_buyers"]:
        s += min(m["smart_buyers"], 3) * 4
        why.append(f"{m['smart_buyers']} proven smart wallets buying")
    # Safety (15)
    s += 15
    if m["insider_wallets"] > 300: s -= 6
    elif m["insider_wallets"] > 100: s -= 3
    if any("copycat" in r.lower() for r in m["risks"]): s -= 6
    s -= min(len([r for r in m["risks"] if r.startswith("warn")]) * 2, 6)
    if m["top10_pct"] > 30: s -= 3
    # Age maturity: tokens that survived past 3 days are less likely to be day-one rugs
    if m["age_h"] > 72: s += 3
    m["score"] = round(min(s, 100), 1)
    m["why"] = why
    return m


def scan(min_age_h, db, verbose=True):
    log = print if verbose else (lambda *a, **k: None)
    log(f"[{datetime.now():%H:%M:%S}] checking smart-wallet radar...")
    hits = radar(db)
    log(f"  radar: {len(hits)} coins newly bought by smart wallets")
    log("  gathering candidates...")
    mints = list(set(gather_candidates()) | set(hits))
    pairs = best_pairs(mints)
    log(f"  {len(mints)} tokens, {len(pairs)} with Solana pairs")
    passed, rejected = [], {}
    s1, all_m = [], []
    for p in pairs:
        m, why = stage1(p, min_age_h)
        m["radar"] = hits.get(m["mint"], 0)
        all_m.append(m)
        (rejected.__setitem__(m["mint"], (m["symbol"], why)) if why else s1.append(m))
    log(f"  {len(s1)} passed liquidity/volume/bot/age filters; checking charts, safety, wallets...")
    # Radar coins always get a deep check; otherwise the most active per tier
    by_rank = sorted(s1, key=lambda x: (-x["radar"], -x["vol_mc"]))
    main = [m for m in by_rank if m["tier"] == "main"][:10]
    early = [m for m in by_rank if m["tier"] == "early"][:8]
    for m in main + early:
        why = price_structure(m)
        if not why:
            why = safety(m); time.sleep(1)
        if not why:
            why = flow(m, db)
        if why:
            rejected[m["mint"]] = (m["symbol"], why)
        else:
            passed.append(score(m))
    passed.sort(key=lambda x: -x["score"])

    # Smart-wallet learning: record whale buyers in other real, active tokens (any age),
    # then grade every recorded entry against current prices.
    done = {m["mint"] for m in s1}
    learn_set = [m for m in all_m if m["mint"] not in done and m["liq"] >= 30_000 and m["mc"]
                 and m["avg_trade_24h"] >= MIN_AVG_TRADE and m["txns_24h"] >= MIN_TXNS_24H]
    for m in sorted(learn_set, key=lambda x: -x["vol_mc"])[:LEARN_TOKENS]:
        record_entries(db, m["mint"], trades(m["pair"], WHALE_USD))
    learn(db, {m["mint"]: (m["price"], m["liq"]) for m in all_m})
    smart_n = sum(1 for r in db["wallets"].values() if is_smart(r))
    early_n = sum(1 for r in db["wallets"].values() if r["w"])
    log(f"  smart-wallet memory: {early_n} wallets caught a 2x early, {smart_n} proven smart (2+ wins), "
        f"{len(db['positions'])} tokens being tracked")
    return passed, rejected, s1


def fmt_money(x):
    return f"${x/1e6:.2f}M" if x >= 1e6 else f"${x/1e3:.0f}K"


def fmt_price(x):
    """$0.0004461 style: plain decimals with 4 significant digits, no scientific notation."""
    if x >= 1:
        return f"${x:,.4f}"
    digits = 4 - int(math.floor(math.log10(x))) - 1 if x > 0 else 4
    return f"${x:.{digits}f}"


def uk_now():
    try:
        return datetime.now(ZoneInfo("Europe/London")).strftime("%H:%M %d %b (UK)")
    except Exception:  # no timezone database (e.g. bare Windows Python)
        return datetime.now(timezone.utc).strftime("%H:%M %d %b (UTC)")


def report(passed, rejected, s1):
    print("\n" + "=" * 78)
    if rejected:
        print("Rejected after deeper checks:")
        for m in s1:
            if m["mint"] in rejected:
                print(f"  - {m['symbol'][:12]:<12} {rejected[m['mint']][1]}")
    best = {}
    for tier, label, bar in (("main", "MAIN (24h+ old, $50K+ liquidity)", ALERT_MAIN),
                             ("early", "EARLY GEMS (younger/smaller - high risk)", ALERT_EARLY)):
        tp = [m for m in passed if m["tier"] == tier]
        print(f"\n--- {label} ---")
        if not tp:
            print("No token passed every filter right now. Best move: don't buy; rescan later.")
            continue
        best[tier] = describe(tp, bar)
    return best


def describe(passed, bar):
    print(f"{'#':<3}{'token':<13}{'score':>6}{'MC':>10}{'liq':>9}{'v/MC':>6}{'off24hHi':>9}{'b/s 5m/1h/6h':>16}{'age':>6}")
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
    print(f"  wallets: {b['uniq_buyers']} buyers vs {b['uniq_sellers']} sellers (last 300 trades) | "
          f"whale net 6h ${b['whale_net_6h']:,.0f}, 24h ${b['whale_net_24h']:,.0f} | "
          f"accumulators {b['accumulators']} | smart wallets {b['smart_buyers']}")
    print(f"  pre-pump: +{b['from_24h_low']:.0f}% from 24h low | volume {b['vol_accel']:.1f}x prior hours, "
          f"price {b['pc_2h']:+.0f}% in 2h | radar {b['radar']}")
    if b["why"]:
        print("  + " + "; ".join(b["why"]))
    if b["risks"]:
        print("  ! " + "; ".join(b["risks"]))
    if b["score"] < bar:
        print(f"  NOTE: best score is under {bar} - weak setup; consider skipping.")
    return b


def save(passed, rejected):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"scan_{datetime.now():%Y%m%d_%H%M}.json")
    with open(path, "w") as f:
        json.dump({"time": datetime.now().isoformat(), "passed": passed,
                   "rejected": {k: list(v) for k, v in rejected.items()}}, f, indent=1)
    return path


def refresh(b):
    """Re-read the price right before alerting: the scan takes minutes and memecoins move fast.
    Returns a reason to skip the alert, or None (and updates price/MC to the fresh values)."""
    p = ((get(f"{DEX}/latest/dex/pairs/solana/{b['pair']}") or {}).get("pairs") or [None])[0]
    if not p or not b["price"]:
        return "could not re-check the price"
    px = float(p.get("priceUsd") or 0)
    drift = (px / b["price"] - 1) * 100
    h1 = (p.get("priceChange") or {}).get("h1") or 0
    if drift > MAX_DRIFT:
        return f"price moved +{drift:.0f}% while scanning"
    if h1 > MAX_PUMP_1H:
        return f"up {h1:.0f}% in the last hour"
    b["price"], b["mc"] = px, p.get("marketCap") or b["mc"]
    return None


def push(topic, b):
    """Phone alert via ntfy.sh (install the ntfy app and subscribe to the same topic)."""
    msg = (f"{b['name']} ({b['symbol']}) score {b['score']}\n"
           f"Price {fmt_price(b['price'])} at {uk_now()}\n"
           f"DON'T BUY ABOVE {fmt_price(b['price'] * BUY_LIMIT)} - if it's higher you're late, skip it.\n"
           f"Plan: stop {fmt_price(b['price'] * 0.75)} (-25%), sell half at {fmt_price(b['price'] * 1.5)} (+50%). Max $10.\n"
           f"MC {fmt_money(b['mc'])} | liq {fmt_money(b['liq'])} | {b['from_24h_high']:.0f}% off 24h high\n"
           f"Up {b['from_24h_low']:.0f}% from 24h low | volume {b['vol_accel']:.1f}x | "
           f"radar {b['radar']} smart wallet(s)\n"
           f"Whales net 6h ${b['whale_net_6h']:,.0f} | accumulators {b['accumulators']} | "
           f"smart wallets {b['smart_buyers']}\n"
           f"{'; '.join(b['risks']) or 'no rugcheck warnings'}\n{b['url']}")
    kind = "EARLY GEM (high risk)" if b["tier"] == "early" else "New pick"
    title = f"{kind}: {b['symbol']}".encode("ascii", "ignore").decode()
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
    ap.add_argument("--wallets", default=os.path.join(OUT_DIR, "wallets.json"),
                    help="smart-wallet memory file (grows more useful over days)")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # token names often contain emoji
    db = {"version": DB_VERSION, "wallets": {}, "positions": {}, "holdings": {}}
    if os.path.exists(a.wallets):
        try:
            saved = json.load(open(a.wallets))
            if saved.get("version") == DB_VERSION:  # older memory was polluted by flipping bots
                db.update(saved)
        except ValueError:
            pass

    def save_db():
        os.makedirs(os.path.dirname(os.path.abspath(a.wallets)), exist_ok=True)
        with open(a.wallets, "w") as f:
            json.dump(db, f)

    last = {}
    if a.state and os.path.exists(a.state):
        raw = open(a.state).read().strip()
        try:
            last = json.loads(raw) if raw else {}
        except ValueError:
            last = {"main": raw}  # older single-pick format

    def alert(best):
        """Alert on a tier's top pick at most once per coin per 24h; returns True if anything fired."""
        fired = False
        now = time.time()
        sent = {m: t for m, t in last.get("alerted", {}).items() if now - t < 86400}
        for tier, bar in (("main", ALERT_MAIN), ("early", ALERT_EARLY)):
            b = best.get(tier)
            skip = b and b["mint"] not in sent and b["score"] >= bar and refresh(b)
            if skip:
                print(f"not alerting {b['symbol']}: {skip}")
            elif b and b["mint"] not in sent and b["score"] >= bar:
                print(f"\a*** NEW {tier.upper()} PICK: {b['symbol']} ***")
                if a.ntfy:
                    push(a.ntfy, b)
                sent[b["mint"]] = now
                last.setdefault("open", {})[b["mint"]] = {"sym": b["symbol"], "pair": b["pair"], "price": b["price"],
                                                          "t": now, "stage": 0, "url": b["url"]}
                fired = True
            last[tier] = b["mint"] if b else None
        last["alerted"] = sent
        return fired

    def follow_up():
        """Follow each alerted coin and say when its plan says to sell: half at +50% (then the rest
        at +100% or back at the buy price), everything at -25%, or a reminder after 72h."""
        now = time.time()
        for mint, o in list(last.get("open", {}).items()):
            p = ((get(f"{DEX}/latest/dex/pairs/solana/{o['pair']}") or {}).get("pairs") or [None])[0]
            px = float((p or {}).get("priceUsd") or 0)
            if not px:
                continue
            r = px / o["price"]
            msg, close = None, True
            if o["stage"] == 0 and r >= 1.5:
                msg, o["stage"] = (f"TARGET HIT +{(r - 1) * 100:.0f}%: sell HALF now. Keep the rest, and sell it at "
                                   f"{fmt_price(o['price'] * 2)} (+100%) or if it falls back to your buy price "
                                   f"{fmt_price(o['price'])}."), 1
                close = False
            elif o["stage"] == 0 and r <= 0.75:
                msg = f"STOP HIT {(r - 1) * 100:.0f}%: sell now. Small loss is the plan working."
            elif o["stage"] == 1 and r >= 2:
                msg = f"+{(r - 1) * 100:.0f}%: sell the REST now."
            elif o["stage"] == 1 and r <= 1.0:
                msg = "Back to your buy price: sell the rest now (you already locked in profit on the first half)."
            elif now - o["t"] > 72 * 3600:
                msg = f"72h since the alert, now {(r - 1) * 100:+.0f}%. If you still hold it, sell."
            if msg:
                print(f"follow-up {o['sym']}: {msg}")
                if a.ntfy:
                    try:
                        body = f"{o['sym']}: {msg}\nAlert price {fmt_price(o['price'])}, now {fmt_price(px)} at {uk_now()}\n{o['url']}"
                        req = urllib.request.Request(f"https://ntfy.sh/{a.ntfy}", data=body.encode(),
                                                     headers={"Title": f"SELL SIGNAL: {o['sym']}".encode("ascii", "ignore").decode(),
                                                              "Priority": "high", "Click": o["url"] or ""})
                        urllib.request.urlopen(req, timeout=15)
                    except Exception as e:
                        print(f"push failed: {e}")
                if close:
                    del last["open"][mint]

    def daily_report(best, fired):
        """Track every run; once a day after REPORT_HOUR_UTC send a 'still alive' summary."""
        st = last.setdefault("stats", {"runs": [], "best": {}, "alerts": 0, "reported": ""})
        now = datetime.now(timezone.utc)
        st["runs"] = [t for t in st["runs"] if t > now.timestamp() - 86400] + [now.timestamp()]
        st["alerts"] += int(fired)
        for tier, b in best.items():
            if b["score"] > (st["best"].get(tier) or ["", -1])[1]:
                st["best"][tier] = [b["symbol"], b["score"]]
        today = now.strftime("%Y-%m-%d")
        if now.hour < REPORT_HOUR_UTC or st["reported"] == today:
            return
        smart_n = sum(1 for r in db["wallets"].values() if is_smart(r))
        lines = [f"{len(st['runs'])} scans in the last 24h, {st['alerts']} pick alert(s)."]
        for tier, label in (("main", "Best main"), ("early", "Best early gem")):
            sym, sc = st["best"].get(tier) or ("none passed", None)
            lines.append(f"{label}: {sym}" + (f" (score {sc})" if sc is not None else ""))
        lines.append(f"{smart_n} proven smart wallets tracked.")
        lines.append(f"Sent {uk_now()}")
        print("DAILY REPORT\n" + "\n".join(lines))
        if a.ntfy:
            try:
                req = urllib.request.Request(f"https://ntfy.sh/{a.ntfy}", data="\n".join(lines).encode(),
                                             headers={"Title": "Scanner OK - daily report", "Priority": "low"})
                urllib.request.urlopen(req, timeout=15)
            except Exception as e:
                print(f"push failed: {e}")
        st.update(best={}, alerts=0, reported=today)

    while True:
        passed, rejected, s1 = scan(a.min_age, db)
        save_db()
        best = report(passed, rejected, s1)
        print(f"\nsaved {save(passed, rejected)}")
        follow_up()
        fired = alert(best)
        daily_report(best, fired)
        if a.state:
            with open(a.state, "w") as f:
                json.dump(last, f)
        if a.loop is None:
            break
        if fired and sys.platform == "win32":
            import winsound
            winsound.Beep(880, 400); winsound.Beep(1320, 400)
        print(f"next scan in {a.loop:g} min (Ctrl+C to stop)")
        time.sleep(a.loop * 60)


if __name__ == "__main__":
    main()
