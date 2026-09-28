"""config/ 孤儿配置只读审计（T19, 自动化 dbda4380 任务队列）。

目标：枚举 config/ 下的 *.yaml|*.yml|*.json（不含 .py / README.md），
在代码库中做引用 grep，标零引用/禁用/过期/残留已废止纪律(IR-xx)引用的配置，
并对照 archive/rules_voided_20260924/ 已废止项给出处置建议。

分类口径：
- ORPHAN          : 在参考根目录中零引用（referenced_count==0）→ 孤儿，建议归档评审
- BACKUP          : 有 .bak_* / _dirty / _old 后缀或同级备份 → 过期备份，建议清理归档
- VOIDED_REF      : 文件内容引用已废止 IR-xx 纪律项（对照 archive/rules_voided）→ 残留需改写
- DISABLED_MARK   : 文件内显式 disabled/deprecated/obsolete=true 标记 → 明确弃用
- ACTIVE          : 被引用且无上述负面标记 → 正常使用

引用检测（启发式，非权威，归档前须人工复核）：
- 在每个参考根目录的源码中，大小写不敏感搜索「完整文件名」「config/<文件名>」与「文件名主干」。
- 命中即视为被引用；排除文件自身。主干 <=3 字符时仅依赖完整文件名匹配，避免噪声。

安全红线：
- 只读：不打开 events.db，不写任何数据库，不删不改任何源文件。
- 仅落盘新审计报告 reports/audit_orphan_config.{json,md}。
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
REPORTS_DIR = os.path.join(ROOT, "reports")
VOIDED_DIR = os.path.join(ROOT, "archive", "rules_voided_20260924")

# 引用搜索根目录（相对 ROOT）；config 自身纳入但排除被查文件
REF_ROOTS = (
    "pipeline", "scripts", "gq", "tests", "analysis", "core",
    "data_collector", "external", "config", "docs",
    "bridge_service.py", "live_odds_gateway.py", "agent_cruise.py",
    "ARCHITECTURE.md", "AGENTS.md", "DESIGN_TOKENS.md",
)
REF_EXTS = (".py", ".md", ".json", ".yaml", ".yml", ".ts", ".tsx",
            ".js", ".txt", ".csv", ".mermaid")

# 视为配置的扩展名（不含 .py / .md）
CONFIG_EXTS = (".yaml", ".yml", ".json")

# 备份/过期后缀判定
BACKUP_RE = re.compile(r"(\.bak[_\-].*|\._dirty$|\._old$|\._backup$|~)$", re.IGNORECASE)

# 文件内显式禁用标记（两种方向）：
#   enabled: false / enabled=0 / enabled=no   → 被关闭
#   disabled: true / deprecated: true / obsolete: yes → 被弃用
DISABLED_RE = re.compile(
    r"^\s*enabled\s*[:=]\s*(?:false|no|0)\s*$"
    r"|"
    r"^\s*(?:disabled|deprecated|obsolete)\s*[:=]\s*(?:true|yes|1)\s*$",
    re.IGNORECASE | re.MULTILINE)

# 已废止纪律引用（IR-xx 编号均已废止, 权威源 docs/DISCIPLINE.md）
VOIDED_RULE_RE = re.compile(r"\bIR[-\s]?\d{1,3}\b", re.IGNORECASE)


def days_old(epoch, now=None):
    if now is None:
        now = time.time()
    return (now - epoch) / 86400.0


def is_backup(basename):
    """按文件名后缀判定是否为备份/过期文件。"""
    return bool(BACKUP_RE.search(basename))


def _stem_of(basename):
    return os.path.splitext(basename)[0]


def build_needles(basename):
    """构造引用搜索 needle：完整文件名 + config/<文件名> + 主干(>3字符)。"""
    stem = _stem_of(basename)
    needles = [basename.lower(), ("config/" + basename).lower()]
    if len(stem) > 3:
        needles.append(stem.lower())
    return needles


def count_references(basename, exclude_path, root, ref_roots=None):
    """在参考根目录中搜索该配置的引用次数（排除自身）。

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


def detect_internal_marks(content):
    """检测文件内禁用标记与已废止纪律引用。

    返回 dict {disabled: bool, voided_rules: [str]}。
    """
    disabled = bool(DISABLED_RE.search(content))
    voided = sorted(set(VOIDED_RULE_RE.findall(content)))
    return {"disabled": disabled, "voided_rules": voided}


def classify_config(referenced_count, backup, marks):
    """分类纯函数。优先级：BACKUP > VOIDED_REF > DISABLED_MARK > ORPHAN > ACTIVE。"""
    if backup:
        return "BACKUP"
    if marks.get("voided_rules"):
        return "VOIDED_REF"
    if marks.get("disabled"):
        return "DISABLED_MARK"
    if referenced_count == 0:
        return "ORPHAN"
    return "ACTIVE"


def suggest_action(status, voided_rules):
    if status == "BACKUP":
        return "过期备份，建议归档/清理（不影响运行）"
    if status == "VOIDED_REF":
        rules = ",".join(voided_rules) if voided_rules else "IR-xx"
        return f"残留已废止纪律引用({rules})，需改写为 docs/DISCIPLINE.md 口径"
    if status == "DISABLED_MARK":
        return "文件内显式弃用标记，建议确认并归档"
    if status == "ORPHAN":
        return "零引用孤儿配置，建议归档评审（归档前人工复核）"
    return ""


def is_config_file(basename):
    """判定是否应纳入审计的配置文件。

    含两类：
    - 常规：以 .yaml|.yml|.json 结尾
    - 备份形式：形如 name.json.bak_2026 / name.yaml._dirty（配置扩展名在备份后缀之前）
    """
    if basename.lower().endswith(CONFIG_EXTS):
        return True
    return bool(re.match(r".+\.(yaml|yml|json)\.[^.]+$", basename, re.IGNORECASE))


def enumerate_configs(config_dir):
    """枚举 config/ 下配置文件（含备份形式），排除 .py/.md。返回 [(rel, abs)]。"""
    out = []
    if not os.path.isdir(config_dir):
        return out
    for fn in sorted(os.listdir(config_dir)):
        ap = os.path.join(config_dir, fn)
        if not os.path.isfile(ap):
            continue
        if fn.lower() in ("readme.md",):
            continue
        if not is_config_file(fn):
            continue
        rel = fn
        out.append((rel, ap))
    return out


def build_report(config_dir, root, voided_dir=None, now=None,
                 output_json=None, output_md=None):
    """枚举 config/ 配置，引用统计 + 标记检测，落盘审计报告。

    返回 (report_dict, output_json_path, output_md_path)。
    """
    if now is None:
        now = time.time()
    voided_present = bool(voided_dir and os.path.isdir(voided_dir))

    entries = []
    for rel, ap in enumerate_configs(config_dir):
        try:
            st = os.stat(ap)
        except OSError:
            continue
        epoch = st.st_mtime
        age_days = days_old(epoch, now)
        referenced_count, _hits = count_references(os.path.basename(ap), ap, root)
        try:
            with open(ap, encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except OSError:
            content = ""
        marks = detect_internal_marks(content)
        backup = is_backup(os.path.basename(ap))
        status = classify_config(referenced_count, backup, marks)
        entries.append({
            "name": os.path.basename(ap),
            "size_bytes": st.st_size,
            "mtime_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)),
            "age_days": round(age_days, 2),
            "referenced_count": referenced_count,
            "is_backup": backup,
            "disabled_mark": marks["disabled"],
            "voided_rules": marks["voided_rules"],
            "status": status,
            "suggestion": suggest_action(status, marks["voided_rules"]),
        })

    by_status = {}
    for e in entries:
        by_status.setdefault(e["status"], 0)
        by_status[e["status"]] += 1

    summary = {
        "total_configs": len(entries),
        "by_status": by_status,
        "orphan_count": by_status.get("ORPHAN", 0),
        "backup_count": by_status.get("BACKUP", 0),
        "voided_ref_count": by_status.get("VOIDED_REF", 0),
        "disabled_count": by_status.get("DISABLED_MARK", 0),
        "voided_dir_present": voided_present,
    }

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "config_dir": config_dir,
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
    lines.append("# config/ 孤儿配置只读审计报告")
    lines.append("")
    lines.append(f"- 生成时间: {report['generated_at']}")
    lines.append(f"- 扫描目录: `{report['config_dir']}`（*.yaml|*.yml|*.json, 不含 .py/.md）")
    lines.append(f"- 引用检测: 启发式（完整文件名 + config/<名> + 主干, 大小写不敏感），归档前须人工复核")
    lines.append(f"- 已废止纪律对照: archive/rules_voided_20260924/ 存在={s['voided_dir_present']}（IR-xx 编号均已废止, 权威源 docs/DISCIPLINE.md）")
    lines.append("")
    lines.append(f"## 汇总（共 {s['total_configs']} 份配置）")
    lines.append("")
    order = ["ORPHAN", "BACKUP", "VOIDED_REF", "DISABLED_MARK", "ACTIVE"]
    for st in order:
        if st in s["by_status"]:
            lines.append(f"- {st}: {s['by_status'][st]}")
    lines.append("")
    lines.append(f"- 孤儿(ORPHAN): {s['orphan_count']} ｜ 备份(BACKUP): {s['backup_count']} ｜ "
                 f"残留废止引用(VOIDED_REF): {s['voided_ref_count']} ｜ 显式弃用(DISABLED_MARK): {s['disabled_count']}")
    lines.append("")
    lines.append("## 需关注项（非 ACTIVE）")
    lines.append("")
    lines.append("| 配置 | 引用数 | 状态 | 建议 |")
    lines.append("|------|--------|------|------|")
    for e in report["entries"]:
        if e["status"] == "ACTIVE":
            continue
        lines.append(f"| {e['name']} | {e['referenced_count']} | "
                     f"{e['status']} | {e['suggestion']} |")
    lines.append("")
    lines.append("## 全量清单")
    lines.append("")
    lines.append("| 配置 | 大小(B) | mtime | 年龄(d) | 引用数 | 状态 |")
    lines.append("|------|--------|-------|--------|--------|------|")
    for e in report["entries"]:
        lines.append(f"| {e['name']} | {e['size_bytes']} | {e['mtime_iso']} | "
                     f"{e['age_days']} | {e['referenced_count']} | {e['status']} |")
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="config/ 孤儿配置只读审计")
    ap.add_argument("--config-dir", default=CONFIG_DIR)
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--voided-dir", default=VOIDED_DIR)
    ap.add_argument("--output-json", default=os.path.join(REPORTS_DIR, "audit_orphan_config.json"))
    ap.add_argument("--output-md", default=os.path.join(REPORTS_DIR, "audit_orphan_config.md"))
    args = ap.parse_args()

    report, oj, om = build_report(
        args.config_dir, args.root, voided_dir=args.voided_dir,
        output_json=args.output_json, output_md=args.output_md,
    )
    s = report["summary"]
    print(f"[audit_orphan_config] 扫描 {s['total_configs']} 份配置")
    print(f"  ORPHAN={s['orphan_count']}  BACKUP={s['backup_count']}  "
          f"VOIDED_REF={s['voided_ref_count']}  DISABLED={s['disabled_count']}  ACTIVE={s['by_status'].get('ACTIVE', 0)}")
    print(f"  报告已落盘: {oj}\n              {om}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
