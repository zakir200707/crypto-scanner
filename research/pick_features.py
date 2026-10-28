"""Which of the scanner's readings separated its winning picks from the losers?

Joins the first BEST PICK block of every coin in results/scanlogs/*.txt with its result under
the alert plan (research/score_scanner.py output, results/scanner_score2.txt), then compares
winners and losers feature by feature. Exploratory: a small sample, so any rule found here has
to prove itself on new picks before it is trusted.

  python research/pick_features.py results/scanner_score2.txt
"""

import glob
import re
import statistics
import sys

NUM = r"(-?[\d,.]+)"
FIELDS = {
    "score": r"score ([\d.]+)/100",
    "mc_k": r"MC \$([\d.]+)K",
    "mc_m": r"MC \$([\d.]+)M",
    "liq_k": r"liq \$([\d.]+)K",
    "off_high": r"(-?\d+)% off 24h high",
    "chg24": r"24h (-?[\d.]+)%",
    "holders": r"holders (\d+)",
    "top1": r"top wallet ([\d.]+)%",
    "top10": r"top10 ([\d.]+)%",
    "insiders": r"insider wallets (\d+)",
    "lp_locked": r"LP locked ([\d.]+)%",
    "buyers": r"(\d+) buyers vs",
    "sellers": r"vs (\d+) sellers",
    "whale6": r"whale net 6h \$" + NUM,
    "whale24": r"24h \$" + NUM + r" \|",
    "accum": r"accumulators (\d+)",
    "smart": r"smart wallets (\d+)",
    "from_low": r"\+(\d+)% from 24h low",
    "vol_x": r"volume ([\d.]+)x",
    "pc2h": r"price ([+-]?\d+)% in 2h",
    "radar": r"radar (\d+)",
}


def blocks():
    first = {}
    for path in sorted(glob.glob("results/scanlogs/*.txt")):
        lines = [re.sub(r"^\S+Z ", "", l.rstrip("\n")) for l in open(path, encoding="utf-8", errors="replace")]
        for i, l in enumerate(lines):
            if not l.startswith("BEST PICK"):
                continue
            blk = "\n".join(lines[i:i + 14])
            m = re.search(r"mint\s+(\w+)", blk)
            if not m or m.group(1) in first:
                continue
            f = {"warns": len(re.findall(r"^\s+! ", blk, re.M)), "plus": len(re.findall(r"^\s+\+ ", blk, re.M))}
            for k, rx in FIELDS.items():
                x = re.search(rx, blk)
                if x:
                    f[k] = float(x.group(1).replace(",", ""))
            f["mc"] = f.pop("mc_k", 0) * 1e3 + f.pop("mc_m", 0) * 1e6
            f["liq"] = f.pop("liq_k", 0) * 1e3
            if f.get("buyers") is not None and f.get("sellers"):
                f["buy_ratio"] = f["buyers"] / f["sellers"]
            if f["mc"] and f["liq"]:
                f["liq_mc"] = f["liq"] / f["mc"]
            f["sym"] = re.search(r"BEST PICK: .*? \((.*?)\)\s+score", blk).group(1)
            first[m.group(1)] = f
    return first


def results(path):
    out = {}
    for l in open(path, encoding="utf-8"):
        m = re.match(r"\s+\d+ \d\d:\d\d (.{12}) score\s+([\d.]+) (ALERT|     ) ([+-]\d+)%", l)
        if m:
            out[(m.group(1).strip(), float(m.group(2)))] = (int(m.group(4)) / 100, m.group(3) == "ALERT")
    return out


def main():
    feats, res = blocks(), results(sys.argv[1])
    rows = []
    for f in feats.values():
        r = next((v for (s, sc), v in res.items() if s == f["sym"][:12].strip()), None)
        if r:
            rows.append({**f, "ret": r[0], "alerted": r[1]})
    win = [r for r in rows if r["ret"] > 0]
    lose = [r for r in rows if r["ret"] <= 0]
    print(f"{len(rows)} picks matched: {len(win)} winners, {len(lose)} losers (alert plan)\n")
    print(f"  {'feature':10s} {'winners median':>15s} {'losers median':>15s}")
    keys = ["score", "mc", "liq", "liq_mc", "off_high", "chg24", "holders", "top1", "top10", "insiders", "lp_locked",
            "buy_ratio", "whale6", "whale24", "accum", "smart", "from_low", "vol_x", "pc2h", "radar", "warns", "plus"]
    for k in keys:
        a = [r[k] for r in win if k in r]
        b = [r[k] for r in lose if k in r]
        if a and b:
            print(f"  {k:10s} {statistics.median(a):15.3g} {statistics.median(b):15.3g}   (n {len(a)}/{len(b)})")
    print("\nper pick:")
    for r in sorted(rows, key=lambda r: r["ret"]):
        print(f"  {r['ret']:+.0%} {'A' if r['alerted'] else ' '} {r['sym'][:10]:10s} sc {r.get('score', 0):4.0f} mc {r['mc']/1e3:6.0f}K "
              f"liq/mc {r.get('liq_mc', 0):.2f} top10 {r.get('top10', -1):4.1f} hold {r.get('holders', -1):5.0f} "
              f"b/s {r.get('buy_ratio', 0):.2f} w6 {r.get('whale6', 0):8.0f} low+ {r.get('from_low', -1):4.0f} "
              f"pc2h {r.get('pc2h', 0):+4.0f} vol {r.get('vol_x', 0):.1f} warn {r['warns']}")


if __name__ == "__main__":
    main()
