"""数据资产基线快照 — P0 增长监控基线 + P2 资产盘点底稿

用法:
    D:/Architecture/.venv/Scripts/python.exe scripts/asset_snapshot.py            # 轻量(秒级)
    D:/Architecture/.venv/Scripts/python.exe scripts/asset_snapshot.py --deep     # 深度(含慢表 COUNT,数分钟)

输出:
    reports/asset_snapshots/YYYYMMDD.json   当日快照
    reports/asset_snapshots/series.jsonl    历史序列(追加,用于算增长率)

设计约束:
  - 只读打开数据库(uri mode=ro),绝不写业务库
  - 轻量模式只取「文件大小 + 最新时间戳 + 快表行数」,慢表跳过
  - events.db 的 COUNT(*) 约 2.5 分钟,仅在 --deep 时执行
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "reports", "asset_snapshots")

# (逻辑名, 相对路径)  —— 仅登记有资产价值的主库
DBS = [
    ("events",        "data/events.db"),
    ("gq",            "data/GQ.db"),
    ("football_data", "data/football_data.db"),
    ("rollball_train", "data/rollball_training.db"),
    ("hist_feature",  "data/hist_feature_matrix.db"),
    ("leisu_odds",    "data/leisu_odds.db"),
]

# 各库中要跟踪「最新时间戳」的表与列(监控数据是否活着)
LIVENESS = {
    "events": [
        ("odds_snapshots", "captured_at"),
        ("odds_changes",   "captured_at"),
        ("matches",        "last_seen"),
        ("prediction_ledger", "created_at"),
    ],
    "gq": [
        ("matches", "last_seen"),
        ("match_outcomes", "captured_at"),
    ],
    "football_data": [
        ("matches", "kickoff"),
        ("historical_matches", "kickoff"),
    ],
}

# 快表(COUNT 秒级);慢表仅在 --deep 时统计
COUNT_FAST = {
    "events": ["matches", "match_outcomes", "prediction_ledger", "match_meta",
               "match_analysis_cache", "cs_verification", "prematch_conclusion"],
    "gq": ["matches", "match_outcomes", "cs_supplement", "pre_match_cs",
           "cs_verification", "prematch_conclusion", "match_analysis_cache"],
    "football_data": ["matches", "historical_matches", "william_ht", "odds_features",
                      "teams", "team_canonical", "live_odds_raw", "handicap_labels",
                      "interwetten_odds", "ou_validation_local"],
}
COUNT_SLOW = {
    "events": ["odds_snapshots", "odds_changes"],
}


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}PB"


def ro_conn(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def table_exists(cur, t):
    r = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone()
    return r is not None


def snap_liveness(cur, t, col):
    if not table_exists(cur, t):
        return None
    try:
        v = cur.execute(f'SELECT MAX("{col}") FROM "{t}"').fetchone()[0]
        return v
    except Exception:
        return None


def snap_count(cur, t, deep):
    if not table_exists(cur, t):
        return None
    try:
        return cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true", help="含慢表 COUNT(数分钟)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).astimezone()
    snap = {
        "snapshot_at": now.isoformat(),
        "snapshot_date": now.strftime("%Y%m%d"),
        "deep": args.deep,
        "dbs": {},
        "totals": {},
    }

    total_bytes = 0
    for name, rel in DBS:
        path = os.path.join(ROOT, rel)
        entry = {"path": rel, "exists": os.path.exists(path)}
        if not entry["exists"]:
            snap["dbs"][name] = entry
            continue

        size = os.path.getsize(path)
        entry["size_bytes"] = size
        entry["size_human"] = human(size)
        total_bytes += size

        # WAL / SHM 一并计入占用
        for suf in ("-wal", "-shm"):
            p2 = path + suf
            if os.path.exists(p2):
                total_bytes += os.path.getsize(p2)

        entry["liveness"] = {}
        entry["counts"] = {}
        try:
            c = ro_conn(path)
            cur = c.cursor()
            for t, col in LIVENESS.get(name, []):
                entry["liveness"][f"{t}.{col}"] = snap_liveness(cur, t, col)
            for t in COUNT_FAST.get(name, []):
                entry["counts"][t] = snap_count(cur, t, False)
            if args.deep:
                for t in COUNT_SLOW.get(name, []):
                    entry["counts"][t] = snap_count(cur, t, True)
            c.close()
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {str(e)[:120]}"

        snap["dbs"][name] = entry

    # 全盘 data/ 占用(含非 db 资产)
    data_dir = os.path.join(ROOT, "data")
    if os.path.isdir(data_dir):
        tot = 0
        for dp, _, fns in os.walk(data_dir):
            for f in fns:
                try:
                    tot += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        snap["totals"]["data_dir_bytes"] = tot
        snap["totals"]["data_dir_human"] = human(tot)

    snap["totals"]["main_dbs_bytes"] = total_bytes
    snap["totals"]["main_dbs_human"] = human(total_bytes)
    snap["totals"]["db_count"] = sum(1 for v in snap["dbs"].values() if v.get("exists"))

    os.makedirs(OUT_DIR, exist_ok=True)
    day_path = os.path.join(OUT_DIR, f"{snap['snapshot_date']}.json")
    with open(day_path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT_DIR, "series.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    # 控制台摘要
    print(f"[asset_snapshot] {snap['snapshot_at']}  deep={args.deep}")
    print(f"  主库 {snap['totals']['db_count']} 个 / {snap['totals']['main_dbs_human']}"
          f"  |  data/ 全量 {snap['totals'].get('data_dir_human', 'n/a')}")
    for name, e in snap["dbs"].items():
        if not e.get("exists"):
            print(f"  - {name:15s} [缺失]")
            continue
        live = e.get("liveness", {})
        mx = max([v for v in live.values() if isinstance(v, (int, float))], default=None)
        counts = e.get("counts", {})
        nmax = max([v for v in counts.values() if isinstance(v, int)], default=None)
        print(f"  - {name:15s} {e['size_human']:>10s}"
              f"  max_ts={mx if mx is not None else 'n/a'}"
              f"  max_count={nmax if nmax is not None else 'n/a'}")
    print(f"  -> {day_path}")


if __name__ == "__main__":
    main()
