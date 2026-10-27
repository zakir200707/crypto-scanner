"""Live test: what happens to a coin's price after a public Telegram call channel posts it?

Reads each channel's public web preview (https://t.me/s/<channel>) every POLL seconds, pulls
Solana contract addresses out of new posts, and prices each called coin on DexScreener every
30s for TRACK_MIN minutes from the moment we saw the post. Score with:
  python research/score_events.py results/telegram.json telegram

  python research/telegram_test.py channels.txt results/telegram.json 150
"""

import html
import json
import os
import re
import sys
import time
import urllib.request

DEX = "https://api.dexscreener.com/tokens/v1/solana/"
POLL = 30
TRACK_MIN = 120
ADDR = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
NOT_COINS = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "ignore")


def posts(channel):
    """[(post_id, unix_time, text)] from the channel's public preview page."""
    page = fetch(f"https://t.me/s/{channel}")
    out = []
    for block in page.split('class="tgme_widget_message_wrap')[1:]:
        pid = re.search(r'data-post="[^/]+/(\d+)"', block)
        ts = re.search(r'<time datetime="([^"]+)"', block)
        txt = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
        if pid and ts:
            text = html.unescape(re.sub(r"<[^>]+>", " ", txt.group(1))) if txt else ""
            links = " ".join(re.findall(r'href="([^"]+)"', block))
            t = time.mktime(time.strptime(ts.group(1)[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            out.append((int(pid.group(1)), t, text + " " + links))
    return out


def main():
    chans = [l.strip() for l in open(sys.argv[1]) if l.strip() and not l.startswith("#")]
    out, minutes = sys.argv[2], float(sys.argv[3])
    last_id = {}
    for ch in chans:  # only posts made while we watch count
        try:
            p = posts(ch)
            last_id[ch] = max((x[0] for x in p), default=0)
            print(ch, "ok, last post", last_id[ch], flush=True)
        except Exception as e:
            print(ch, "failed", e, flush=True)
    coins = {}
    end, k = time.time() + minutes * 60, 0
    while time.time() < end:
        t0 = time.time()
        for ch in chans:
            if ch not in last_id:
                continue
            try:
                for pid, ts, text in posts(ch):
                    if pid <= last_id[ch]:
                        continue
                    last_id[ch] = max(last_id[ch], pid)
                    for a in set(ADDR.findall(text)) - NOT_COINS:
                        key = f"{ch}:{a}"
                        if key not in coins:
                            coins[key] = {"t": time.time(), "post_t": ts, "channel": ch, "mint": a, "kind": ch, "snaps": []}
                            print(time.strftime("%H:%M:%S"), ch, a, flush=True)
            except Exception as e:
                print("poll error", ch, e, file=sys.stderr, flush=True)
        live = [c for c in coins.values() if time.time() - c["t"] < TRACK_MIN * 60]
        mints = sorted({c["mint"] for c in live})
        for i in range(0, len(mints), 30):
            try:
                pairs = json.loads(fetch(DEX + ",".join(mints[i:i + 30])))
            except Exception:
                continue
            best = {}
            for p in pairs:
                a = p["baseToken"]["address"]
                key = (p.get("dexId") != "pumpfun", (p.get("liquidity") or {}).get("usd") or 0)
                if a not in best or key > best[a][0]:
                    best[a] = (key, p)
            now = round(time.time())
            for c in live:
                if c["mint"] in best:
                    p = best[c["mint"]][1]
                    c["snaps"].append({"t": now, "price": float(p.get("priceUsd") or 0), "mc": p.get("marketCap") or 0})
        k += 1
        if k % 4 == 0:
            json.dump(coins, open(out + ".tmp", "w"))
            os.replace(out + ".tmp", out)
        time.sleep(max(1, POLL - (time.time() - t0)))
    json.dump(coins, open(out, "w"))


if __name__ == "__main__":
    main()
