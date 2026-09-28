#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
track_db_size_drift.py — 数据资产体积漂移巡检（只读 stat，绝不开库写）

用途（T23 / backlog）：
  列出 D:/Architecture/data/*.db 与 archive/*.db，对每个 db 仅用 os.path.getsize / getmtime
  做**文件系统 stat**（绝不 open / 绝不写入），输出累积趋势：
    - reports/db_size_drift.csv  （每次快照追加一行/dbn）
    - reports/db_size_drift.json （最新快照 + 历史 + 漂移判定）
  漂移检测目标：
    1. events.db 单次快照突增 → 采集异常信号（存储暴涨/异常回放）
    2. archive/ 总体积异常膨胀 → 归档失控 / 误写生产

红线合规：
  - 全程只读 stat，永不 open 任何 *.db，绝不写入/删除/杀进程/schema 变更。
  - 唯一落盘 = reports/db_size_drift.{csv,json}，不碰 events.db 等生产数据。
  - 可安全每 10 分钟运行（stat 对 35GB 主库也只 syscalls，无 I/O 负担）。

用法：
  python scripts/track_db_size_drift.py                 # 默认扫 data + archive
  python scripts/track_db_size_drift.py --dirs data archive --out reports
  python scripts/track_db_size_drift.py --json-only     # 只写 json
  python scripts/track_db_size_drift.py --reset         # 清空历史重新累积（仍只读 stat）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

# ---- 漂移阈值（单次快照，单位 MB）-----------------------------------------
DEFAULT_THRESHOLDS = {
    "events_spike_mb": 5000,   # events.db 单次快照增长 >5GB → 采集异常信号
    "archive_spike_mb": 2000,  # archive 总增长 >2GB → 归档失控/误写
}
# 生产主库（仅用于命名告警，不做任何特殊处理/写入）
WATCH_PRODUCTION = {"events.db"}
# 扫描引用的源文件扩展名
DB_EXT = ".db"


def collect_snapshot(scan_dirs: list[str]) -> tuple[list[dict], dict]:
    """对每个 scan_dir 递归列出 *.db，仅 stat（getsize/getmtime）。

    返回 (dbs, agg)：
      dbs  = [{name, path, size_bytes, size_mb, mtime}]
      agg  = {events_size_mb, archive_total_mb, total_mb, events_size_bytes}
    不 open 任何文件。
    """
    dbs: list[dict] = []
    for d in scan_dirs:
        root = os.path.abspath(d)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if not fn.lower().endswith(DB_EXT):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(p)
                    mtime = os.path.getmtime(p)
                except OSError:
                    size, mtime = -1, -1
                dbs.append({
                    "name": fn,
                    "path": p,
                    "size_bytes": size,
                    "size_mb": round(size / (1024 * 1024), 3) if size >= 0 else None,
                    "mtime": mtime,
                })
    dbs.sort(key=lambda x: x["name"])

    events_size_bytes = sum(
        d["size_bytes"] for d in dbs
        if d["name"] in WATCH_PRODUCTION and d["size_bytes"] >= 0
    )
    archive_total = sum(
        d["size_bytes"] for d in dbs
        if "archive" in os.path.normcase(d["path"]).split(os.sep) and d["size_bytes"] >= 0
    )
    total = sum(d["size_bytes"] for d in dbs if d["size_bytes"] >= 0)
    agg = {
        "events_size_bytes": events_size_bytes,
        "events_size_mb": round(events_size_bytes / (1024 * 1024), 3),
        "archive_total_mb": round(archive_total / (1024 * 1024), 3),
        "total_mb": round(total / (1024 * 1024), 3),
    }
    return dbs, agg


def compute_size_delta(current: list[dict], previous: list[dict]) -> dict:
    """计算 current 相对 previous 的每 db 体积增量（MB / bytes）。"""
    prev_map = {d["name"]: d for d in previous}
    deltas: dict[str, dict] = {}
    for d in current:
        name = d["name"]
        cur = d["size_bytes"]
        if cur < 0:
            continue
        prev = prev_map.get(name)
        prev_bytes = prev["size_bytes"] if (prev and prev.get("size_bytes", -1) >= 0) else None
        if prev_bytes is None:
            delta_bytes = None
        else:
            delta_bytes = cur - prev_bytes
        deltas[name] = {
            "delta_bytes": delta_bytes,
            "delta_mb": round(delta_bytes / (1024 * 1024), 3) if delta_bytes is not None else None,
            "previous_mb": round(prev_bytes / (1024 * 1024), 3) if prev_bytes is not None else None,
        }
    return deltas


def detect_anomalies(deltas: dict, agg_now: dict, agg_prev: dict | None,
                     thresholds: dict) -> dict:
    """输出 drift 判定。events / archive 各给 spike 标记 + 全库 Top 增幅。"""
    drift: dict = {"events": None, "archive": None, "top_movers": []}

    ev = deltas.get("events.db")
    if ev and ev["delta_mb"] is not None:
        drift["events"] = {
            "delta_mb": ev["delta_mb"],
            "previous_mb": ev["previous_mb"],
            "current_mb": round(agg_now["events_size_mb"], 3),
            "anomaly": ev["delta_mb"] > thresholds["events_spike_mb"],
            "threshold_mb": thresholds["events_spike_mb"],
        }

    if agg_prev is not None:
        arch_delta_mb = round(agg_now["archive_total_mb"] - agg_prev.get("archive_total_mb", 0.0), 3)
        drift["archive"] = {
            "delta_mb": arch_delta_mb,
            "current_mb": agg_now["archive_total_mb"],
            "previous_mb": agg_prev.get("archive_total_mb", 0.0),
            "anomaly": arch_delta_mb > thresholds["archive_spike_mb"],
            "threshold_mb": thresholds["archive_spike_mb"],
        }

    # Top 增幅（取正增量前 5，绝对 MB）
    movers = [
        {"name": n, "delta_mb": v["delta_mb"]}
        for n, v in deltas.items()
        if v["delta_mb"] is not None and v["delta_mb"] > 0
    ]
    movers.sort(key=lambda x: x["delta_mb"], reverse=True)
    drift["top_movers"] = movers[:5]
    return drift


def build_report(dbs: list[dict], agg: dict, deltas: dict, drift: dict,
                 history: list[dict], scan_dirs: list[str], thresholds: dict) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = {
        "timestamp": ts,
        "events_size_mb": agg["events_size_mb"],
        "archive_total_mb": agg["archive_total_mb"],
        "total_mb": agg["total_mb"],
    }
    new_history = history + [snap]
    return {
        "generated_at": ts,
        "note": "只读 stat 巡检 data/archive *.db 体积；绝不开库写；events.db 突增=采集异常信号",
        "scan_dirs": [os.path.abspath(d) for d in scan_dirs],
        "thresholds_mb": thresholds,
        "latest_snapshot": {
            "timestamp": ts,
            "dbs": dbs,
            "events_size_mb": agg["events_size_mb"],
            "archive_total_mb": agg["archive_total_mb"],
            "total_mb": agg["total_mb"],
        },
        "history": new_history,
        "deltas": deltas,
        "drift": drift,
    }


def snapshot_rows(dbs: list[dict], ts: str) -> list[list]:
    """CSV 行：[timestamp, name, size_bytes, size_mb, mtime]。"""
    rows = []
    for d in dbs:
        rows.append([
            ts,
            d["name"],
            d["size_bytes"],
            d["size_mb"] if d["size_mb"] is not None else "",
            d["mtime"],
        ])
    return rows


def write_outputs(dbs: list[dict], report: dict, out_dir: str, json_only: bool) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    written: dict[str, str] = {}

    json_path = os.path.join(out_dir, "db_size_drift.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    written["json"] = json_path

    if not json_only:
        csv_path = os.path.join(out_dir, "db_size_drift.csv")
        ts = report["latest_snapshot"]["timestamp"]
        file_exists = os.path.exists(csv_path)
        with open(csv_path, "a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if not file_exists:
                w.writerow(["timestamp", "name", "size_bytes", "size_mb", "mtime"])
            for row in snapshot_rows(dbs, ts):
                w.writerow(row)
        written["csv"] = csv_path
    return written


def load_history(json_path: str) -> tuple[list[dict], list[dict] | None]:
    """读取上次 json 的历史段；返回 (history, previous_snapshot_dbs)。"""
    if not os.path.exists(json_path):
        return [], None
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            prev = json.load(fh)
        history = prev.get("history", [])
        prev_dbs = prev.get("latest_snapshot", {}).get("dbs", [])
        return history, prev_dbs
    except (json.JSONDecodeError, OSError):
        return [], None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="数据资产体积漂移巡检（只读 stat）")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--dirs", nargs="+", default=["data", "archive"],
                    help="要扫描的目录（默认 data archive）")
    ap.add_argument("--out", default=os.path.join(here, "reports"))
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="清空历史重新累积（仍只读 stat，不删任何 db）")
    ap.add_argument("--events-spike-mb", type=float, default=DEFAULT_THRESHOLDS["events_spike_mb"])
    ap.add_argument("--archive-spike-mb", type=float, default=DEFAULT_THRESHOLDS["archive_spike_mb"])
    args = ap.parse_args(argv)

    thresholds = {
        "events_spike_mb": args.events_spike_mb,
        "archive_spike_mb": args.archive_spike_mb,
    }

    json_path = os.path.join(args.out, "db_size_drift.json")
    if args.reset and os.path.exists(json_path):
        os.remove(json_path)

    history, prev_dbs = ([], None) if args.reset else load_history(json_path)

    dbs, agg = collect_snapshot(args.dirs)
    deltas = compute_size_delta(dbs, prev_dbs) if prev_dbs else {}
    agg_prev = history[-1] if history else None
    drift = detect_anomalies(deltas, agg, agg_prev, thresholds)
    report = build_report(dbs, agg, deltas, drift, history, args.dirs, thresholds)

    written = write_outputs(dbs, report, args.out, args.json_only)

    # 控制台摘要
    print(f"[db_size_drift] 扫描 db 数={len(dbs)} 总体积={agg['total_mb']} MB")
    print(f"  events.db={agg['events_size_mb']} MB  archive 总={agg['archive_total_mb']} MB")
    ev = drift.get("events")
    if ev:
        flag = "⚠ANOMALY" if ev["anomaly"] else "ok"
        print(f"  events 漂移={ev['delta_mb']} MB [{flag}]")
    ar = drift.get("archive")
    if ar:
        flag = "⚠ANOMALY" if ar["anomaly"] else "ok"
        print(f"  archive 漂移={ar['delta_mb']} MB [{flag}]")
    if drift.get("top_movers"):
        print("  Top 增幅: " + ", ".join(f"{m['name']}(+{m['delta_mb']}MB)" for m in drift["top_movers"]))
    print(f"  报告已写: {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
