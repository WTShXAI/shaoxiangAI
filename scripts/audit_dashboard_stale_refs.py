"""T29 前端看板挂载失效报告只读盘点（自动化 dbda4380 任务队列）。

目标：基于 T17 的 stale_reports_audit.json，取出 89 份 INVALIDATED_BY_RETRAIN（模型待重训、
结论待作废）报告，只读 grep 以下两类前端载体是否挂载/内嵌这些失效模型结论：
  1. `deliverables/dashboard/*.html`  —— 静态评估看板
  2. `frontend/` 递归任意文本文件   —— React 前端源码/资源

嵌入方式分类（embed_kind）：
  - iframe_embed : `<iframe src="...report...">` → 看板直接把失效报告页面内嵌渲染（高风险）
  - data_embed   : 报告数据被固化进 `<script>` JSON / SVG / 数据属性 → 失效结论数字直接画进图表（高风险）
  - fetch_load   : `fetch()` / `src=` / `url()` / `import` 运行时加载报告文件（中风险，须核来源是否实时）
  - link_ref     : markdown 链接 / `<a href>` 跳转引用（低风险，不渲染结论）
  - incidental   : 仅字符串出现（注释/日志/文件名常量），无语义挂载（低风险）

risk 启发式（非绝对判定，供人工复核优先级）：
  - high   : iframe_embed / data_embed（用户可能直接看到失效结论）
  - medium : fetch_load
  - low    : link_ref / incidental

安全红线：
  - 只读：不打开 events.db，不写任何数据库，不删不改任何源报告/源代码/看板。
  - 仅落盘新审计报告 reports/dashboard_stale_refs_audit.{json,md}。
  - 扫描排除 reports/ 自身（避免自我命中）与 data/、archive/、node_modules、.venv。
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(ROOT, "reports")
STALE_AUDIT = os.path.join(REPORTS_DIR, "stale_reports_audit.json")
DASHBOARD_DIR = os.path.join(ROOT, "deliverables", "dashboard")

# 前端扫描根
FRONTEND_ROOT = os.path.join(ROOT, "frontend")

EXCLUDE_DIRS = {".venv", "node_modules", "__pycache__", ".git", "reports",
                "data", "archive", "dist", "build"}
TEXT_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".md", ".html", ".htm",
    ".json", ".csv", ".txt", ".yaml", ".yml", ".ini", ".sh", ".bat",
}
REPORT_EXTS = (".json", ".md", ".html", ".csv", ".out", ".log", ".jsonl")


# --------------------------------------------------------------------------
# 纯函数区（不触碰 events.db，可被单测直接调用）
# --------------------------------------------------------------------------
def load_invalidated_reports(audit_path):
    """从 stale_reports_audit.json 取出 INVALIDATED_BY_RETRAIN 报告名列表。

    返回 (names, total_invalidated)。文件缺失/损坏时返回 ([], 0)，绝不抛错。
    """
    try:
        with open(audit_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return [], 0
    names = []
    for e in data.get("entries", []):
        if e.get("status") == "INVALIDATED_BY_RETRAIN":
            names.append(e["name"])
    return names, len(names)


def stem_of(name):
    """取报告文件名 stem（去掉末尾扩展）。纯函数。"""
    base = name
    for ext in REPORT_EXTS:
        if base.lower().endswith(ext):
            return base[: -len(ext)]
    return base


def detect_embed_kind(raw, name):
    """按行内容判定失效报告在该行被如何挂载（embed_kind）。纯函数。

    优先级：iframe 内嵌 > 数据固化(svg/window./data-/对象字面量赋值) >
    运行时加载(fetch/import/url/src/href/扩展名) > markdown/<a> 链接 > incidental。
    """
    low = raw.lower()
    # 1) iframe 内嵌渲染（高风险）
    if "<iframe" in low and "src" in low:
        return "iframe_embed"
    # 2) 数据固化进页面（svg 元素 / window. 全局 / data-* 属性 / 对象字面量赋值）
    if ("<svg" in low or "window." in low or "data-" in low
            or re.search(r"\b(const|var|let)\s+\w+\s*=\s*[\{\[]", low)):
        return "data_embed"
    # 3) 运行时加载语义（fetch/import/require/url/src=/href= 显式加载构造；
    #    仅出现文件名字符串本身——如注释/常量——不算加载，归 incidental）
    is_load = any(k in low for k in ("fetch(", "import ", "require(", "url(",
                                    "src=", "href="))
    # 4) markdown 链接 / <a href 跳转（须先于 load 判定，避免 .md 扩展名抢判）
    if re.search(r"\]\([^)]*" + re.escape(name), raw) or "<a " in low:
        return "link_ref"
    if is_load:
        return "fetch_load"
    # 5) 其余：仅字符串出现，无语义挂载
    return "incidental"


def risk_of(embed_kind):
    """risk 启发式。纯函数。"""
    if embed_kind in ("iframe_embed", "data_embed"):
        return "high"
    if embed_kind == "fetch_load":
        return "medium"
    return "low"


def classify_file_type(file_path):
    """按文件位置/扩展分类载体类型。纯函数。"""
    parts = _path_parts(file_path)
    low_parts = [p.lower() for p in parts]
    ext = os.path.splitext(file_path)[1].lower()
    if "tests" in low_parts:
        return "test"
    if ext in (".html", ".htm"):
        return "dashboard_html"
    if ext == ".md":
        return "doc"
    if ext == ".py":
        return "code_backend"
    if ext in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"):
        return "code_frontend"
    return "other"


def _path_parts(file_path):
    try:
        rel = os.path.relpath(file_path, ROOT)
        if rel.startswith(".."):
            return os.path.normpath(file_path).split(os.sep)
        return rel.split(os.sep)
    except ValueError:
        return os.path.normpath(file_path).split(os.sep)


def safe_relpath(file_path):
    try:
        return os.path.relpath(file_path, ROOT)
    except ValueError:
        return os.path.normpath(file_path)


def _iter_text_files(scan_root):
    """生成 scan_root 下所有文本文件的绝对路径（排除 EXCLUDE_DIRS）。"""
    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXTS:
                continue
            yield os.path.join(dirpath, fn)


def search_dashboard_refs(dashboard_dir, frontend_root, invalidated_names,
                          max_line_len=8000):
    """扫描看板 html + 前端目录，返回挂载记录列表。

    每条记录: dict(report, stem, file, line_no, line, file_type, embed_kind, risk)。
    匹配：先全名匹配，再 stem 二次匹配（同样做 LIVE companion 精度保护）。
    """
    names = sorted(set(invalidated_names), key=len, reverse=True)
    invalidated_set = set(names)
    patterns = []
    for n in names:
        full = re.compile(re.escape(n))
        stem = stem_of(n)
        stem_boundary = re.compile(re.escape(stem) + r"(?=[\"'`)\]\s.,]|\.|$)")
        patterns.append((n, full, stem, stem_boundary))

    refs = []
    seen_pairs = set()
    scan_roots = [dashboard_dir, frontend_root]
    for scan_root in scan_roots:
        if not os.path.isdir(scan_root):
            continue
        for fp in _iter_text_files(scan_root):
            ftype = classify_file_type(fp)
            if ftype == "test":
                continue
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            for i, raw in enumerate(lines, start=1):
                if len(raw) > max_line_len:
                    continue
                for (name, full_re, stem, stem_boundary) in patterns:
                    kind = None
                    if full_re.search(raw):
                        kind = "full"
                    else:
                        m = stem_boundary.search(raw)
                        if m:
                            after = raw[m.end():]
                            ext_m = re.match(r"\.(json|md|html|csv|log|out|jsonl)",
                                            after, re.IGNORECASE)
                            if ext_m:
                                candidate = stem + ext_m.group(0).lower()
                                if candidate not in invalidated_set:
                                    kind = None  # 引用未失效同伴，跳过
                                else:
                                    kind = "stem"
                            else:
                                kind = "stem"
                    if kind is None:
                        continue
                    pair = (name, fp, i)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    embed = detect_embed_kind(raw, name)
                    refs.append({
                        "report": name,
                        "stem": stem_of(name),
                        "file": safe_relpath(fp),
                        "line_no": i,
                        "line": raw.strip()[:300],
                        "file_type": ftype,
                        "embed_kind": embed,
                        "risk": risk_of(embed),
                        "match_kind": kind,
                    })
    return refs


def build_report(invalidated_names, refs, audit_path, now=None):
    """汇总挂载清单并构造报告 dict（不落盘）。纯函数。"""
    if now is None:
        now = time.time()
    by_risk = {}
    by_embed = {}
    by_type = {}
    by_report = {}
    dashboards = {}  # dashboard 文件 -> {reports:set, top_risk}
    for r in refs:
        by_risk[r["risk"]] = by_risk.get(r["risk"], 0) + 1
        by_embed[r["embed_kind"]] = by_embed.get(r["embed_kind"], 0) + 1
        by_type[r["file_type"]] = by_type.get(r["file_type"], 0) + 1
        by_report[r["report"]] = by_report.get(r["report"], 0) + 1
        if r["file_type"] == "dashboard_html":
            d = dashboards.setdefault(r["file"], {"reports": set(), "top_risk": "low"})
            d["reports"].add(r["report"])
            # top_risk 取该看板内所有挂载的最高风险
            if r["risk"] == "high":
                d["top_risk"] = "high"
            elif r["risk"] == "medium" and d["top_risk"] != "high":
                d["top_risk"] = "medium"

    risk_order = {"high": 0, "medium": 1, "low": 2}
    refs_sorted = sorted(refs, key=lambda r: (risk_order.get(r["risk"], 9),
                                              r["embed_kind"], r["report"],
                                              r["file"], r["line_no"]))

    summary = {
        "invalidated_reports": len(invalidated_names),
        "reports_with_refs": len(by_report),
        "total_refs": len(refs),
        "dashboards_scanned": 0,
        "dashboards_affected": len(dashboards),
        "by_risk": by_risk,
        "by_embed_kind": by_embed,
        "by_file_type": by_type,
        "by_report_top": dict(sorted(by_report.items(),
                                     key=lambda kv: -kv[1])[:20]),
    }
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "source_audit": os.path.relpath(audit_path, ROOT),
        "summary": summary,
        "dashboards_affected": {k: {"report_count": len(v["reports"]),
                                   "reports": sorted(v["reports"]),
                                   "top_risk": v["top_risk"]}
                                for k, v in dashboards.items()},
        "references": refs_sorted,
    }
    return report


def write_md(report, path):
    s = report["summary"]
    lines = []
    lines.append("# 前端看板挂载失效报告只读盘点（T29）")
    lines.append("")
    lines.append(f"- 生成时间: {report['generated_at']}")
    lines.append(f"- 来源审计: `{report['source_audit']}`")
    lines.append(f"- 失效报告(待重训): {s['invalidated_reports']} 份 ｜ "
                 f"被挂载: {s['reports_with_refs']} 份 ｜ 挂载命中: {s['total_refs']} 处")
    lines.append(f"- 受影响看板(静态html): {s['dashboards_affected']} 个")
    lines.append("")
    lines.append("## 风险分布（启发式，非绝对）")
    lines.append("")
    for k in ("high", "medium", "low"):
        if k in s["by_risk"]:
            lines.append(f"- {k}: {s['by_risk'][k]}")
    lines.append("")
    lines.append("## 嵌入方式分布")
    lines.append("")
    for k in ("iframe_embed", "data_embed", "fetch_load", "link_ref", "incidental"):
        if k in s["by_embed_kind"]:
            label = {
                "iframe_embed": "iframe 内嵌渲染报告页面",
                "data_embed": "报告数据固化进 script/SVG/数据属性",
                "fetch_load": "fetch/src/url/import 运行时加载",
                "link_ref": "markdown/<a> 跳转引用",
                "incidental": "仅字符串出现(注释/日志/常量)",
            }[k]
            lines.append(f"- {k} ({label}): {s['by_embed_kind'][k]}")
    lines.append("")
    lines.append("## 载体类型分布")
    lines.append("")
    for k, v in sorted(s["by_file_type"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 受影响静态看板（deliverables/dashboard/*.html）")
    lines.append("")
    if report["dashboards_affected"]:
        lines.append("| 看板文件 | 受影响报告数 | 最高风险 | 报告清单 |")
        lines.append("|----------|-------------|----------|----------|")
        for f, info in sorted(report["dashboards_affected"].items(),
                              key=lambda kv: ({"high": 0, "medium": 1, "low": 2}[kv[1]["top_risk"]],
                                             -kv[1]["report_count"])):
            rpt = ", ".join(info["reports"][:8])
            if len(info["reports"]) > 8:
                rpt += f" …(+{len(info['reports']) - 8})"
            lines.append(f"| {f} | {info['report_count']} | {info['top_risk']} | {rpt} |")
    else:
        lines.append("- 无（静态看板未挂载任何失效报告结论）")
    lines.append("")
    lines.append("## 受挂载最多的失效报告 Top20")
    lines.append("")
    lines.append("| 报告 | 被挂载次数 |")
    lines.append("|------|-----------|")
    for name, cnt in s["by_report_top"].items():
        lines.append(f"| {name} | {cnt} |")
    lines.append("")
    lines.append("## 挂载清单（按风险→嵌入方式→报告→文件排序）")
    lines.append("")
    lines.append("| 风险 | 嵌入方式 | 载体 | 报告 | 文件 | 行 | 命中 | 片段 |")
    lines.append("|------|----------|------|------|------|----|------|------|")
    for r in report["references"]:
        snippet = r["line"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['risk']} | {r['embed_kind']} | {r['file_type']} | {r['report']} | "
                     f"{r['file']} | {r['line_no']} | {r['match_kind']} | {snippet[:80]} |")
    lines.append("")
    lines.append("> 红线提示：本报告仅列出挂载位置，**不删不改任何源文件/看板/报告**。high 级 "
                 "(iframe_embed/data_embed) 表示用户可能在看板上直接看到基于重训前模型的失效结论，"
                 "须人工复核并在重训后刷新数据源。")
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="T29 前端看板挂载失效报告只读盘点")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--audit", default=STALE_AUDIT)
    ap.add_argument("--dashboard-dir", default=DASHBOARD_DIR)
    ap.add_argument("--frontend-root", default=FRONTEND_ROOT)
    ap.add_argument("--output-json", default=os.path.join(REPORTS_DIR,
                                                          "dashboard_stale_refs_audit.json"))
    ap.add_argument("--output-md", default=os.path.join(REPORTS_DIR,
                                                        "dashboard_stale_refs_audit.md"))
    args = ap.parse_args()

    names, n_total = load_invalidated_reports(args.audit)
    if n_total == 0:
        print(f"[audit_dashboard_stale_refs] 警告: 未在 {args.audit} 找到失效报告"
              f"（文件缺失或未跑 T17）→ 仅产出空报告")
    refs = search_dashboard_refs(args.dashboard_dir, args.frontend_root, names)
    report = build_report(names, refs, args.audit)
    with open(args.output_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    write_md(report, args.output_md)

    s = report["summary"]
    print(f"[audit_dashboard_stale_refs] 失效报告={s['invalidated_reports']}  "
          f"被挂载报告={s['reports_with_refs']}  "
          f"挂载命中={s['total_refs']}  "
          f"受影响看板={s['dashboards_affected']}")
    print(f"  by_risk={s['by_risk']}")
    print(f"  by_embed_kind={s['by_embed_kind']}")
    print(f"  报告已落盘: {args.output_json}\n              {args.output_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
