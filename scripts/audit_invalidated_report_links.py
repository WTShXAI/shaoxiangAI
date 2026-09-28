"""T17 失效报告引用链只读盘点（T24, 自动化 dbda4380 任务队列）。

目标：基于 T17 的 stale_reports_audit.json，取出 INVALIDATED_BY_RETRAIN 类报告（模型待重训，
结论待作废），只读 grep 代码与 docs 中引用这些失效报告路径的位置，输出受影响引用清单。

引用分类：
- code_backend  : 后端 Python 文件（pipeline/analysis/scripts/...），可能正在加载失效结论
- code_frontend : 前端 / JS 家族文件（frontend/browser_extension/...）
- doc           : Markdown / HTML 文档链接
- config        : config/ 下配置引用
- other         : 其余文本

severity 启发式（非绝对判定，供人工复核优先级）：
- high   : code_backend（可能在生产代码路径中直接 import/open 失效报告结论）
- medium : code_frontend
- low    : doc / config / other

安全红线：
- 只读：不打开 events.db，不写任何数据库，不删不改任何源报告/源代码。
- 仅落盘新审计报告 reports/invalidated_report_links_audit.{json,md}。
- 扫描排除 reports/ 自身（避免 stale_reports_audit.json 自我命中全部报告名的噪声）、
  data/（海量游戏数据非报告引用）、archive/（已归档死代码，单独不计入主清单）。
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

# 扫描根目录（相对 ROOT）
SCAN_DIRS = [
    "scripts", "pipeline", "core", "analysis", "gq", "config", "deploy",
    "docs", "frontend", "deliverables", "external", "data_collector",
    "history", "odds_db", "browser_extension",
]
EXCLUDE_DIRS = {".venv", "node_modules", "__pycache__", ".git", "reports",
                "data", "archive"}
TEXT_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".md", ".html", ".htm",
    ".json", ".csv", ".txt", ".yaml", ".yml", ".ini", ".sh", ".bat",
}
# 报告文件名末尾扩展（用于在 stem 二次匹配时剥离）
REPORT_EXTS = (".json", ".md", ".html", ".csv", ".out", ".log", ".jsonl")


def load_invalidated_reports(audit_path):
    """从 stale_reports_audit.json 取出 INVALIDATED_BY_RETRAIN 报告名列表（纯函数）。

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
    """取报告文件名 stem（去掉末尾 .json/.md 等）。纯函数。"""
    base = name
    for ext in REPORT_EXTS:
        if base.lower().endswith(ext):
            return base[: -len(ext)]
    return base


def _path_parts(file_path):
    """返回相对 ROOT 的路径分片；跨挂载（测试临时目录）时退化为绝对分片，绝不抛错。"""
    try:
        rel = os.path.relpath(file_path, ROOT)
        if rel.startswith(".."):
            # 不在 ROOT 内（如临时目录）→ 用绝对分片近似
            return os.path.normpath(file_path).split(os.sep)
        return rel.split(os.sep)
    except ValueError:
        return os.path.normpath(file_path).split(os.sep)


def safe_relpath(file_path):
    """相对 ROOT 的可读路径；跨挂载时退化为绝对路径，绝不抛错。"""
    try:
        return os.path.relpath(file_path, ROOT)
    except ValueError:
        return os.path.normpath(file_path)


def classify_reference_type(file_path):
    """按文件位置/扩展分类引用类型。纯函数。

    注意：tests/ 下的引用（多为测试夹具字符串，如本审计自身的 fixtures）归为
    "test" 类型 + low severity，避免把无害的测试夹具误报为生产代码加载失效结论。
    """
    parts = _path_parts(file_path)
    low_parts = [p.lower() for p in parts]
    ext = os.path.splitext(file_path)[1].lower()
    if "tests" in low_parts:
        return "test"
    if low_parts and low_parts[0] == "config":
        return "config"
    if ext in (".md", ".html", ".htm"):
        return "doc"
    if ext == ".py":
        return "code_backend"
    if ext in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"):
        return "code_frontend"  # JS 家族默认前端侧
    return "other"


def severity_of(ref_type):
    """severity 启发式。纯函数。"""
    if ref_type == "code_backend":
        return "high"
    if ref_type == "code_frontend":
        return "medium"
    return "low"


def detect_access(raw):
    """按行内容启发式判定引用是读(read, 消费者)还是写(write, 生产者/预期内)。

    生产者脚本 open(..., "w")/json.dump 写出该报告属预期内（报告被 T17 判失效只说明
    内容基于重训前模型，生产者本身无需处置）；消费者 json.load/open(..., "r") 读取该报告
    结论则须人工复核是否在生产路径中依赖了失效结论。纯函数。
    """
    low = raw.lower()
    if any(k in low for k in ("json.dump", "write_text", "to_csv", "file.write",
                              '"w"', "'w'")):
        return "write"
    if any(k in low for k in ("json.load", "read_text", "read()", ',"r"', ",'r'",
                              'open(', "pd.read", "csv.reader", "csv.DictReader")):
        return "read"
    return "unknown"


def _iter_text_files(scan_root):
    """生成 scan_root 下所有文本文件的绝对路径（排除 EXCLUDE_DIRS）。"""
    for dirpath, dirnames, filenames in os.walk(scan_root):
        # 就地修改 dirnames 以排除
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXTS:
                continue
            yield os.path.join(dirpath, fn)


def search_references(scan_root, invalidated_names, max_line_len=4000):
    """对失效报告名做全名 + stem 二次匹配，返回引用记录列表。

    每条记录: dict(report, file, line_no, line, reference_type, match_kind, access, severity)。
    match_kind: "full" 命中完整文件名；"stem" 命中去掉扩展后的 stem（扩展名被省略的引用）。
    精度保护：stem 匹配时若行内实际扩展名对应的完整文件**不在**失效集内（即引用的是
    未失效同伴，如 LIVE 的 .json），则跳过，避免把 live 报告误伤为失效引用。
    纯函数（scan_root 为目录路径，不触碰 events.db）。
    """
    names = sorted(set(invalidated_names), key=len, reverse=True)
    invalidated_set = set(names)
    # 预编译：每个名对应 (full_regex, stem, stem_boundary_regex)
    patterns = []
    for n in names:
        # full：文件名作为子串（非单词边界，兼容路径/引号）
        full = re.compile(re.escape(n))
        stem = stem_of(n)
        # stem 边界匹配：stem 后须紧跟 引号/括号/空白/点/逗号/行尾，避免 p0_10_retrain_statusXjson 误报
        stem_boundary = re.compile(re.escape(stem) + r"(?=[\"'`)\]\s.,]|\.|$)")
        patterns.append((n, full, stem, stem_boundary))

    refs = []
    seen_pairs = set()
    for fp in _iter_text_files(scan_root):
        ref_type = classify_reference_type(fp)
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
                        # 取 stem 之后的扩展名（若有），判定实际引用文件是否失效
                        after = raw[m.end():]
                        ext_m = re.match(r"\.(json|md|html|csv|log|out|jsonl)",
                                        after, re.IGNORECASE)
                        if ext_m:
                            candidate = stem + ext_m.group(0).lower()
                            # 引用的是未失效同伴（如 LIVE 的 .json）→ 跳过，不误伤
                            if candidate not in invalidated_set:
                                kind = None
                            else:
                                kind = "stem"
                        else:
                            # 裸 stem（省略扩展名的族引用）→ 保留
                            kind = "stem"
                if kind is None:
                    continue
                pair = (name, fp, i)
                if pair in seen_pairs:
                    # 同一行若同时命中 full 与 stem（full 优先），仅记一次
                    continue
                seen_pairs.add(pair)
                refs.append({
                    "report": name,
                    "file": safe_relpath(fp),
                    "line_no": i,
                    "line": raw.strip()[:300],
                    "reference_type": ref_type,
                    "match_kind": kind,
                    "access": detect_access(raw),
                    "severity": severity_of(ref_type),
                })
    return refs


def build_report(invalidated_names, refs, audit_path, now=None):
    """汇总引用清单并构造报告 dict（不落盘）。纯函数。"""
    if now is None:
        now = time.time()

    by_type = {}
    by_sev = {}
    by_report = {}
    by_access = {}
    for r in refs:
        by_type[r["reference_type"]] = by_type.get(r["reference_type"], 0) + 1
        by_sev[r["severity"]] = by_sev.get(r["severity"], 0) + 1
        by_report[r["report"]] = by_report.get(r["report"], 0) + 1
        by_access[r["access"]] = by_access.get(r["access"], 0) + 1

    # 仅按 severity 排序：high > medium > low，再按 report/file
    sev_order = {"high": 0, "medium": 1, "low": 2}
    refs_sorted = sorted(refs, key=lambda r: (sev_order.get(r["severity"], 9),
                                              r["report"], r["file"], r["line_no"]))

    summary = {
        "invalidated_reports": len(invalidated_names),
        "reports_with_references": len(by_report),
        "total_references": len(refs),
        "by_reference_type": by_type,
        "by_severity": by_sev,
        "by_access": by_access,
        "by_report_top": dict(sorted(by_report.items(),
                                     key=lambda kv: -kv[1])[:20]),
    }

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "source_audit": os.path.relpath(audit_path, ROOT),
        "summary": summary,
        "references": refs_sorted,
    }
    return report


def write_md(report, path):
    s = report["summary"]
    lines = []
    lines.append("# 失效报告引用链只读盘点（T24）")
    lines.append("")
    lines.append(f"- 生成时间: {report['generated_at']}")
    lines.append(f"- 来源审计: `{report['source_audit']}`")
    lines.append(f"- 失效报告(待重训): {s['invalidated_reports']} 份 ｜ "
                 f"其中被引用: {s['reports_with_references']} 份 ｜ "
                 f"引用命中: {s['total_references']} 处")
    lines.append("")
    lines.append("## 引用类型分布")
    lines.append("")
    for k in ("code_backend", "code_frontend", "doc", "config", "other"):
        if k in s["by_reference_type"]:
            lines.append(f"- {k}: {s['by_reference_type'][k]}")
    lines.append("")
    lines.append("## 严重度分布（启发式，非绝对）")
    lines.append("")
    for k in ("high", "medium", "low"):
        if k in s["by_severity"]:
            lines.append(f"- {k}: {s['by_severity'][k]}")
    lines.append("")
    lines.append("## 读写姿态分布（access）")
    lines.append("")
    lines.append("- write : 该文件是报告**生产者**（open(...,\"w\")/json.dump 写出），属预期内，无需处置")
    lines.append("- read  : 该文件**读取**该失效报告结论，须人工复核是否在生产路径依赖失效结论")
    lines.append("- unknown: 无法从行内容判定读写（如纯字符串出现）")
    for k in ("write", "read", "unknown"):
        if k in s["by_access"]:
            lines.append(f"- {k}: {s['by_access'][k]}")
    lines.append("")
    lines.append("## 受引用最多的失效报告 Top20")
    lines.append("")
    lines.append("| 报告 | 被引用次数 |")
    lines.append("|------|-----------|")
    for name, cnt in s["by_report_top"].items():
        lines.append(f"| {name} | {cnt} |")
    lines.append("")
    lines.append("## 引用清单（按严重度→报告→文件排序）")
    lines.append("")
    lines.append("| 严重度 | 类型 | 读写 | 报告 | 文件 | 行 | 命中 | 片段 |")
    lines.append("|--------|------|------|------|------|----|------|------|")
    for r in report["references"]:
        snippet = r["line"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['severity']} | {r['reference_type']} | {r['access']} | {r['report']} | "
                     f"{r['file']} | {r['line_no']} | {r['match_kind']} | {snippet[:80]} |")
    lines.append("")
    lines.append("> 红线提示：本报告仅列出引用位置，**不删不改任何源文件/报告**。high 级 "
                 "(code_backend) 表示可能有生产代码正在加载失效结论，须人工复核后再决定是否替换数据源。")
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="T17 失效报告引用链只读盘点")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--audit", default=STALE_AUDIT)
    ap.add_argument("--output-json", default=os.path.join(REPORTS_DIR,
                                                          "invalidated_report_links_audit.json"))
    ap.add_argument("--output-md", default=os.path.join(REPORTS_DIR,
                                                        "invalidated_report_links_audit.md"))
    args = ap.parse_args()

    names, n_total = load_invalidated_reports(args.audit)
    if n_total == 0:
        print(f"[audit_invalidated_report_links] 警告: 未在 {args.audit} 找到失效报告"
              f"（文件缺失或未跑 T17）→ 仅产出空报告")
    refs = search_references(args.root, names)
    report = build_report(names, refs, args.audit)
    with open(args.output_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    write_md(report, args.output_md)

    s = report["summary"]
    print(f"[audit_invalidated_report_links] 失效报告={s['invalidated_reports']}  "
          f"被引用报告={s['reports_with_references']}  "
          f"引用命中={s['total_references']}")
    print(f"  by_type={s['by_reference_type']}")
    print(f"  by_severity={s['by_severity']}")
    print(f"  报告已落盘: {args.output_json}\n              {args.output_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
