"""Copy the live watcher's snapshots of graduated coins out of its cloud cache for offline study.
Runs in research-run.yml, which restores the cache read-only. Rows from before graduation are dropped.

  python research/export_snaps.py out/snaps.jsonl.gz
"""

import glob
import gzip
import json
import sys

n = rows = 0
coins = set()
with gzip.open(sys.argv[1], "wt", encoding="utf-8") as out:
    for p in glob.glob("state/snaps/*.jsonl*"):
        n += 1
        op = gzip.open if p.endswith(".gz") else open
        try:
            with op(p, "rt", encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("grad") and r["t"] >= r["grad"]:
                        out.write(line.rstrip("\n") + "\n")
                        rows += 1
                        coins.add(r["mint"])
        except (OSError, EOFError):
            continue
print(f"{n} files, {rows} rows, {len(coins)} graduated coins")
