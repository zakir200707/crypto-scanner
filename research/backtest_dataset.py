"""Do holder, bundler, sniper or smart-money numbers at detection pick winners?
Data: ian05012/solana-memecoin-dataset (10-29 June 2026). Its return labels are empty, so every trade is
rebuilt here from its 15-minute candles. One trade per token, at its first snapshot.

Entry = open of the first candle after the snapshot + 2%, at most 1h later. A stop fills at the stop or at
the candle's open if it gapped through, and comes first when a candle hits both. Costs 3.7%.
Thresholds come from the train days only (before 23 June) and each rule is judged on the later days.
The same rules on shuffled features show how many pass by luck. Only 47% of snapshots have candles, so
each rule's candle coverage is printed: returns of a rule that mostly picks covered coins can't be trusted.

  python -I research/backtest_dataset.py tokens.parquet ohlcv.parquet results/dataset_backtest.json
"""

import json
import sys
from collections import defaultdict

import numpy as np
import pyarrow.parquet as pq

COST = 0.037
STEP = 900
MIN_LIQ = 10_000
MIN_N = 30
CUT = 1782172800  # 23 June 00:00 UTC
EXITS = {"TP30/SL20/2h": (0.3, 0.2, 2), "TP50/SL25/24h": (0.5, 0.25, 24), "TP100/SL35/48h": (1.0, 0.35, 48)}
AGES = {"under 10m": (0, 600), "10m-1h": (600, 3600), "1h+": (3600, 1e12)}
# Address-book columns may have been built from the whole period, so a pass on them is not trusted.
SUSPECT = ("known_smart_count", "known_insider_count")
FEATURES = ("market_cap_usd liquidity_usd volume_h24 holder_count time_since_launch_sec top10_holder_pct "
            "rat_position_pct sniper_count dev_remaining_pct new_wallet_count smart_money_count smart_money_pct "
            "lp_burned_pct price_change_1h price_change_6h bundler_ratio top_holder_ratio smart_buy_1h kol_buy_1h "
            "buy_usd_1h sell_usd_1h smart_concurrent_15m unique_buyers unique_sellers flipper_ratio large_buy_streak "
            "seller_loss_ratio trade_buy_sell_ratio net_flow_usd insider_cluster_size insider_holding_ratio "
            "insider_net_usd cluster_count cluster_max_pct cluster_top3_pct cluster_newest_days bundler_pct_holders "
            "fresh_wallet_pct insider_pct_holders only_buy_pct low_freq_pct kol_pct_holders smart_pct_holders "
            "sniper_pct_holders fishing_pct profitable_pct pool_pct smart_ratio early_smart_ratio smart_earliness "
            "crew_cohesion buy_late buy_last10 n_late buyers_late smart_entry_rec has_twitter has_website "
            "has_telegram is_boosted known_smart_count known_insider_count").split()


def load(tp, op):
    t = pq.read_table(tp)
    o = pq.read_table(op)
    cols = {n: np.array(t[n].to_pylist(), dtype=float) for n in FEATURES}
    tok = t["token_address"].to_pylist()
    cap = np.array([x.timestamp() for x in t["captured_at"].to_pylist()])
    first = {}
    for i in np.argsort(cap):
        first.setdefault(tok[i], int(i))
    idx = np.array(sorted(first.values()))
    idx = idx[cols["liquidity_usd"][idx] >= MIN_LIQ]
    c = defaultdict(list)
    for r in zip(*[o[n].to_pylist() for n in ("token_address", "ts", "open", "high", "low", "close")]):
        c[r[0]].append(r[1:])
    for k in c.values():
        k.sort()
    return {n: v[idx] for n, v in cols.items()}, [tok[i] for i in idx], cap[idx], c


def trade(k, cap, tp, sl, hold):
    bars = [x for x in k if cap < x[0] <= cap + hold * 3600 + 3600]
    if not bars or bars[0][0] > cap + 3600 or bars[0][1] <= 0:
        return None
    e = bars[0][1] * 1.02
    t0 = bars[0][0]
    stop, target = e * (1 - sl), e * (1 + tp)
    last = bars[0][4]
    for ts, o, h, l, c in bars:
        if ts > t0 + hold * 3600:
            break
        if h > c * 10:  # the dataset's own warning: glitch wicks
            h = max(o, c)
        if l <= stop:
            return min(stop, o) / e - 1 - COST
        if h >= target:
            return tp - COST
        last = c
    return last / e - 1 - COST


def rule_masks(v, train):
    """Two tails of a feature, cut at the train quintiles; a yes/no feature gives yes and no."""
    x = v[train & np.isfinite(v)]
    if len(x) < 100:
        return {}
    vals = np.unique(x)
    if len(vals) <= 2:
        return {f"= {a:g}": lambda u, a=a: u == a for a in vals}
    lo, hi = np.percentile(x, [20, 80])
    out = {}
    if hi > vals[0]:
        out[f">= {hi:.4g}"] = lambda u, b=hi: u >= b
    if lo < vals[-1]:
        out[f"<= {lo:.4g}"] = lambda u, b=lo: u <= b
    return out


def stats(y, m):
    v = y[m & np.isfinite(y)]
    return (float(v.mean()), len(v)) if len(v) else (0.0, 0)


def run_rules(cols, y, train, has, base):
    """Every feature tail under one exit: [(name, rule, train mean, n, test mean, n, coverage)]."""
    test = ~train
    rows = []
    for n, v in cols.items():
        for name, f in rule_masks(v, train).items():
            m = f(v)
            a, na = stats(y, m & train)
            b, nb = stats(y, m & test)
            rows.append((n, name, a, na, b, nb, float(has[m].mean()) if m.any() else 0.0))
    ok = [r for r in rows if r[3] >= MIN_N and r[5] >= MIN_N and r[2] > 0 and r[4] > 0
          and r[2] > base[0] and r[4] > base[1] and r[0] not in SUSPECT]
    return rows, ok


def ridge(cols, y, train):
    """All features at once on ranks, fitted on train; returns a score for every coin."""
    names = [n for n in cols if n not in SUSPECT]
    X = []
    for n in names:
        v = cols[n]
        s = np.sort(v[train & np.isfinite(v)])
        r = np.searchsorted(s, np.nan_to_num(v, nan=np.median(s))) / max(len(s), 1) - 0.5
        X.append(r)
    X = np.column_stack(X + [np.ones(len(y))])
    m = train & np.isfinite(y)
    w = np.linalg.solve(X[m].T @ X[m] + 50 * np.eye(X.shape[1]), X[m].T @ np.clip(y[m], -1, 2))
    return X @ w


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cols, tok, cap, c = load(sys.argv[1], sys.argv[2])
    train = cap < CUT
    has = np.array([t in c for t in tok])
    print(f"{len(tok)} tokens with ${MIN_LIQ:,}+ liquidity, {has.mean():.0%} have candles "
          f"(train {has[train].mean():.0%}, test {has[~train].mean():.0%})")
    rng = np.random.default_rng(1)
    out = {}
    for en, (tp, sl, hold) in EXITS.items():
        y = np.array([np.nan if t not in c or (r := trade(c[t], x, tp, sl, hold)) is None else r for t, x in zip(tok, cap)])
        base = stats(y, train)[0], stats(y, ~train)[0]
        print(f"\n== {en}: random coin train {base[0]:+.1%} n={stats(y, train)[1]} | test {base[1]:+.1%} n={stats(y, ~train)[1]}, "
              f"win {(y[np.isfinite(y)] > 0).mean():.0%}, median {np.nanmedian(y):+.0%}")
        for an, (a0, a1) in AGES.items():
            m = (cols["time_since_launch_sec"] >= a0) & (cols["time_since_launch_sec"] < a1)
            print(f"   age {an:10s} train {stats(y, m & train)[0]:+.1%} n={stats(y, m & train)[1]} | "
                  f"test {stats(y, m & ~train)[0]:+.1%} n={stats(y, m & ~train)[1]}")
        rows, ok = run_rules(cols, y, train, has, base)
        print(f"   {len(rows)} rules; {sum(r[3] >= MIN_N and r[5] >= MIN_N for r in rows)} with {MIN_N}+ trades in both; "
              f"{len(ok)} positive in both and better than random")
        for r in sorted(ok, key=lambda r: -r[4])[:12]:
            print(f"   {r[0]} {r[1]}: train {r[2]:+.1%} n={r[3]} | test {r[4]:+.1%} n={r[5]} | candles {r[6]:.0%}")
        print("   best on train, then test:")
        for r in sorted([r for r in rows if r[3] >= MIN_N and r[5] >= MIN_N and r[0] not in SUSPECT], key=lambda r: -r[2])[:6]:
            print(f"   {r[0]} {r[1]}: train {r[2]:+.1%} n={r[3]} -> test {r[4]:+.1%} n={r[5]} | candles {r[6]:.0%}")
        luck = []
        for _ in range(200):
            p = rng.permutation(len(y))
            luck.append(len(run_rules({n: v[p] for n, v in cols.items()}, y, train, has[p], base)[1]))
        print(f"   shuffled features: {np.mean(luck):.1f} rules pass on average, 95th percentile {np.percentile(luck, 95):.0f}")
        s = ridge(cols, y, train)
        cut = np.percentile(s[train & np.isfinite(y)], 90)
        print(f"   all features together, top 10%: train {stats(y, train & (s >= cut))[0]:+.1%} n={stats(y, train & (s >= cut))[1]} | "
              f"test {stats(y, ~train & (s >= cut))[0]:+.1%} n={stats(y, ~train & (s >= cut))[1]}")
        out[en] = {"base": base, "passed": ok, "luck_mean": float(np.mean(luck)), "luck_p95": float(np.percentile(luck, 95))}
    json.dump(out, open(sys.argv[3], "w"))


main()
