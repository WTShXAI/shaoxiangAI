"""
track_sample_growth.py — 验证台样本累积追踪（只读）

读 verification.db.verification_ledger，按 match_date（考卷时间轴）累计各 model_source
样本量，输出 CSV + JSON 趋势，并对照 G1 验收线（2500）给出距离。

安全：
- 全程只读：sqlite3 uri + PRAGMA query_only=1，绝不写入 verification.db。
- 不触碰 events.db，不杀进程，不跑 schema 变更。
- 默认库 = verification.db（幂法口径账本）；跨口径（比例法）行已不存在，不做混用。

用法：
  python scripts/track_sample_growth.py                # 默认库 + 默认 reports/ 输出
  python scripts/track_sample_growth.py --db X.db --out DIR
  python scripts/track_sample_growth.py --json-only    # 仅打印 JSON 摘要
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DEFAULT_DB = "verification.db"
DEFAULT_OUT = "reports"
G1_ACCEPT_N = 2500  # 盈利验证台 G1 样本验收线


def open_readonly(db_path: str) -> sqlite3.Connection:
    """以只读方式打开（uri + query_only 双保险）。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=1")
    con.row_factory = sqlite3.Row
    return con


def fetch_ledger(con: sqlite3.Connection):
    """返回 (rows, devig_methods)。只读 SELECT。"""
    cur = con.cursor()
    # 探测列存在性，避免旧库缺字段报错
    cur.execute("PRAGMA table_info(verification_ledger)")
    cols = {r["name"] for r in cur.fetchall()}
    need = {"model_source", "match_date", "is_credible", "devig_method"}
    if not need.issubset(cols):
        raise ValueError(f"verification_ledger 缺列: {need - cols}")
    cur.execute(
        "SELECT model_source, match_date, devig_method, is_credible "
        "FROM verification_ledger WHERE is_credible = 1"
    )
    return cur.fetchall()


def compute_trend(rows):
    """按 model_source 计算每日新增 + 累计（按 match_date 升序）。"""
    # (source, date) -> daily_n
    daily: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        src = r["model_source"]
        date = (r["match_date"] or "")[:10]
        daily[(src, date)] += 1

    sources = sorted({s for (s, _) in daily})
    trend: dict[str, list[dict]] = {s: [] for s in sources}
    cum: dict[str, int] = defaultdict(int)
    for (src, date) in sorted(daily.keys(), key=lambda k: (k[1], k[0])):
        cum[src] += daily[(src, date)]
        trend[src].append({
            "date": date,
            "daily_n": daily[(src, date)],
            "cum_n": cum[src],
        })

    # 总累计（含 unknown source 兜底）
    total_cum = 0
    for s in sources:
        total_cum = max(total_cum, cum[s])  # 各 source 独立样本，max 即账本总规模
    return trend, sources, dict(cum)


def build_summary(trend, cum, total_rows):
    """生成 JSON 摘要，含 G1 距离。"""
    per_source = {}
    for src, series in trend.items():
        latest = series[-1] if series else None
        per_source[src] = {
            "latest_date": latest["date"] if latest else None,
            "cum_n": cum[src],
            "g1_target": G1_ACCEPT_N,
            "gap_to_g1": max(0, G1_ACCEPT_N - cum[src]),
            "g1_reached": cum[src] >= G1_ACCEPT_N,
            "first_date": series[0]["date"] if series else None,
            "last_date": latest["date"] if latest else None,
            "n_days": len(series),
        }
    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": "只读追踪 verification.db 幂法口径账本；样本按 match_date 累计(k考卷时间轴)",
        "total_credible_rows": total_rows,
        "g1_accept_n": G1_ACCEPT_N,
        "by_source": per_source,
    }
    return summary


def write_outputs(trend, summary, out_dir, db_name):
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(db_name))[0]
    csv_path = os.path.join(out_dir, f"{stem}_sample_growth.csv")
    json_path = os.path.join(out_dir, f"{stem}_sample_growth.json")

    # CSV：date, model_source, daily_n, cum_n
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write("date,model_source,daily_n,cum_n\n")
        for src, series in trend.items():
            for row in series:
                f.write(f"{row['date']},{src},{row['daily_n']},{row['cum_n']}\n")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    return csv_path, json_path


def main():
    ap = argparse.ArgumentParser(description="验证台样本累积追踪（只读）")
    ap.add_argument("--db", default=DEFAULT_DB, help="verification.db 路径")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--json-only", action="store_true", help="仅打印 JSON 摘要不落盘")
    args = ap.parse_args()

    con = open_readonly(args.db)
    try:
        rows = fetch_ledger(con)
    finally:
        con.close()

    trend, sources, cum = compute_trend(rows)
    summary = build_summary(trend, cum, len(rows))

    if args.json_only:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        csv_path, json_path = write_outputs(trend, summary, args.out, args.db)
        print(f"[OK] rows={len(rows)} sources={sources}")
        for src, info in summary["by_source"].items():
            flag = "REACHED" if info["g1_reached"] else f"gap={info['gap_to_g1']}"
            print(f"  {src:18s} cum={info['cum_n']:5d}  G1(2500) {flag}  span={info['first_date']}->{info['last_date']}")
        print(f"[out] {csv_path}")
        print(f"[out] {json_path}")

    return summary


if __name__ == "__main__":
    main()
