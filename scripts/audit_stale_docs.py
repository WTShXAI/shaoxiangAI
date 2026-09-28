"""docs/ 陈旧度与孤岛文档只读审计（T18, 自动化 dbda4380 任务队列）。

目标：枚举 docs/ 全部 .md（递归），按 mtime 计算陈旧度（>60d 视为陈旧），
并对每个文档在代码/其他文档中做引用 grep，标零引用孤岛文档（归档候选）。

分类口径：
- STALE            : age_days > stale_days(默认60) → 过期，建议评审是否仍有效
- ISLAND           : 在参考根目录中零引用（referenced_count==0）→ 孤岛，建议归档评审
- STALE_ISLAND     : 既陈旧又零引用 → 高优先归档候选
- OK               : 新鲜且有引用（或虽无引用但年轻）

引用检测（启发式，非权威，归档前须人工复核）：
- 在每个参考根目录（pipeline/scripts/gq/tests/config/analysis/core/data_collector/
  external/docs 等）的源码与文档中，大小写不敏感搜索文档的「完整文件名」与「文件名主干」。
- 命中即视为被引用；排除文档自身。
- 文件名过短（<=3 字符主干）的主干搜索会被跳过，仅依赖完整文件名匹配，避免噪声。

安全红线：
- 只读：不打开 events.db，不写任何数据库，不删不改任何源文档。
- 仅落盘新审计报告 reports/stale_docs_audit.{json,md}。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(ROOT, "docs")
REPORTS_DIR = os.path.join(ROOT, "reports")

# 引用搜索根目录（相对 ROOT）；docs 自身也纳入但排除被查文件本身
REF_ROOTS = (
    "docs", "pipeline", "scripts", "gq", "tests", "config",
    "analysis", "core", "data_collector", "external",
    "bridge_service.py", "live_odds_gateway.py", "agent_cruise.py",
    "ARCHITECTURE.md", "AGENTS.md", "DESIGN_TOKENS.md",
)

# 参与引用搜索的文件扩展名
REF_EXTS = (".py", ".md", ".json", ".yaml", ".yml", ".ts", ".tsx",
            ".js", ".txt", ".csv", ".mermaid")


def days_old(epoch, now=None):
    """给定 mtime epoch 距 now 的天数（浮点）。"""
    if now is None:
        now = time.time()
    return (now - epoch) / 86400.0


def is_stale(epoch, now=None, stale_days=60):
    """age_days > stale_days 即陈旧。"""
    return days_old(epoch, now) > stale_days


def _stem_of(basename):
    """取文件名主干（去扩展名）。"""
    return os.path.splitext(basename)[0]


def build_needles(basename):
    """构造引用搜索 needle 列表：完整文件名 + 主干（主干>3 字符才纳入）。"""
    stem = _stem_of(basename)
    needles = [basename.lower()]
    if len(stem) > 3:
        needles.append(stem.lower())
    return needles


def count_references(basename, exclude_path, root, ref_roots=None):
    """在参考根目录中搜索该文档的引用次数（排除文档自身）。

    返回 (count, hit_files)。纯只读。
    """
    if ref_roots is None:
        ref_roots = REF_ROOTS
    needles = build_needles(basename)
    exclude_abs = os.path.abspath(exclude_path)
    count = 0
    hits = []
    for r in ref_roots:
        rd = os.path.join(root, r)
        if not os.path.exists(rd):
            continue
        if os.path.isfile(rd):
            files = [rd]
        else:
            files = []
            for dirpath, dirs, fnames in os.walk(rd):
                # 跳过明显的产物/缓存目录
                dirs[:] = [d for d in dirs if d not in (
                    "__pycache__", "node_modules", ".git", ".workbuddy")]
                for fn in fnames:
                    if fn.lower().endswith(REF_EXTS):
                        files.append(os.path.join(dirpath, fn))
        for fp in files:
            if os.path.abspath(fp) == exclude_abs:
                continue
            try:
                with open(fp, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read().lower()
            except OSError:
                continue
            if any(n in text for n in needles):
                count += 1
                hits.append(fp)
    return count, hits


def classify_doc(age_days, referenced_count, stale_days=60):
    """分类纯函数。"""
    stale = age_days > stale_days
    island = referenced_count == 0
    if stale and island:
        return "STALE_ISLAND"
    if stale:
        return "STALE"
    if island:
        return "ISLAND"
    return "OK"


def suggest_action(status):
    if status == "STALE_ISLAND":
        return "高优先归档候选（既陈旧又零引用）"
    if status == "STALE":
        return "过期，建议评审是否仍有效"
    if status == "ISLAND":
        return "零引用孤岛，建议归档评审（归档前人工复核）"
    return ""


def enumerate_docs(docs_dir):
    """递归枚举 docs/ 下全部 .md，返回 [(relpath, abspath)]。"""
    out = []
    for dirpath, dirs, fnames in os.walk(docs_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for fn in fnames:
            if fn.lower().endswith(".md"):
                ap = os.path.join(dirpath, fn)
                rel = os.path.relpath(ap, docs_dir)
                out.append((rel, ap))
    out.sort(key=lambda x: x[0])
    return out


def build_report(docs_dir, root, stale_days=60, now=None,
                 output_json=None, output_md=None):
    """枚举 docs/ .md，陈旧度分类 + 引用统计，落盘审计报告。

    返回 (report_dict, output_json_path, output_md_path)。
    """
    if now is None:
        now = time.time()
    entries = []
    for rel, ap in enumerate_docs(docs_dir):
        try:
            st = os.stat(ap)
        except OSError:
            continue
        epoch = st.st_mtime
        age_days = days_old(epoch, now)
        referenced_count, _hits = count_references(
            os.path.basename(ap), ap, root)
        status = classify_doc(age_days, referenced_count, stale_days)
        entries.append({
            "relpath": rel,
            "name": os.path.basename(ap),
            "size_bytes": st.st_size,
            "mtime_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)),
            "age_days": round(age_days, 2),
            "referenced_count": referenced_count,
            "status": status,
            "suggestion": suggest_action(status),
        })

    by_status = {}
    for e in entries:
        by_status.setdefault(e["status"], 0)
        by_status[e["status"]] += 1

    summary = {
        "total_docs": len(entries),
        "by_status": by_status,
        "stale_days": stale_days,
        "stale_count": by_status.get("STALE", 0) + by_status.get("STALE_ISLAND", 0),
        "island_count": by_status.get("ISLAND", 0) + by_status.get("STALE_ISLAND", 0),
    }

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "docs_dir": docs_dir,
        "summary": summary,
        "entries": entries,
    }

    if output_json:
        with open(output_json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    if output_md:
        _write_md(report, output_md)
    return report, output_json, output_md


def _write_md(report, path):
    s = report["summary"]
    lines = []
    lines.append("# docs/ 陈旧度与孤岛文档审计报告")
    lines.append("")
    lines.append(f"- 生成时间: {report['generated_at']}")
    lines.append(f"- 扫描目录: `{report['docs_dir']}`（递归 .md）")
    lines.append(f"- 陈旧阈值: >{s['stale_days']}d")
    lines.append(f"- 引用检测: 启发式（完整文件名+主干, 大小写不敏感），归档前须人工复核")
    lines.append("")
    lines.append(f"## 汇总（共 {s['total_docs']} 份文档）")
    lines.append("")
    order = ["STALE_ISLAND", "STALE", "ISLAND", "OK"]
    for st in order:
        if st in s["by_status"]:
            lines.append(f"- {st}: {s['by_status'][st]}")
    lines.append("")
    lines.append(f"- 陈旧总数(STALE+STALE_ISLAND): {s['stale_count']}")
    lines.append(f"- 孤岛总数(ISLAND+STALE_ISLAND): {s['island_count']}")
    lines.append("")
    lines.append("## 需关注项（STALE / ISLAND / STALE_ISLAND）")
    lines.append("")
    lines.append("| 文档 | 年龄(d) | 引用数 | 状态 | 建议 |")
    lines.append("|------|--------|--------|------|------|")
    for e in report["entries"]:
        if e["status"] == "OK":
            continue
        lines.append(f"| {e['relpath']} | {e['age_days']} | "
                     f"{e['referenced_count']} | {e['status']} | {e['suggestion']} |")
    lines.append("")
    lines.append("## 全量清单")
    lines.append("")
    lines.append("| 文档 | 大小(B) | mtime | 年龄(d) | 引用数 | 状态 |")
    lines.append("|------|--------|-------|--------|--------|------|")
    for e in report["entries"]:
        lines.append(f"| {e['relpath']} | {e['size_bytes']} | {e['mtime_iso']} | "
                     f"{e['age_days']} | {e['referenced_count']} | {e['status']} |")
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="docs/ 陈旧度与孤岛文档只读审计")
    ap.add_argument("--docs-dir", default=DOCS_DIR)
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--stale-days", type=float, default=60)
    ap.add_argument("--output-json", default=os.path.join(REPORTS_DIR, "stale_docs_audit.json"))
    ap.add_argument("--output-md", default=os.path.join(REPORTS_DIR, "stale_docs_audit.md"))
    args = ap.parse_args()

    report, oj, om = build_report(
        args.docs_dir, args.root, stale_days=args.stale_days,
        output_json=args.output_json, output_md=args.output_md,
    )
    s = report["summary"]
    print(f"[audit_stale_docs] 扫描 {s['total_docs']} 份文档")
    print(f"  STALE+STALE_ISLAND={s['stale_count']}  "
          f"ISLAND+STALE_ISLAND={s['island_count']}")
    print(f"  明细: {s['by_status']}")
    print(f"  报告已落盘: {oj}\n              {om}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
