"""Live test: do new Pump.fun coins named after breaking news / trending searches do better?

Every 5 min it reads Google Trends (US, UK), Google News, BBC and Cointelegraph headlines and
keeps the trending keywords. Every new Pump.fun coin (PumpPortal feed) is matched against them.
All news-matched coins plus a 1-in-8 sample of the rest are priced on DexScreener every 30s for
TRACK_MIN minutes, so research/score_news.py can compare the two groups at a realistic entry delay.

  python research/news_test.py results/news.json 150
"""

import asyncio
import json
import os
import re
import sys
import time
import urllib.request

import websockets

WS = "wss://pumpportal.fun/api/data"
DEX = "https://api.dexscreener.com/tokens/v1/solana/"
FEEDS = {
    "trends_us": "https://trends.google.com/trending/rss?geo=US",
    "trends_gb": "https://trends.google.com/trending/rss?geo=GB",
    "gnews": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "bbc": "https://feeds.bbci.co.uk/news/rss.xml",
    "cointelegraph": "https://cointelegraph.com/rss",
}
TRACK_MIN = 120
KEYWORD_TTL = 12 * 3600
STOP = set("""about after again against also among amid amidst being below between could during every first found
from have here into just last least many more most much never news next only other over says said since some still
such than that their them then there these they this those three through today under until very want week were what
when where which while will with world would year years your live update updates watch video breaking report reports
coin token solana crypto pump fun meme memecoin official the and for new how why who are was has its not but you all
can out get got make made says help home house people time back best big day days game games team season police court
state states united president minister government election city north south east west bitcoin ethereum btc eth
apple army action award awards brand david john michael james africa china india russia america american
market markets price prices million billion dollar money bank""".split())


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "ignore")


def keywords_from(src, xml):
    titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml)[1:]
    out = set()
    for t in titles:
        t = re.sub(r" - [^-]+$", "", t)  # drop " - Publisher"
        if src.startswith("trends"):
            out.add(norm(t))  # the whole trending query, e.g. "keanureeves"
            out |= {norm(w) for w in t.split() if len(w) >= 5}
        else:
            words = re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", t)
            out |= {norm(w) for w in words}
            out |= {norm(a + b) for a, b in zip(words, words[1:])}
    return {k for k in out if len(k) >= 4 and k not in STOP}


def match(kw, name, sym):
    n, s = norm(name or ""), norm(sym or "")
    for k in kw:
        if k == s or (len(k) >= 5 and k in n) or (len(s) >= 4 and s == k[:len(s)] and len(s) >= len(k) - 1):
            return k
    return None


async def news_loop(state):
    while True:
        for src, url in FEEDS.items():
            try:
                kws = await asyncio.to_thread(lambda: keywords_from(src, fetch(url)))
                now = time.time()
                for k in kws:
                    state["kw"].setdefault(k, {"t": now, "src": src})
            except Exception as e:
                print("feed error", src, e, file=sys.stderr, flush=True)
        now = time.time()
        state["kw"] = {k: v for k, v in state["kw"].items() if now - v["t"] < KEYWORD_TTL}
        print(time.strftime("%H:%M:%S"), "keywords", len(state["kw"]), flush=True)
        await asyncio.sleep(300)


async def listen(state):
    n = 0
    while True:
        try:
            async with websockets.connect(WS, ping_interval=30) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("txType") != "create" or not m.get("mint"):
                        continue
                    n += 1
                    k = match(state["kw"], m.get("name"), m.get("symbol"))
                    if k or n % 8 == 0:
                        state["coins"][m["mint"]] = {"t": time.time(), "name": m.get("name"), "sym": m.get("symbol"),
                                                     "kw": k, "kw_src": state["kw"][k]["src"] if k else None,
                                                     "kw_age": time.time() - state["kw"][k]["t"] if k else None,
                                                     "snaps": []}
                        if k:
                            print(time.strftime("%H:%M:%S"), "NEWS COIN", m.get("symbol"), "|", m.get("name"), "<-", k, flush=True)
        except Exception as e:
            print("ws reconnect:", e, file=sys.stderr, flush=True)
            await asyncio.sleep(5)


def sweep(state):
    now = time.time()
    live = [m for m, c in state["coins"].items() if now - c["t"] < TRACK_MIN * 60]
    for i in range(0, len(live), 30):
        try:
            pairs = json.loads(fetch(DEX + ",".join(live[i:i + 30])))
        except Exception:
            continue
        best = {}
        for p in pairs:
            a = p["baseToken"]["address"]
            key = (p.get("dexId") != "pumpfun", (p.get("liquidity") or {}).get("usd") or 0)
            if a in state["coins"] and (a not in best or key > best[a][0]):
                best[a] = (key, p)
        for a, (_, p) in best.items():
            tx = (p.get("txns") or {}).get("m5") or {}
            state["coins"][a]["snaps"].append({"t": round(now), "price": float(p.get("priceUsd") or 0),
                                               "mc": p.get("marketCap") or 0, "dex": p.get("dexId"),
                                               "b5": tx.get("buys", 0), "s5": tx.get("sells", 0)})


async def main():
    out, minutes = sys.argv[1], float(sys.argv[2])
    state = {"kw": {}, "coins": {}}
    tasks = [asyncio.create_task(news_loop(state)), asyncio.create_task(listen(state))]
    end = time.time() + minutes * 60
    k = 0
    while time.time() < end:
        t = time.time()
        await asyncio.to_thread(sweep, state)
        k += 1
        if k % 4 == 0:
            tmp = out + ".tmp"
            json.dump({"coins": state["coins"], "kw": state["kw"]}, open(tmp, "w"))
            os.replace(tmp, out)
        await asyncio.sleep(max(1, 30 - (time.time() - t)))
    for x in tasks:
        x.cancel()
    json.dump({"coins": state["coins"], "kw": state["kw"]}, open(out, "w"))


if __name__ == "__main__":
    asyncio.run(main())
