"""Live pre-pump watcher for brand-new Pump.fun coins, with self-testing alert rules.

The 15-min scanner only sees coins once they trend, which is usually after the move.
This runs continuously: it hears every new Pump.fun coin and every graduation the
moment it happens (PumpPortal's free websocket) and re-checks each young coin on
DexScreener every POLL seconds.

No rule is trusted up front. Every snapshot is logged, and every hour each candidate
rule is backtested on the last 48h as a real trade (one alert per coin, buy ~30s late,
take-profit / stop-loss / time limit, 3% costs). Only rules that made money on both
the older and the newer half of the data send alerts, and each alert is followed by
a "sell now" message when its target, stop or time limit is hit.

  python live.py --log results/live_snaps.jsonl --hours 3     # collect only
  python live.py --ntfy TOPIC --state state --hours 5.6       # GitHub Actions
"""

import argparse
import asyncio
import glob
import gzip
import json
import math
import os
import statistics
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import websockets

from research.news_test import FEEDS as NEWS_FEEDS, fetch as fetch_feed, keywords_from, match as news_match

WS = "wss://pumpportal.fun/api/data"
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
POLL = 30             # seconds between DexScreener sweeps
TRACK_H = 4           # stop watching a coin after this many hours
DEAD_MC = 6000        # drop coins still below this market cap ...
DEAD_AFTER = 20 * 60  # ... after this many seconds
LOG_MC = 5000         # only log coins once they have reached this market cap
KEEP_H = 48           # backtest window
RETEST = 3600         # seconds between backtests
COST = 0.03           # round-trip fees + slippage
MIN_COINS = 25        # a rule needs this many past trades before it may alert
MIN_AVG = 0.05        # ... and this average return per trade
GRAD_RULE = "Survived graduation (5+ min after, MC $10K+), 30% trailing stop, max 48h"
GRAD_WAIT = 300       # seconds after graduation before a coin counts as having survived the first dump
GRAD_MIN_MC = 1e4
GRAD_TRAIL = 0.3
GRAD_TRACK_H = 48     # graduated coins that stay alive are followed this long ...
GRAD_SLOW_AFTER = 4 * 3600
SLOW_POLL = 300       # ... and polled every 5 min after their first 4 hours
GRAD_MIN_COINS = 150  # high-variance setup (most trades lose ~30%), so it needs more trades to prove itself
# News coins: new coins named after a trending Google search or a current headline.
NEWS_REFRESH = 300    # seconds between news/trends reads
NEWS_TTL = 12 * 3600  # a keyword stays "in the news" this long
NEWS_DELAY = 60       # alert/entry this long after launch (time to notice and buy)
NEWS_MIN_COINS = 40
NEWS_RULES = {
    "News-named new coin, buy 1 min after launch, +100%/-40%/60m": (1.0, 0.4, 3600),
    "News-named new coin, buy 1 min after launch, +50%/-30%/30m": (0.5, 0.3, 1800),
}
# Pre-graduation: buy on the Pump.fun curve once the market cap crosses a threshold, sell
# straight after graduation, when Pump.fun's BOOST auto-buys push the new pool's price up.
PREGRAD_SELL_AFTER = 60   # seconds after graduation we assume it takes to sell
PREGRAD_HOLD = 3600       # sell anyway if it has not graduated within an hour
PREGRAD_MIN_COINS = 60   # first tests: about +30% per trade in one 2.5h window, flat in the next, so it must
                         # also make MIN_AVG in BOTH halves before it may alert
PREGRAD_RULES = {f"Pre-graduation: MC crosses ${t/1e3:.0f}K on the curve, sell 1 min after graduation": t
                 for t in (28000, 31000, 34000)}
REPORT_HOUR_UTC = 9
UA = {"User-Agent": "Mozilla/5.0"}
LOG_VERSION = 2       # v2: graduated coins are priced from their graduated pool (v1 could read the frozen curve)

# Candidate rules: market-cap band x buys in last 5 min x exit plan. All require the
# market cap to have grown 20%+ in the last ~2.5 min (momentum just starting).
EXITS = [(0.3, 0.2, 900), (0.5, 0.3, 1800), (1.0, 0.4, 1800)]
RULES = {}
for lo, hi in [(8e3, 20e3), (12e3, 35e3), (20e3, 60e3), (35e3, 1e5), (6e4, 3e5), (1e5, 1e6)]:
    for bmin in (60, 120):
        for tp, sl, hold in EXITS:
            name = f"MC ${lo/1e3:.0f}K-{hi/1e3:.0f}K, {bmin}+ buys/5m, +{tp:.0%}/-{sl:.0%}/{hold//60}m"
            RULES[name] = (lo, hi, bmin, 1.2, tp, sl, hold)


def matches(rule, s):
    lo, hi, bmin, gmin = rule[:4]
    return s["price"] > 0 and lo <= s["mc"] < hi and s["b5"] >= bmin and s.get("g2", 1) >= gmin


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def snapshot(p):
    tx = p.get("txns") or {}
    vol = p.get("volume") or {}
    pc = p.get("priceChange") or {}
    info = p.get("info") or {}
    return {
        "dex": p.get("dexId"),
        "price": float(p.get("priceUsd") or 0),
        "mc": p.get("marketCap") or p.get("fdv") or 0,
        "liq": (p.get("liquidity") or {}).get("usd") or 0,
        "v5": vol.get("m5") or 0, "v1h": vol.get("h1") or 0,
        "b5": (tx.get("m5") or {}).get("buys", 0), "s5": (tx.get("m5") or {}).get("sells", 0),
        "b1h": (tx.get("h1") or {}).get("buys", 0), "s1h": (tx.get("h1") or {}).get("sells", 0),
        "pc5": pc.get("m5") or 0, "pc1h": pc.get("h1") or 0,
        "socials": len(info.get("socials") or []) + len(info.get("websites") or []),
        "boost": (p.get("boosts") or {}).get("active", 0),
    }


def growth(hist, s):
    """Market cap now vs ~2.5 min ago."""
    prev = [x for x in hist if x["t"] >= s["t"] - 150 and x is not s]
    return s["mc"] / prev[0]["mc"] if prev and prev[0]["mc"] else 1


def fmt_money(x):
    return f"${x/1e6:.2f}M" if x >= 1e6 else f"${x/1e3:.0f}K"


def fmt_price(x):
    if x >= 1:
        return f"${x:,.4f}"
    digits = 4 - int(math.floor(math.log10(x))) - 1 if x > 0 else 4
    return f"${x:.{digits}f}"


def uk_now():
    try:
        return datetime.now(ZoneInfo("Europe/London")).strftime("%H:%M %d %b (UK)")
    except Exception:
        return datetime.now(timezone.utc).strftime("%H:%M %d %b (UTC)")


# ---------------------------------------------------------------- backtest

def load_history(paths):
    coins = {}
    for path in paths:
        op = gzip.open if path.endswith(".gz") else open
        try:
            with op(path, "rt", encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue  # partly written last line
                    coins.setdefault(r["mint"], []).append(r)
        except (OSError, EOFError):
            continue
    cutoff = time.time() - KEEP_H * 3600
    out = {}
    for mint, h in coins.items():
        h = sorted((r for r in h if r["t"] >= cutoff), key=lambda r: r["t"])
        if not h or max(r["mc"] for r in h) < 8e3:
            continue
        for s in h:
            s["g2"] = growth(h, s)
        out[mint] = h
    return out


def trade(h, i, tp, sl, hold, last_t):
    """Buy at the snapshot after h[i]; return (return, first_alert_time) or None if too recent."""
    e = h[i + 1]
    if e["t"] + hold > last_t and h[-1]["t"] > last_t - 120:
        return None
    entry = e["price"]
    if entry <= 0:
        return None
    fut = [x for x in h[i + 2:] if x["t"] <= e["t"] + hold]
    for x in fut:
        r = x["price"] / entry
        if r >= 1 + tp:
            return 1 + tp - 1 - COST
        if r <= 1 - sl:
            return r - 1 - COST
    # time limit, or the coin died and dropped off the watch list (last seen price)
    return (fut[-1]["price"] / entry if fut else 0.3) - 1 - COST


def grad_trades(coins, last_t):
    """Graduation survivors: buy at the snapshot after the first one taken 5+ min after graduation
    with MC $10K+, then sell on a 30% drop from the best price since entry (max 48h)."""
    res = []
    for h in coins.values():
        g = max((r.get("grad") or 0) for r in h)
        if not g or min(r.get("v", 1) for r in h) < 2:
            continue  # older logs may hold frozen curve prices after graduation
        for i, s in enumerate(h[:-1]):
            if s["t"] < g + GRAD_WAIT or s["mc"] < GRAD_MIN_MC or s["price"] <= 0:
                continue
            e = h[i + 1]
            if e["price"] <= 0:
                break
            peak, out = e["price"], None
            for x in h[i + 2:]:
                if x["t"] > e["t"] + GRAD_TRACK_H * 3600:
                    break
                if x["price"] <= peak * (1 - GRAD_TRAIL):
                    out = x["price"] / e["price"]
                    break
                peak = max(peak, x["price"])
            if out is None:
                if h[-1]["t"] >= last_t - 900 and h[-1]["t"] < e["t"] + GRAD_TRACK_H * 3600:
                    break  # still being followed and the stop has not hit: no result yet
                out = h[-1]["price"] / e["price"]  # held 48h, or the coin died and was dropped
            res.append((s["t"], out - 1 - COST))
            break
    return res


def news_trades(coins, last_t, tp, sl, hold):
    """News-named coins: buy at the first snapshot 1+ min after launch, fixed exits."""
    res = []
    for h in coins.values():
        born = h[0].get("born")
        if not born or not any(r.get("news") for r in h):
            continue
        ent = [x for x in h if x["t"] >= born + NEWS_DELAY and x["price"] > 0]
        if not ent or ent[0]["t"] > born + NEWS_DELAY + 120:
            continue
        i = h.index(ent[0])
        if i == 0:
            h = [dict(ent[0], t=ent[0]["t"] - 1)] + h  # trade() buys at the snapshot after h[i]
            i = 0
        else:
            i -= 1
        r = trade(h, i, tp, sl, hold, last_t)
        if r is not None:
            res.append((born, r))
    return res


def pregrad_trades(coins, last_t, thr):
    """Buy at the curve snapshot after the first one with MC >= thr; sell at the first graduated-pool
    snapshot PREGRAD_SELL_AFTER s after graduation, or at the last curve price after PREGRAD_HOLD."""
    res = []
    for h in coins.values():
        if min(r.get("v", 1) for r in h) < 2:
            continue
        g = max((r.get("grad") or 0) for r in h)
        curve = [r for r in h if r.get("dex") == "pumpfun" and r["price"] > 0 and (not g or r["t"] < g)]
        i = next((k for k, r in enumerate(curve) if r["mc"] >= thr), None)
        if i is None or i + 1 >= len(curve):
            continue  # never crossed, or graduated before the next look (too late to buy on the curve)
        e = curve[i + 1]
        if g and g <= e["t"] + PREGRAD_HOLD:
            post = [r for r in h if r.get("dex") != "pumpfun" and r["price"] > 0 and r["t"] >= g + PREGRAD_SELL_AFTER]
            if post:
                res.append((curve[i]["t"], post[0]["price"] / e["price"] - 1 - COST))
            elif h[-1]["t"] < last_t - 900:
                res.append((curve[i]["t"], h[-1]["price"] / e["price"] - 1 - COST))  # pool never priced: last seen
            continue
        if e["t"] + PREGRAD_HOLD > last_t and h[-1]["t"] > last_t - 120:
            continue  # still running
        later = [r for r in curve if e["t"] < r["t"] <= e["t"] + PREGRAD_HOLD]
        res.append((curve[i]["t"], (later[-1]["price"] if later else e["price"]) / e["price"] - 1 - COST))
    return res


def summarize(res):
    res.sort()
    rets = [r for _, r in res]
    half = len(rets) // 2
    return {"n": len(rets), "avg": statistics.mean(rets), "win": sum(r > 0 for r in rets) / len(rets),
            "old": statistics.mean(rets[:half]) if half else 0, "new": statistics.mean(rets[half:])}


def backtest(coins):
    if not coins:
        return {}
    last_t = max(h[-1]["t"] for h in coins.values())
    stats = {}
    for name, rule in RULES.items():
        tp, sl, hold = rule[4:]
        res = []
        for h in coins.values():
            for i, s in enumerate(h[:-1]):
                if matches(rule, s):
                    r = trade(h, i, tp, sl, hold, last_t)
                    if r is not None:
                        res.append((s["t"], r))
                    break
        if res:
            stats[name] = summarize(res)
    res = grad_trades(coins, last_t)
    if res:
        stats[GRAD_RULE] = summarize(res)
    for name, (tp, sl, hold) in NEWS_RULES.items():
        res = news_trades(coins, last_t, tp, sl, hold)
        if res:
            stats[name] = summarize(res)
    for name, thr in PREGRAD_RULES.items():
        res = pregrad_trades(coins, last_t, thr)
        if res:
            stats[name] = summarize(res)
    return stats


def live_rules(stats):
    good = {k: v for k, v in stats.items()
            if v["n"] >= (GRAD_MIN_COINS if k == GRAD_RULE else NEWS_MIN_COINS if k in NEWS_RULES
                          else PREGRAD_MIN_COINS if k in PREGRAD_RULES else MIN_COINS)
            and v["avg"] >= MIN_AVG and v["old"] > 0 and v["new"] > 0
            and (k not in PREGRAD_RULES or min(v["old"], v["new"]) >= MIN_AVG)}
    return dict(sorted(good.items(), key=lambda kv: -kv[1]["avg"]))


# ---------------------------------------------------------------- watcher

class Watcher:
    def __init__(self, args):
        self.args = args
        self.coins = {}  # mint -> {born, sym, dev_sol, grad, logged, hist}
        self.log_path = args.log or (os.path.join(args.state, "snaps", f"{int(time.time())}.jsonl") if args.state else None)
        if self.log_path:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
        self.log = open(self.log_path, "a", encoding="utf-8") if self.log_path else None
        self.state_path = os.path.join(args.state, "live.json") if args.state else None
        self.state = {"alerted": {}, "open": {}, "closed": [], "report_day": "", "stats": {}, "live": {}}
        if self.state_path and os.path.exists(self.state_path):
            self.state.update(json.load(open(self.state_path, encoding="utf-8")))
        self.last_test = 0
        self.created = 0  # new-coin events heard this run
        self.news_kw = {}  # keyword -> {"t": first seen, "src": feed}
        for mint, o in self.state["open"].items():  # keep following positions opened by an earlier run
            self.coins[mint] = {"born": None, "sym": o["sym"], "dev_sol": 0, "grad": o.get("grad") or o["t"],
                                "logged": True, "hist": [], "added": time.time(), "grad_seen": True}

    def save(self):
        if self.state_path:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f)

    def push(self, title, body, url="", priority="default"):
        print(f"\n[{title}]\n{body}\n", flush=True)
        if not self.args.ntfy:
            return
        try:
            req = urllib.request.Request(f"https://ntfy.sh/{self.args.ntfy}", data=body.encode(),
                                         headers={"Title": title.encode("ascii", "ignore").decode(),
                                                  "Click": url, "Priority": priority})
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print("ntfy error", e, file=sys.stderr)

    def on_message(self, m):
        mint = m.get("mint")
        if not mint:
            return
        now = time.time()
        if m.get("txType") == "create":
            self.created += 1
            k = news_match(self.news_kw, m.get("name"), m.get("symbol")) if self.news_kw else None
            self.coins.setdefault(mint, {"born": now, "sym": m.get("symbol"), "name": m.get("name"),
                                         "dev_sol": m.get("solAmount") or 0, "grad": None,
                                         "logged": bool(k),  # log news coins from the start (no survivor bias)
                                         "news": k, "news_src": self.news_kw[k]["src"] if k else None, "hist": []})
        elif m.get("txType") == "migrate" or m.get("pool") in ("pump-amm", "raydium"):
            c = self.coins.setdefault(mint, {"born": None, "sym": m.get("symbol"), "dev_sol": 0,
                                             "grad": None, "logged": False, "hist": []})
            c["grad"] = c["grad"] or now
            o = self.state["open"].get(mint)
            if o and o.get("kind") == "pregrad" and not o.get("sell_sent"):
                o["sell_sent"] = True  # tell the phone at once; manage_open records the result later
                threading.Thread(target=self.push, daemon=True, args=(
                    f"GRADUATED - SELL NOW: {o['sym']}",
                    f"{o['sym']} just graduated at {uk_now()}. Sell now, within a minute: the auto-buys after "
                    f"graduation push the price up first, then most coins dump.\n"
                    f"Bought around {fmt_price(o['entry'])}.\nhttps://jup.ag/swap/{mint}-SOL",
                    o["url"] or "", "urgent")).start()

    def sweep(self):
        now = time.time()
        for mint, c in list(self.coins.items()):
            start = c["born"] or c["grad"]
            last = c["hist"][-1] if c["hist"] else None
            if mint in self.state["open"]:
                continue
            if c["grad"]:  # graduated: follow survivors for up to 48h, drop the ones that died
                if now - c["grad"] > GRAD_TRACK_H * 3600 or (now - c["grad"] > 1800 and (not last or last["mc"] < DEAD_MC)):
                    del self.coins[mint]
            elif now - start > TRACK_H * 3600 or (now - start > DEAD_AFTER and (not last or last["mc"] < DEAD_MC)):
                del self.coins[mint]
        mints = [m for m, c in self.coins.items()
                 if m in self.state["open"] or not c["grad"] or now - c["grad"] < GRAD_SLOW_AFTER
                 or not c["hist"] or now - c["hist"][-1]["t"] >= SLOW_POLL - 15]
        for i in range(0, len(mints), 30):
            try:
                pairs = get(DEX + ",".join(mints[i:i + 30]))
            except Exception as e:
                print("dex error", e, file=sys.stderr)
                continue
            best = {}
            for p in pairs:
                a = p["baseToken"]["address"]
                if a not in self.coins:
                    continue
                # After graduation the Pump.fun curve pair is frozen at the graduation price, so the
                # graduated pool (PumpSwap/Raydium) always wins; then the deepest, most traded pair.
                key = (p.get("dexId") != "pumpfun", (p.get("liquidity") or {}).get("usd") or 0,
                       (p.get("volume") or {}).get("h1") or 0)
                if a not in best or key > best[a][0]:
                    best[a] = (key, p)
            for a, (_, p) in best.items():
                c = self.coins[a]
                s = snapshot(p)
                s["t"] = round(now)
                s["url"] = p.get("url")
                c["sym"] = c["sym"] or p["baseToken"].get("symbol")
                c["name"] = c.get("name") or p["baseToken"].get("name")
                c["hist"] = c["hist"][-20:] + [s]
                s["g2"] = growth(c["hist"], s)
                c["logged"] = c["logged"] or s["mc"] >= LOG_MC
                if self.log and c["logged"]:
                    row = {k: v for k, v in s.items() if k not in ("url", "g2")}
                    self.log.write(json.dumps({"mint": a, "sym": c["sym"], "born": c["born"], "grad": c["grad"],
                                               "dev_sol": c["dev_sol"], "v": LOG_VERSION, "news": c.get("news"),
                                               **row}) + "\n")
                self.check(a, c, s)
        if self.log:
            self.log.flush()
        self.manage_open(now)
        if self.args.state and now - self.last_test > RETEST:
            self.retest()
        self.daily_report()
        self.save()

    # -------------------------------------------------------------- alerts

    def check(self, mint, c, s):
        if not self.args.state or mint in self.state["alerted"]:
            return
        if (c["grad"] and not c.get("grad_seen") and s["t"] >= c["grad"] + GRAD_WAIT
                and s["mc"] >= GRAD_MIN_MC and s["price"] > 0):
            c["grad_seen"] = True  # only the first qualifying moment counts, exactly as in the backtest
            if GRAD_RULE in self.state["live"]:
                self.alert_survivor(mint, c, s, self.state["live"][GRAD_RULE])
                return
        if c.get("news") and not c.get("news_seen") and c["born"] and s["t"] >= c["born"] + NEWS_DELAY:
            c["news_seen"] = True  # first chance only, as in the backtest
            for name, st in self.state["live"].items():
                if name in NEWS_RULES and s["price"] > 0:
                    tp, sl, hold = NEWS_RULES[name]
                    self.state["alerted"][mint] = s["t"]
                    self.state["open"][mint] = {"sym": c["sym"], "rule": name, "entry": s["price"], "t": s["t"],
                                                "tp": tp, "sl": sl, "hold": hold, "url": s["url"]}
                    body = (f"{c.get('name') or c['sym']} ({c['sym']}) launched 1 min ago, named after "
                            f"'{c['news']}' which is in the news now ({c.get('news_src')})\n"
                            f"Price {fmt_price(s['price'])} at {uk_now()} | MC {fmt_money(s['mc'])}\n"
                            f"PLAN: sell at {fmt_price(s['price'] * (1 + tp))} (+{tp:.0%}), stop at "
                            f"{fmt_price(s['price'] * (1 - sl))} (-{sl:.0%}), or after {hold // 60} min. I will message you.\n"
                            f"This setup, last 48h: {st['n']} coins, {st['win']:.0%} winners, avg {st['avg']:+.0%} per trade.\n"
                            f"Very high risk - small amounts only.\nhttps://jup.ag/swap/SOL-{mint}")
                    self.push(f"NEWS COIN: {c['sym']}", body, s["url"] or "", "high")
                    return
        if not c["grad"] and s.get("dex") == "pumpfun" and s["price"] > 0:
            seen = c.setdefault("pregrad_seen", [])  # thresholds already crossed: first crossing only, as in the backtest
            for name, st in self.state["live"].items():
                thr = PREGRAD_RULES.get(name)
                if thr and s["mc"] >= thr and thr not in seen:
                    self.alert_pregrad(mint, c, s, name, st)
                    return
            seen += [t for t in PREGRAD_RULES.values() if s["mc"] >= t and t not in seen]
        for name, st in self.state["live"].items():
            if name == GRAD_RULE or name in NEWS_RULES or name in PREGRAD_RULES:
                continue
            rule = RULES.get(name)
            if rule and matches(rule, s):
                tp, sl, hold = rule[4:]
                self.state["alerted"][mint] = s["t"]
                self.state["open"][mint] = {"sym": c["sym"], "rule": name, "entry": s["price"], "t": s["t"],
                                            "tp": tp, "sl": sl, "hold": hold, "url": s["url"]}
                age = (s["t"] - (c["born"] or c["grad"])) / 60
                body = (f"{c.get('name') or c['sym']} ({c['sym']}) - {'graduated' if c['grad'] else 'still on Pump.fun curve'}, {age:.0f} min old\n"
                        f"Price {fmt_price(s['price'])} at {uk_now()}\n"
                        f"MC {fmt_money(s['mc'])} (+{(s['g2']-1)*100:.0f}% in 2.5 min) | {s['b5']} buys / {s['s5']} sells in 5 min\n"
                        f"PLAN: sell at {fmt_price(s['price']*(1+tp))} (+{tp:.0%}), stop at {fmt_price(s['price']*(1-sl))} (-{sl:.0%}), "
                        f"or after {hold//60} min. I'll message you when to sell.\n"
                        f"This setup, last 48h: {st['n']} coins, {st['win']:.0%} winners, avg {st['avg']:+.0%} per trade.\n"
                        f"Very high risk - small amounts only.\n"
                        f"https://jup.ag/swap/SOL-{mint}")
                self.push(f"LAUNCH ALERT: {c['sym']}", body, s["url"] or "", "high")
                return

    def alert_survivor(self, mint, c, s, st):
        self.state["alerted"][mint] = s["t"]
        self.state["open"][mint] = {"sym": c["sym"], "rule": GRAD_RULE, "entry": s["price"], "t": s["t"],
                                    "kind": "trail", "trail": GRAD_TRAIL, "peak": s["price"], "grad": c["grad"],
                                    "hold": GRAD_TRACK_H * 3600, "url": s["url"]}
        body = (f"{c.get('name') or c['sym']} ({c['sym']}) graduated {(s['t'] - c['grad']) / 60:.0f} min ago "
                f"and survived the first dump\n"
                f"Price {fmt_price(s['price'])} at {uk_now()} | MC {fmt_money(s['mc'])}\n"
                f"PLAN: buy small (max $5-10). Sell when it falls 30% below its best price after you buy "
                f"(right now: below {fmt_price(s['price'] * (1 - GRAD_TRAIL))}). I will message you when to sell, for up to 48h.\n"
                f"Most of these trades lose about 30%; a few big runners pay for them.\n"
                f"This setup, last 48h: {st['n']} coins, {st['win']:.0%} winners, avg {st['avg']:+.0%} per trade.\n"
                f"https://jup.ag/swap/SOL-{mint}")
        self.push(f"GRADUATION SURVIVOR: {c['sym']}", body, s["url"] or "", "high")

    def alert_pregrad(self, mint, c, s, name, st):
        self.state["alerted"][mint] = s["t"]
        self.state["open"][mint] = {"sym": c["sym"], "rule": name, "entry": s["price"], "t": s["t"],
                                    "kind": "pregrad", "hold": PREGRAD_HOLD, "url": s["url"]}
        body = (f"{c.get('name') or c['sym']} ({c['sym']}) is close to graduating from Pump.fun: "
                f"MC {fmt_money(s['mc'])} at {uk_now()}, price {fmt_price(s['price'])}\n"
                f"PLAN: buy small NOW on Pump.fun (max $5-10), only if it has NOT graduated yet and the price is "
                f"under {fmt_price(s['price'] * 1.15)}. Sell within a minute of graduation - I will send "
                f"GRADUATED - SELL NOW. If it has not graduated in 1 hour, sell anyway.\n"
                f"This setup, last 48h: {st['n']} coins, {st['win']:.0%} winners, avg {st['avg']:+.0%} per trade.\n"
                f"https://pump.fun/coin/{mint}")
        self.push(f"ABOUT TO GRADUATE: {c['sym']}", body, f"https://pump.fun/coin/{mint}", "high")

    def manage_open(self, now):
        for mint, o in list(self.state["open"].items()):
            c = self.coins.get(mint)
            s = c["hist"][-1] if c and c["hist"] else None
            why = None
            r = s["price"] / o["entry"] if s and o["entry"] else None
            if o.get("kind") == "pregrad":
                if c and c["grad"]:
                    if s and s.get("dex") != "pumpfun" and s["t"] >= c["grad"] + PREGRAD_SELL_AFTER:
                        self.state["closed"].append({**o, "exit_t": now, "ret": r - 1, "why": "graduated"})
                        del self.state["open"][mint]
                    elif now - c["grad"] > 900:
                        self.state["closed"].append({**o, "exit_t": now, "ret": None, "why": "graduated, no pool price"})
                        del self.state["open"][mint]
                    continue  # the sell message went out when the graduation was heard
                if now - o["t"] >= o["hold"]:
                    why = f"NOT GRADUATED IN 1H, now {(r - 1) * 100:+.0f}% - sell" if r else "NOT GRADUATED IN 1H - sell"
                elif s is None and (not c or now - c.get("added", 0) > 600):
                    why = "lost track of price - check it and sell"
            elif o.get("kind") == "trail":
                if s:
                    o["peak"] = max(o["peak"], s["price"])
                if s and s["price"] <= o["peak"] * (1 - o["trail"]):
                    why = f"TRAILING STOP HIT, {(r - 1) * 100:+.0f}% from your buy - sell now"
                elif now - o["t"] >= o["hold"]:
                    why = f"48H UP, now {(r - 1) * 100:+.0f}% - sell" if r else "48H UP - sell"
                elif s is None and (not c or now - c.get("added", 0) > 600):
                    why = "lost track of price - check it and sell"
            elif r is not None and r >= 1 + o["tp"]:
                why = f"TARGET HIT +{(r-1)*100:.0f}% - sell now"
            elif r is not None and r <= 1 - o["sl"]:
                why = f"STOP HIT {(r-1)*100:.0f}% - sell now"
            elif now - o["t"] >= o["hold"]:
                why = f"TIME UP ({o['hold']//60} min), now {(r-1)*100:+.0f}% - sell" if r else "TIME UP - sell"
            elif s is None and (not c or now - c.get("added", 0) > 600):
                why = "lost track of price - check it and sell"
            if why:
                self.push(f"{why.split(' -')[0]}: {o['sym']}",
                          f"{o['sym']}: {why}\nBought at {fmt_price(o['entry'])}, now {fmt_price(s['price']) if s else '?'} at {uk_now()}\n"
                          f"https://jup.ag/swap/{mint}-SOL", o["url"] or "", "high")
                self.state["closed"].append({**o, "exit_t": now, "ret": (r - 1) if r else None, "why": why})
                del self.state["open"][mint]
        self.state["closed"] = self.state["closed"][-300:]
        cutoff = now - 3 * 86400
        self.state["alerted"] = {k: v for k, v in self.state["alerted"].items() if v > cutoff}

    def retest(self):
        self.last_test = time.time()
        snaps = os.path.join(self.args.state, "snaps")
        for p in glob.glob(os.path.join(snaps, "*.jsonl*")):
            if os.path.getmtime(p) < time.time() - (KEEP_H + 6) * 3600:
                os.remove(p)
        paths = glob.glob(os.path.join(snaps, "*.jsonl*")) + glob.glob("data/*.jsonl*")
        coins = load_history(paths)
        stats = backtest(coins)
        live = live_rules(stats)
        newly = [k for k in live if k not in self.state["live"]]
        self.state["stats"], self.state["live"] = stats, live
        print(f"retest: {len(coins)} coins, {len(live)} live rule(s)", flush=True)
        for k, v in sorted(stats.items(), key=lambda kv: -kv[1]["avg"])[:5]:
            print(f"  {k}: n={v['n']} win={v['win']:.0%} avg={v['avg']:+.1%} old={v['old']:+.1%} new={v['new']:+.1%}")
        if newly:
            self.push("Launch watch: new setup switched ON",
                      "\n".join(f"{k}: {live[k]['n']} coins, {live[k]['win']:.0%} win, avg {live[k]['avg']:+.0%}" for k in newly),
                      priority="low")

    def daily_report(self):
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        if not self.args.ntfy or now.hour < REPORT_HOUR_UTC or self.state["report_day"] == day:
            return
        self.state["report_day"] = day
        recent = [c for c in self.state["closed"] if c["exit_t"] > time.time() - 86400 and c.get("ret") is not None]
        lines = [f"Launch alerts last 24h: {len(recent)}"]
        if recent:
            lines.append(f"Winners {sum(c['ret'] > 0 for c in recent)}/{len(recent)}, "
                         f"avg {statistics.mean(c['ret'] for c in recent):+.0%} per trade (before fees)")
            lines += [f"  {c['sym']}: {c['ret']:+.0%}" for c in recent[-8:]]
        live = self.state["live"]
        lines.append(f"Setups switched on: {len(live)}" + ("" if live else " (none has made money yet, so no alerts)"))
        best = sorted(self.state["stats"].items(), key=lambda kv: -kv[1]["avg"])[:3]
        lines += [f"  {k}: {v['n']} coins, avg {v['avg']:+.0%}" for k, v in best]
        for name in NEWS_RULES:
            n_ = self.state["stats"].get(name)
            if n_:
                lines.append(f"News-coin test ({name.split(', ')[-1]}): {n_['n']} coins, avg {n_['avg']:+.0%} "
                             f"(older {n_['old']:+.0%}, newer {n_['new']:+.0%}); switches on at {NEWS_MIN_COINS}+ with both halves positive")
        lines.append(f"Trending keywords tracked now: {len(self.news_kw)}")
        for name, thr in PREGRAD_RULES.items():
            p_ = self.state["stats"].get(name)
            if p_:
                lines.append(f"Pre-graduation test (${thr/1e3:.0f}K): {p_['n']} coins, avg {p_['avg']:+.0%} "
                             f"(older {p_['old']:+.0%}, newer {p_['new']:+.0%}); switches on at {PREGRAD_MIN_COINS}+ with both halves +5% or better")
        g = self.state["stats"].get(GRAD_RULE)
        if g:
            lines.append(f"Graduation-survivor test: {g['n']} trades, avg {g['avg']:+.0%} (older {g['old']:+.0%}, "
                         f"newer {g['new']:+.0%}); switches on at {GRAD_MIN_COINS}+ trades with both halves positive")
        lines.append(f"Sent {uk_now()}")
        self.push("Launch watch daily report", "\n".join(lines), priority="low")


async def listen(w):
    while True:
        try:
            async with websockets.connect(WS, ping_interval=30) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                async for raw in ws:
                    w.on_message(json.loads(raw))
        except Exception as e:
            print("ws reconnect:", e, file=sys.stderr)
            await asyncio.sleep(5)


async def news_loop(w):
    """Keep w.news_kw filled with what is trending in searches and headlines right now."""
    while True:
        for src, url in NEWS_FEEDS.items():
            try:
                kws = await asyncio.to_thread(lambda: keywords_from(src, fetch_feed(url)))
                now = time.time()
                for k in kws:
                    w.news_kw.setdefault(k, {"t": now, "src": src})
            except Exception as e:
                print("news feed error", src, e, file=sys.stderr)
        now = time.time()
        w.news_kw = {k: v for k, v in w.news_kw.items() if now - v["t"] < NEWS_TTL}
        await asyncio.sleep(NEWS_REFRESH)


async def run(args):
    w = Watcher(args)
    end = time.time() + args.hours * 3600
    task = asyncio.create_task(listen(w))
    news_task = asyncio.create_task(news_loop(w))
    started = time.time()
    while time.time() < end:
        t = time.time()
        if not w.created and t - started > 600:
            sys.exit("no new coins from the PumpPortal feed in 10 min - feed blocked or down")
        await asyncio.to_thread(w.sweep)
        print(time.strftime("%H:%M:%S"), "watching", len(w.coins), "open", len(w.state["open"]), flush=True)
        await asyncio.sleep(max(1, POLL - (time.time() - t)))
    task.cancel()
    news_task.cancel()
    if w.log:
        w.log.close()
        if args.state:  # compress this run's log for the cache
            with open(w.log_path, "rb") as src, gzip.open(w.log_path + ".gz", "wb") as dst:
                dst.write(src.read())
            os.remove(w.log_path)
    w.save()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", help="collect-only: append snapshots here")
    ap.add_argument("--state", help="state dir (alerts, rule stats, snapshot history)")
    ap.add_argument("--ntfy")
    ap.add_argument("--hours", type=float, default=5.6)
    ap.add_argument("--backtest", action="store_true", help="just backtest the logs in --state and data/")
    args = ap.parse_args()
    if args.backtest:
        paths = glob.glob(os.path.join(args.state or "state", "snaps", "*.jsonl*")) + glob.glob("data/*.jsonl*")
        if args.log:
            paths.append(args.log)
        stats = backtest(load_history(paths))
        for k, v in sorted(stats.items(), key=lambda kv: -kv[1]["avg"]):
            print(f"{k:55s} n={v['n']:3d} win={v['win']:.0%} avg={v['avg']:+.1%} old={v['old']:+.1%} new={v['new']:+.1%}")
        print("live:", list(live_rules(stats)))
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
