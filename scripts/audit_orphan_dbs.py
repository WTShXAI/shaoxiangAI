#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_orphan_dbs.py — data/*.db 零引用 / 进程持有 只读审计（仅报告，不改动任何数据）。

用途（T04 / backlog B7）：
  列出 D:/Architecture/data/*.db，对每个 db 做两件事：
    1. 进程持有检测 —— 哪些运行中的进程正打开该 db 文件（用 psutil，best-effort，绝不 kill）。
    2. 代码引用 grep —— 代码库哪些源文件引用了该 db 文件名（判定是否"零引用"）。
  据此产出"归档安全清单"：既无代码引用、又无进程持有、且非已知生产资产的 db，
  才标记为 ARCHIVE_CANDIDATE；其余标注 IN_USE / PRODUCTION_KEEP。

红线合规：
  - 完全只读：只读取文件、扫描进程，绝不做任何写入 / 删除 / 杀进程 / schema 变更。
  - 唯一落盘 = 审计报告（reports/audit_orphan_dbs.{json,md}），不碰 events.db 等生产数据。
  - 进程持有检测只读取，不发送任何信号（无 kill / 无 terminate）。

用法：
  python -m scripts.audit_orphan_dbs                # 默认 data=./data, repo=./, 写 reports/
  python scripts/audit_orphan_dbs.py --data-dir X --repo Y --out-dir Z
  python scripts/audit_orphan_dbs.py --json-only    # 只写 json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# ---- 已知生产资产（绝不标记为可归档）---------------------------------------
# 来源：working_memory（events.db / GQ.db / football_data.db / indep_features_gq.db /
#       shaoxiang_feature_library.db / efootball.db），以及本系统自有工具产出库。
PROTECTED_DBS = {
    "events.db",                 # 生产赔率主源 + 滚球实时（35.85GB，零写入）
    "GQ.db",                     # GQ 线（6.20GB，OU 线嵌 market 名，干净真相源）
    "football_data.db",          # 历史底座（~31 万场）
    "indep_features_gq.db",      # 13 独立特征
    "shaoxiang_feature_library.db",  # 特征库 3275 场
    "efootball.db",              # 电子盘口探针独立库（零接触 events.db）
    "verification.db",           # 盈利验证台只读落库
    "schedule_density.db",       # T03 离线派生隔离库
}

# 扫描引用的源文件扩展名（缩小范围，避开 14725 文件的 frontend 等）
SCAN_EXTS = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".txt", ".cfg"}
# 跳过的目录（大 / 非源码 / 数据本身）
SKIP_DIRS = {
    "data", "archive", "node_modules", ".git", ".venv", "__pycache__",
    "frontend", "backups", "history", "deliverables", ".workbuddy",
}


def list_dbs(data_dir: str) -> list[dict]:
    """列出 data_dir 下所有 *.db（含子目录），返回 {path,name,size_bytes}。"""
    out = []
    root = os.path.abspath(data_dir)
    if not os.path.isdir(root):
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        # 不递归进已知数据/大目录，避免扫进 events.db 自身等无谓遍历
        if os.path.basename(dirpath) in SKIP_DIRS and dirpath != root:
            continue
        for fn in filenames:
            if fn.lower().endswith(".db"):
                p = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = -1
                out.append({"path": p, "name": fn, "size_bytes": size})
    out.sort(key=lambda d: d["name"])
    return out


def find_references(repo_root: str, db_names: set[str]) -> dict[str, list[dict]]:
    """在 repo_root 源文件中 grep 每个 db 文件名（basename）出现的位置。

    返回 {db_name: [{file, count, sample}]}。sample 取首个匹配行（截断）。
    只扫 SCAN_EXTS 且跳过 SKIP_DIRS，避免误伤 data/ 自身与超大前端目录。
    """
    result: dict[str, list[dict]] = {n: [] for n in db_names}
    root = os.path.abspath(repo_root)
    if not os.path.isdir(root):
        return result
    for dirpath, dirnames, filenames in os.walk(root):
        # 就地剪枝
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SCAN_EXTS:
                continue
            fpath = os.path.join(dirpath, fn)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            for name in db_names:
                cnt = 0
                sample = ""
                for ln in lines:
                    if name in ln:
                        cnt += 1
                        if not sample:
                            sample = ln.strip()[:160]
                if cnt:
                    rel = os.path.relpath(fpath, root)
                    result[name].append({"file": rel, "count": cnt, "sample": sample})
    return result


def find_holders(db_path: str) -> list[dict]:
    """best-effort 检测哪些进程正持有该 db 文件（只读，绝不 kill）。

    优先 psutil.process_iter + open_files()。任一进程枚举失败（AccessDenied 等）
    静默跳过，整体返回已能识别的持有者。无 psutil 时返回 [] 并标记 unknown。
    """
    holders: list[dict] = []
    try:
        import psutil  # type: ignore
    except Exception:
        return holders
    target = os.path.normcase(os.path.abspath(db_path))
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                files = proc.open_files()
            except Exception:
                continue
            for f in files:
                if os.path.normcase(f.path) == target:
                    holders.append({"pid": proc.info["pid"], "name": proc.info["name"]})
                    break
    except Exception:
        pass
    return holders


def classify(name: str, refs: list[dict], holders: list[dict]) -> str:
    """判定归档建议类别。"""
    if name in PROTECTED_DBS:
        return "PRODUCTION_KEEP"
    if holders:
        return "IN_USE_HELD"
    if refs:
        return "IN_USE_REFERENCED"
    return "ARCHIVE_CANDIDATE"


def build_report(data_dir: str, repo_root: str) -> dict:
    dbs = list_dbs(data_dir)
    names = {d["name"] for d in dbs}
    refs = find_references(repo_root, names)
    entries = []
    for d in dbs:
        holders = find_holders(d["path"])
        status = classify(d["name"], refs.get(d["name"], []), holders)
        ref_list = refs.get(d["name"], [])
        entries.append({
            "name": d["name"],
            "path": d["path"],
            "size_mb": round(d["size_bytes"] / (1024 * 1024), 3) if d["size_bytes"] >= 0 else None,
            "status": status,
            "ref_count": sum(r["count"] for r in ref_list),
            "ref_files": ref_list,
            "held_by": holders,
        })
    archive_candidates = [e["name"] for e in entries if e["status"] == "ARCHIVE_CANDIDATE"]
    protected = [e["name"] for e in entries if e["status"] == "PRODUCTION_KEEP"]
    in_use = [e["name"] for e in entries if e["status"].startswith("IN_USE")]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": os.path.abspath(data_dir),
        "repo_root": os.path.abspath(repo_root),
        "total_dbs": len(entries),
        "summary": {
            "archive_candidates": archive_candidates,
            "production_keep": protected,
            "in_use": in_use,
        },
        "entries": entries,
    }


def _md_table(report: dict) -> str:
    lines = ["# data/*.db 零引用 / 进程持有 审计报告", ""]
    lines.append(f"- 生成时间(UTC): {report['generated_at']}")
    lines.append(f"- 扫描 data 目录: `{report['data_dir']}`")
    lines.append(f"- 代码库根: `{report['repo_root']}`")
    lines.append(f"- 检出 db 总数: {report['total_dbs']}")
    s = report["summary"]
    lines.append(f"- 可归档候选: {len(s['archive_candidates'])}  ·  生产保留: {len(s['production_keep'])}  ·  使用中: {len(s['in_use'])}")
    lines.append("")
    lines.append("## 归档安全清单（ARCHIVE_CANDIDATE）")
    lines.append("")
    if s["archive_candidates"]:
        lines.append("| db 名 | 大小(MB) |")
        lines.append("|------|---------|")
        for e in report["entries"]:
            if e["status"] == "ARCHIVE_CANDIDATE":
                lines.append(f"| {e['name']} | {e['size_mb']} |")
    else:
        lines.append("（无 —— 所有 db 均有引用或进程持有或为生产资产）")
    lines.append("")
    lines.append("## 生产保留（PRODUCTION_KEEP，红线保护，不可归档）")
    lines.append("")
    lines.append("| db 名 | 大小(MB) |")
    lines.append("|------|---------|")
    for e in report["entries"]:
        if e["status"] == "PRODUCTION_KEEP":
            lines.append(f"| {e['name']} | {e['size_mb']} |")
    lines.append("")
    lines.append("## 使用中（IN_USE，有引用/被进程持有，暂不归档）")
    lines.append("")
    lines.append("| db 名 | 状态 | 引用文件数 | 持有进程 |")
    lines.append("|------|------|-----------|---------|")
    for e in report["entries"]:
        if e["status"].startswith("IN_USE"):
            procs = ",".join(f"{h['name']}({h['pid']})" for h in e["held_by"]) or "-"
            lines.append(f"| {e['name']} | {e['status']} | {len(e['ref_files'])} | {procs} |")
    lines.append("")
    lines.append("> 本报告为只读审计产物，未对任何 db 做写入/删除/杀进程操作。")
    return "\n".join(lines)


def write_report(report: dict, out_dir: str, json_only: bool = False) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "audit_orphan_dbs.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    written = {"json": json_path}
    if not json_only:
        md_path = os.path.join(out_dir, "audit_orphan_dbs.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(_md_table(report))
        written["md"] = md_path
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="data/*.db 零引用/进程持有 只读审计")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--data-dir", default=os.path.join(here, "data"))
    ap.add_argument("--repo", default=here)
    ap.add_argument("--out-dir", default=os.path.join(here, "reports"))
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args(argv)
    report = build_report(args.data_dir, args.repo)
    written = write_report(report, args.out_dir, args.json_only)
    s = report["summary"]
    print(f"[audit_orphan_dbs] 扫描 {report['total_dbs']} 个 db")
    print(f"  可归档候选({len(s['archive_candidates'])}): {s['archive_candidates']}")
    print(f"  生产保留({len(s['production_keep'])}): {s['production_keep']}")
    print(f"  使用中({len(s['in_use'])}): {s['in_use']}")
    print(f"  报告已写: {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
