#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T20 — pipeline/ 测试覆盖缺口盘点（只读）。

作用：
  1. 静态解析 pipeline/ 下全部 .py 的「公共」函数/类（名称不以 _ 开头）。
  2. 比对 tests/ 是否出现该符号名（引用即视为「有测试触点」，粗粒度代理，非行覆盖）。
  3. 按风险评级（HIGH/MED/LOW）标注关键路径未覆盖项，输出 Top N 缺口报告。

红线：仅读 pipeline/ 与 tests/，不写任何生产代码、不碰 events.db、不杀进程。
用法：
  python scripts/audit_test_coverage_gap.py            # 实跑 → reports/test_coverage_gap.{json,md}
  python -m pytest tests/test_audit_test_coverage_gap.py -q   # 单测解析逻辑
"""
from __future__ import annotations

import ast
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Iterable

# ----------------------------------------------------------------------------
# 风险评级规则（按「系统关键度」而非代码行数）
# ----------------------------------------------------------------------------
# 高关键度模块：涉及金钱结算 / 去水 / 校准 / 预测输出 / 诚实守卫 / 假0-0过滤 / 跨庄红线
HIGH_MODULES = {
    "settle", "odds_math", "calibration", "calibration_overlay", "cs_calibration",
    "predict_export", "clean_outcomes", "verification", "opening_line",
    "build_opening_lines", "odds_candles_predict", "feature_consistency",
    "test_no_crossbook", "crossbook", "ir32",
}
# 高关键度符号名片段（命中即 HIGH，除非被显式 LOW 排除）
HIGH_SYMBOL_FRAGMENTS = (
    "settle", "devig", "devig_power", "calibrat", "predict", "verif",
    "clean_outcome", "opening_line", "build_opening", "crossbook", "cross_book",
    "kelly", "guard", "security", "ir32", "edge", "value_layer", "signal",
)
# 中关键度：数据接入 / 特征工程 / 分析 / 模型
MED_MODULE_FRAGMENTS = (
    "collector", "feature", "analysis", "model", "predictor", "odds",
    "league", "match", "draw", "cs_", "ht_", "inplay", "handicap", "dc_",
)
MED_SYMBOL_FRAGMENTS = (
    "feature", "analy", "collect", "scrape", "model", "predict", "odds",
    "league", "match", "draw", "signal", "prob", "score",
)
# 低关键度模块（明确排除，避免噪声）
LOW_MODULES = {
    "odds_theory_exam", "odds_taxonomy", "cup_over_edge_test", "external_h2h_scraper",
}


@dataclass
class Symbol:
    module: str            # 模块文件 basename（去 .py）
    name: str              # 公共符号名
    kind: str              # 'function' | 'class'
    lineno: int
    container: str = ""    # 若为嵌套定义，所属上层名


def iter_python_modules(root: str) -> Iterable[tuple[str, str]]:
    """yield (relpath, abspath) for every .py under root, skipping __pycache__."""
    for dirpath, dirnames, filenames in os.walk(root):
        if "__pycache__" in dirnames:
            dirnames.remove("__pycache__")
        for fn in filenames:
            if fn.endswith(".py") and not fn.startswith("test_"):
                abspath = os.path.join(dirpath, fn)
                relpath = os.path.relpath(abspath, root)
                yield relpath, abspath


def extract_public_symbols(path: str) -> list[Symbol]:
    """AST 解析单文件，提取所有不以 _ 开头的 function/class 定义（含嵌套）。"""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        try:
            tree = ast.parse(fh.read(), filename=path)
        except SyntaxError:
            return []
    out: list[Symbol] = []

    def walk(node: ast.AST, container: str = ""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if child.name.startswith("_"):
                    # 仍递归进类内部找公共方法（类本身私有但方法可能公共）
                    if isinstance(child, ast.ClassDef):
                        walk(child, child.name)
                    continue
                out.append(Symbol(
                    module=os.path.splitext(os.path.basename(path))[0],
                    name=child.name,
                    kind="class" if isinstance(child, ast.ClassDef) else "function",
                    lineno=child.lineno,
                    container=container,
                ))
                if isinstance(child, ast.ClassDef):
                    walk(child, child.name)

    walk(tree)
    return out


def load_test_symbols(tests_dir: str) -> set[str]:
    """收集 tests/ 内出现过的全部标识符（Name/函数名/import），粗粒度覆盖代理。"""
    used: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(tests_dir):
        if "__pycache__" in dirnames:
            dirnames.remove("__pycache__")
        for fn in filenames:
            if not fn.startswith("test_") or not fn.endswith(".py"):
                continue
            ap = os.path.join(dirpath, fn)
            with open(ap, "r", encoding="utf-8", errors="replace") as fh:
                try:
                    tree = ast.parse(fh.read(), filename=ap)
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    used.add(node.id)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    used.add(node.name)
                elif isinstance(node, ast.Attribute):
                    used.add(node.attr)
                elif isinstance(node, ast.arg):
                    used.add(node.arg)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        used.add(alias.asname or alias.name)
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        used.add(alias.asname or alias.name)
    return used


def rate_risk(module: str, name: str) -> tuple[str, str]:
    """返回 (评级, 理由)。"""
    if module in LOW_MODULES:
        return "LOW", f"模块 {module} 属低关键度（理论/诊断/爬虫类）"
    if module in HIGH_MODULES or any(f in name for f in HIGH_SYMBOL_FRAGMENTS):
        return "HIGH", f"命中高关键度路径（结算/去水/校准/预测/诚实守卫/跨庄红线）"
    if any(f in module for f in MED_MODULE_FRAGMENTS) or any(f in name for f in MED_SYMBOL_FRAGMENTS):
        return "MED", "命中中关键度路径（数据/特征/分析/模型）"
    return "LOW", "属工具/脚本/辅助类，低关键度"


def build_gap_report(pipeline_root: str, tests_dir: str, top_n: int = 40) -> dict:
    symbols = []
    for rel, ap in iter_python_modules(pipeline_root):
        symbols.extend(extract_public_symbols(ap))
    test_ids = load_test_symbols(tests_dir)

    rows = []
    for s in symbols:
        covered = s.name in test_ids
        risk, reason = rate_risk(s.module, s.name)
        rows.append({
            "module": s.module,
            "name": s.name,
            "kind": s.kind,
            "lineno": s.lineno,
            "container": s.container,
            "risk": risk,
            "covered": covered,
            "risk_reason": reason,
        })

    total = len(rows)
    covered_n = sum(1 for r in rows if r["covered"])
    by_risk = {
        lv: {"total": sum(1 for r in rows if r["risk"] == lv),
             "uncovered": sum(1 for r in rows if r["risk"] == lv and not r["covered"])}
        for lv in ("HIGH", "MED", "LOW")
    }

    # 缺口表：仅未覆盖，按风险排序（HIGH>MED>LOW），同风险按模块名
    rank = {"HIGH": 0, "MED": 1, "LOW": 2}
    gaps = [r for r in rows if not r["covered"]]
    gaps.sort(key=lambda r: (rank[r["risk"]], r["module"], r["name"]))
    top_gaps = gaps[:top_n]

    return {
        "summary": {
            "total_public_symbols": total,
            "covered_symbols": covered_n,
            "uncovered_symbols": total - covered_n,
            "coverage_proxy_pct": round(100.0 * covered_n / total, 1) if total else 0.0,
            "by_risk": by_risk,
            "note": "覆盖为符号名引用代理，非行/分支覆盖；未在 tests/ 出现名即计为缺口。",
        },
        "top_uncovered_gaps": top_gaps,
    }


def _default_paths() -> tuple[str, str]:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "pipeline"), os.path.join(base, "tests")


def main() -> int:
    pipeline_root, tests_dir = _default_paths()
    report = build_gap_report(pipeline_root, tests_dir)
    os.makedirs(os.path.join(os.path.dirname(pipeline_root), "reports"), exist_ok=True)
    out_json = os.path.join(os.path.dirname(pipeline_root), "reports", "test_coverage_gap.json")
    out_md = os.path.join(os.path.dirname(pipeline_root), "reports", "test_coverage_gap.md")
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    s = report["summary"]
    lines = [
        "# pipeline/ 测试覆盖缺口盘点（只读代理）",
        "",
        f"- 公共符号总数: **{s['total_public_symbols']}**",
        f"- 有测试触点(名引用): {s['covered_symbols']} → 代理覆盖率 **{s['coverage_proxy_pct']}%**",
        f"- 缺口符号: {s['uncovered_symbols']}",
        f"- 按风险: HIGH 未覆盖 {s['by_risk']['HIGH']['uncovered']}/{s['by_risk']['HIGH']['total']}"
        f" | MED {s['by_risk']['MED']['uncovered']}/{s['by_risk']['MED']['total']}"
        f" | LOW {s['by_risk']['LOW']['uncovered']}/{s['by_risk']['LOW']['total']}",
        "",
        "> ⚠ 覆盖为「符号名在 tests/ 出现」粗粒度代理，非行/分支覆盖。"
        "高关键度缺口（结算/去水/校准/预测/诚实守卫）应优先补 pytest。",
        "",
        "## Top 未覆盖缺口（按风险）",
        "",
        "| 风险 | 模块 | 符号 | 类型 | 行 | 理由 |",
        "|---|---|---|---|---|---|",
    ]
    for r in report["top_uncovered_gaps"]:
        loc = f"{r['module']}.py:{r['lineno']}"
        lines.append(
            f"| {r['risk']} | {r['module']} | {r['name']}"
            f"{(' (' + r['container'] + ')') if r['container'] else ''} | {r['kind']} | {loc} | {r['risk_reason']} |"
        )
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"[T20] 符号 {s['total_public_symbols']} / 覆盖代理 {s['coverage_proxy_pct']}%"
          f" / HIGH缺口 {s['by_risk']['HIGH']['uncovered']} → {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
