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
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import websockets

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
REPORT_HOUR_UTC = 9
UA = {"User-Agent": "Mozilla/5.0"}

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
        if not res:
            continue
        res.sort()
        rets = [r for _, r in res]
        half = len(rets) // 2
        stats[name] = {
            "n": len(rets), "avg": statistics.mean(rets), "win": sum(r > 0 for r in rets) / len(rets),
            "old": statistics.mean(rets[:half]) if half else 0, "new": statistics.mean(rets[half:]),
        }
    return stats


def live_rules(stats):
    good = {k: v for k, v in stats.items()
            if v["n"] >= MIN_COINS and v["avg"] >= MIN_AVG and v["old"] > 0 and v["new"] > 0}
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
            self.coins.setdefault(mint, {"born": now, "sym": m.get("symbol"), "dev_sol": m.get("solAmount") or 0,
                                         "grad": None, "logged": False, "hist": []})
        elif m.get("txType") == "migrate" or m.get("pool") in ("pump-amm", "raydium"):
            c = self.coins.setdefault(mint, {"born": None, "sym": m.get("symbol"), "dev_sol": 0,
                                             "grad": None, "logged": False, "hist": []})
            c["grad"] = c["grad"] or now

    def sweep(self):
        now = time.time()
        for mint, c in list(self.coins.items()):
            start = c["born"] or c["grad"]
            last = c["hist"][-1] if c["hist"] else None
            if mint in self.state["open"]:
                continue
            if now - start > TRACK_H * 3600 or (now - start > DEAD_AFTER and (not last or last["mc"] < DEAD_MC) and not c["grad"]):
                del self.coins[mint]
        mints = list(self.coins)
        for i in range(0, len(mints), 30):
            try:
                pairs = get(DEX + ",".join(mints[i:i + 30]))
            except Exception as e:
                print("dex error", e, file=sys.stderr)
                continue
            best = {}
            for p in pairs:
                a = p["baseToken"]["address"]
                if a in self.coins and ((p.get("volume") or {}).get("h1") or 0) >= ((best.get(a) or {}).get("volume") or {}).get("h1", -1):
                    best[a] = p
            for a, p in best.items():
                c = self.coins[a]
                s = snapshot(p)
                s["t"] = round(now)
                s["url"] = p.get("url")
                c["sym"] = c["sym"] or p["baseToken"].get("symbol")
                c["name"] = p["baseToken"].get("name")
                c["hist"] = c["hist"][-20:] + [s]
                s["g2"] = growth(c["hist"], s)
                c["logged"] = c["logged"] or s["mc"] >= LOG_MC
                if self.log and c["logged"]:
                    row = {k: v for k, v in s.items() if k not in ("url", "g2")}
                    self.log.write(json.dumps({"mint": a, "sym": c["sym"], "born": c["born"], "grad": c["grad"],
                                               "dev_sol": c["dev_sol"], **row}) + "\n")
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
        for name, st in self.state["live"].items():
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

    def manage_open(self, now):
        for mint, o in list(self.state["open"].items()):
            c = self.coins.get(mint)
            s = c["hist"][-1] if c and c["hist"] else None
            why = None
            r = s["price"] / o["entry"] if s and o["entry"] else None
            if r is not None and r >= 1 + o["tp"]:
                why = f"TARGET HIT +{(r-1)*100:.0f}% - sell now"
            elif r is not None and r <= 1 - o["sl"]:
                why = f"STOP HIT {(r-1)*100:.0f}% - sell now"
            elif now - o["t"] >= o["hold"]:
                why = f"TIME UP ({o['hold']//60} min), now {(r-1)*100:+.0f}% - sell" if r else "TIME UP - sell"
            elif s is None:
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


async def run(args):
    w = Watcher(args)
    end = time.time() + args.hours * 3600
    task = asyncio.create_task(listen(w))
    started = time.time()
    while time.time() < end:
        t = time.time()
        if not w.created and t - started > 600:
            sys.exit("no new coins from the PumpPortal feed in 10 min - feed blocked or down")
        await asyncio.to_thread(w.sweep)
        print(time.strftime("%H:%M:%S"), "watching", len(w.coins), "open", len(w.state["open"]), flush=True)
        await asyncio.sleep(max(1, POLL - (time.time() - t)))
    task.cancel()
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
