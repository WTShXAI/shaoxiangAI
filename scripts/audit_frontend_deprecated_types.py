#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T06 B2 — 前端废弃类型只读 grep 审计。

目标：在 2026-09-18 预测系统改造（去喊单化）后，排查前端 src/ 与 e2e/ 中
对"旧喊单 / 投注决策 / 跨庄共识 / 训练-管理后台时代"类型的残留引用，
并分类判定：仍渲染 / 仅类型定义 / 孤儿(定义但无渲染) / 仅测试。

红线遵守：
- 纯只读 grep，不修改任何文件（含前端）。
- 不触碰 events.db、不杀进程、不跑 schema 变更。
- 输出两份报告：reports/audit_frontend_deprecated_types.json / .md。

用法：
  python scripts/audit_frontend_deprecated_types.py            # 跑真实前端 + 写报告
  python scripts/audit_frontend_deprecated_types.py --root DIR # 指定前端根
  python scripts/audit_frontend_deprecated_types.py --list-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# ── 候选废弃类型清单（按时代/语义归类）─────────────────────────────
# category:
#   shout      = 旧喊单/投注决策时代（去喊单化后仍可能残留语义）
#   crossbook  = 跨庄共识类（IR-32 红线，必须标记是否仍渲染）
#   training   = 训练/模型管理时代
#   admin      = 系统管理后台时代
#   legacy_pred= 旧预测口径（被新预测系统取代）
DEPRECATED = [
    # ── 旧喊单 / 投注决策时代 ──
    ("ModelComparison", "shout"),     # 模型对比 UI，改造后应已无入口
    ("ValueLayerRow", "shout"),
    ("value_layer", "shout"),         # 价值层（去喊单后仍作概率对照渲染）
    ("OperatorCard", "shout"),
    ("operator_card", "shout"),
    ("OperatorView", "shout"),        # 操盘手视角（喊单时代术语）
    ("StrategySignal", "shout"),
    ("operator_intent", "shout"),     # 操盘手意图
    ("operator_signals", "shout"),
    ("TerminalDecisionCard", "shout"),
    ("FixturesResponse", "legacy_pred"),
    ("FixturePrediction", "legacy_pred"),  # 旧 /fixtures/upcoming 预测口径
    # ── 跨庄共识（IR-32 红线）──
    ("MultibookConsensus", "crossbook"),
    ("multibook_consensus", "crossbook"),
    # ── 训练 / 模型管理时代 ──
    ("ModelVersion", "training"),
    ("TrainingStatus", "training"),
    ("TeamFeatures", "training"),
    # ── 系统管理后台时代 ──
    ("SystemHealth", "admin"),
    ("MetricsSummary", "admin"),
    ("Alert", "admin"),
    ("User", "admin"),
]

# 搜索范围
SCAN_DIRS = ["src", "e2e"]
SKIP_DIRS = {"node_modules", "dist", "build", ".cache", "coverage"}


def iter_files(root: Path):
    for d in SCAN_DIRS:
        base = root / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            if p.suffix in (".ts", ".tsx", ".js", ".jsx", ".json"):
                yield p


def audit_types(root: Path, deprecated=DEPRECATED):
    """返回结构化审计结果（纯函数，便于测试）。"""
    files = list(iter_files(root))
    # 预读全部文件内容
    contents: dict[Path, str] = {}
    for p in files:
        try:
            contents[p] = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            contents[p] = ""

    # 先找每个类型的"定义位置"
    def_pat = re.compile(r"export\s+(?:interface|type|class|enum)\s+(\w+)")
    defs: dict[str, Path] = {}
    for p, text in contents.items():
        for m in def_pat.finditer(text):
            defs.setdefault(m.group(1), p)

    results = []
    for ident, category in deprecated:
        # 词边界匹配（避免匹配子串，如 Alert 不匹配 Alerts）
        pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(ident) + r"(?![A-Za-z0-9_])")
        total = 0
        refs_by_file: dict[str, int] = defaultdict(int)
        render_refs = 0          # 出现在 .tsx 且非定义行的引用
        def_line_count = 0       # 出现在定义行的引用
        for p, text in contents.items():
            hits = pat.findall(text)
            if not hits:
                continue
            cnt = len(hits)
            total += cnt
            refs_by_file[str(p.relative_to(root))] += cnt
            if p.suffix == ".tsx":
                # 统计非定义行的命中（定义行形如 export interface X）
                for line in text.splitlines():
                    if pat.search(line):
                        if defs.get(ident) == p and re.search(
                            r"export\s+(?:interface|type|class|enum)\s+" + re.escape(ident), line
                        ):
                            def_line_count += 1
                        else:
                            render_refs += 1
        is_def = ident in defs
        # 状态判定
        if total == 0:
            status = "ABSENT"            # 前端中完全无引用（已彻底清除）
        elif not is_def and render_refs == 0:
            status = "REFERENCED_NO_DEF"  # 引用了但未定义（可能来自 import 或拼写）
        elif render_refs > 0:
            status = "RENDERED"          # 仍在组件渲染路径
        elif is_def and total <= def_line_count + 1:
            status = "ORPHAN_DEF"        # 仅类型定义，无渲染
        else:
            status = "TYPE_ONLY"         # 仅类型注解引用，无渲染
        results.append({
            "identifier": ident,
            "category": category,
            "total_refs": total,
            "render_refs": render_refs,
            "has_definition": is_def,
            "definition_file": str(defs[ident].relative_to(root)) if is_def else None,
            "status": status,
            "files": dict(sorted(refs_by_file.items(), key=lambda x: -x[1])),
        })

    # 汇总
    by_status = defaultdict(int)
    by_cat = defaultdict(int)
    for r in results:
        by_status[r["status"]] += 1
        by_cat[r["category"]] += 1
    crossbook_rendered = [
        r["identifier"] for r in results
        if r["category"] == "crossbook" and r["status"] == "RENDERED"
    ]
    summary = {
        "scanned_files": len(files),
        "identifiers_checked": len(results),
        "by_status": dict(by_status),
        "by_category": dict(by_cat),
        "crossbook_rendered_redline": crossbook_rendered,
    }
    return {"summary": summary, "results": results}


def write_reports(out_dir: Path, data: dict):
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "audit_frontend_deprecated_types.json"
    md_path = out_dir / "audit_frontend_deprecated_types.md"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    lines.append("# T06 B2 前端废弃类型只读审计\n")
    s = data["summary"]
    lines.append(f"- 扫描文件数: {s['scanned_files']}")
    lines.append(f"- 检查标识符: {s['identifiers_checked']}")
    lines.append(f"- 状态分布: {s['by_status']}")
    lines.append(f"- 类别分布: {s['by_category']}")
    rb = s.get("crossbook_rendered_redline", [])
    if rb:
        lines.append(f"\n## ⚠️ IR-32 红线告警：跨庄共识类型仍在渲染\n")
        lines.append("以下跨庄共识类标识符仍出现在渲染路径，违反 IR-32 跨庄禁区：")
        for x in rb:
            lines.append(f"  - {x}")
    lines.append("\n## 逐标识符明细\n")
    lines.append("| 标识符 | 类别 | 状态 | 总引用 | 渲染引用 | 定义文件 |")
    lines.append("|---|---|---|---|---|---|")
    for r in data["results"]:
        lines.append(
            f"| `{r['identifier']}` | {r['category']} | {r['status']} | "
            f"{r['total_refs']} | {r['render_refs']} | "
            f"{r['definition_file'] or '—'} |"
        )
    lines.append("\n## 引用文件分布（仅列非 0 引用）\n")
    for r in data["results"]:
        if not r["files"]:
            continue
        lines.append(f"### `{r['identifier']}` ({r['status']})\n")
        for f, c in r["files"].items():
            lines.append(f"  - {f}: {c}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="前端废弃类型只读审计")
    ap.add_argument("--root", default=None, help="前端根目录(默认 D:/Architecture/frontend)")
    ap.add_argument("--out", default=None, help="报告输出目录(默认 D:/Architecture/reports)")
    ap.add_argument("--list-only", action="store_true", help="仅打印摘要不写报告")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else Path(__file__).resolve().parents[1] / "frontend"
    out = Path(args.out) if args.out else Path(__file__).resolve().parents[1] / "reports"
    if not root.exists():
        print(f"[ERR] 前端根不存在: {root}", file=sys.stderr)
        return 2

    data = audit_types(root)
    if args.list_only:
        print(json.dumps(data["summary"], ensure_ascii=False, indent=2))
        return 0
    jp, mp = write_reports(out, data)
    print(f"[OK] 已写报告:\n  {jp}\n  {mp}")
    s = data["summary"]
    print(f"[OK] 扫描 {s['scanned_files']} 文件 / {s['identifiers_checked']} 标识符")
    print(f"[OK] 状态分布: {s['by_status']}")
    rb = s.get("crossbook_rendered_redline", [])
    if rb:
        print(f"[RED] IR-32 跨庄共识仍在渲染: {rb}")
    else:
        print("[OK] IR-32 跨庄共识类型未渲染（符合红线）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
