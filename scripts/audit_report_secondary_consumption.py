#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T42 reports/ 产物二次消费风险只读盘点（承接 T38③）。

T38 实证 `scripts/audit_stale_reports.py:85` 用 `monitor_status.json` 里的
`retrain_gate.suggest` 直接触发 `INVALIDATED_BY_RETRAIN`，把 89 份报告判死且不
随时间自动复活。本条把范围从「单条判定式」扩到「全部 reports/*.md|json 中被
后置脚本二次消费的字段」：

  1. 谁写（producer） / 谁读（consumer）：扫全仓 .py 对 `reports/...` 的路径字面量，
     按同小段的 dump/write 判 WRITE、open/load/read 判 READ。
  2. 值级传播：跨 report 文件共享的字面量（布尔开关 / 数值）。上游翻转、下游不会自动跟。
  3. **伪活引用**：consumer 的输出 mtime 早于它读到的输入 mtime → 输入已经变了而产物
     没跟着变，读者会以为它是"最新消费结果"，实际必须人工重跑。
  4. 无生产者 / 无消费者：手写快照（上游变了它不变）与死产物。

零生产 I/O：不重训、不碰调度、零 events.db 写入；reports/ 只写本脚本自己的两份输出。

用法：
    python scripts/audit_report_secondary_consumption.py
输出：reports/report_secondary_consumption_audit.{json,md}
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
OUT_JSON = os.path.join(REPORTS, "report_secondary_consumption_audit.json")
OUT_MD = os.path.join(REPORTS, "report_secondary_consumption_audit.md")

# 只统计 reports/ 顶层产物（与 T17 同口径），子目录不递归（避免把历史归档卷进来）
REPORT_EXT = {".json", ".md"}
SRC_SCAN_DIRS = ["scripts", "pipeline", "gq", "core", "config", "analysis",
                 "data_collector", "external", "sandbox", "deliverables"]
SRC_SCAN_EXTRA_FILES = ["bridge_service.py", "live_odds_gateway.py", "agent_cruise.py",
                        "odds_comp_finder.py", "odds_structure_classifier.py"]

MAX_FILE_BYTES = 4_000_000
SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__", "archive", ".venv", "venv",
                  "dist", "build", ".pytest_cache", "models", "logs", "frontend",
                  "deliverables/dashboard", "reports", ".workbuddy"}

# 本脚本自身（及测试）对 reports/ 的引用属自指，须排除（与 T33/T36/T38 同一坑）
SELF_TOKEN = "audit_report_secondary_consumption"

RE_PATH_LITERAL = re.compile(r"""["']([^"']*reports[/\\][^"'\n]*?\.(?:json|md|csv))["']""")
RE_DUMP = re.compile(r"(json\.dump|\.dump\(|\.write\(|to_csv\(|f\.write\()")
RE_READ = re.compile(r"(open\(|json\.load|\.read\(\)|load_json|Path\()(?!.*\bdump\b)")
RE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
RE_NUM = re.compile(r"(?<![\w.])-?\d+\.\d+(?![\w])")
RE_INT = re.compile(r"(?<![\w.])-?\d{3,}(?![\w.])")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _rel(fp, root=None):
    """统一正斜杠；跨盘（测试 tmp 在 C:）时降级为 basename，防 relpath ValueError。"""
    try:
        return os.path.relpath(fp, root or ROOT).replace("\\", "/")
    except ValueError:
        return os.path.basename(fp)


def _is_self(path_rel: str) -> bool:
    return SELF_TOKEN in path_rel


def _read_text(fp):
    try:
        if os.path.getsize(fp) > MAX_FILE_BYTES:
            return ""
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _mtime(fp):
    try:
        return os.path.getmtime(fp)
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# 1) reports/ 产物索引
# ---------------------------------------------------------------------------
def collect_report_index(root=ROOT):
    """只读枚举 reports/ 顶层 .json/.md（不打开不解析，仅 stat）。"""
    out = []
    d = os.path.join(root, "reports")
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        fp = os.path.join(d, name)
        if not os.path.isfile(fp):
            continue
        # 本盘点自己的两份产物不参与统计（自指，否则它会把自己也算成"高风险产物"）
        if SELF_TOKEN in name:
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in REPORT_EXT:
            continue
        out.append({
            "name": name,
            "path": _rel(fp, root),
            "ext": ext,
            "mtime": _mtime(fp),
            "mtime_iso": datetime.fromtimestamp(_mtime(fp)).strftime("%Y-%m-%d %H:%M"),
            "size": os.path.getsize(fp),
        })
    return sorted(out, key=lambda x: x["name"])


# ---------------------------------------------------------------------------
# 2) 源码侧引用扫描
# ---------------------------------------------------------------------------
def _iter_source_files(root=ROOT):
    seen = set()
    jobs = []
    for d in SRC_SCAN_DIRS:
        p = os.path.join(root, d)
        if os.path.isdir(p):
            jobs.append(p)
    for f in SRC_SCAN_EXTRA_FILES:
        p = os.path.join(root, f)
        if os.path.isfile(p):
            jobs.append(p)
    for p in jobs:
        if os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [x for x in dirnames if x not in SKIP_DIR_NAMES]
                for fn in filenames:
                    if fn.endswith(".py"):
                        fp = os.path.join(dirpath, fn)
                        if fp not in seen:
                            seen.add(fp)
                            yield fp
        elif p not in seen:
            seen.add(p)
            yield p


# 踩坑：原写法 `["']\s*[wawb+]` 不吃右引号，会把 `open(..., "a_report.json")` 里的
# 首字母 a 当成写模式匹配上 → 所有含 "a_xxx.json" 的读行被判 WRITE（整条链反掉）。
# 必须要求模式串自成引号内的 token。
RE_OPEN_W = re.compile(r"""open\((?:[^)\n]*?)["']\s*([rwax+]{1,2}b?)["']""")
RE_OPEN_CALL = re.compile(r"""open\(""")
RE_MODE_TAIL = re.compile(r"""["']([rwa]{1,2}b?)["']""")


def _explicit_open_modes(line: str):
    """行内 `open(...)` 是否带显式模式串（"r"/"w"/"rb"...）。

    踩坑（本轮最贵的一处）：`open(p, "r")` 的读证据与它下方 3 行的
    `open(p2, "w")` 落在同一 ±3 行窗口里，窗口兜底会把「读」判成「写」，
    于是"谁读谁写"整条链反掉（实测把 76 份产物误标 HIGH）。
    凡 open() 自带模式串，模式就是决定性证据，窗口一律不再override。
    """
    modes = set()
    for m in RE_OPEN_CALL.finditer(line):
        mm = RE_MODE_TAIL.search(line[m.end():m.end() + 120])
        if mm:
            modes.add(mm.group(1).lower())
    return modes


def _op_of_line(line: str):
    """行级读写判定，返回 None 表示"本行没有证据，需要窗口兜底"。

    顺序有讲究：先看 open() 自带模式串（决定性证据），再看"任何写证据"兜底
    （`open(out + ".json", "w")` 这类没有显式 filename 的写法只能靠 RE_OPEN_W）。
    若反过来先判 RE_OPEN_W，`open("x.json", "r")` 会被误判成 WRITE。
    """
    modes = _explicit_open_modes(line)
    if modes:
        return "READ" if all(m.startswith("r") for m in modes) else "WRITE"
    if RE_DUMP.search(line) or RE_OPEN_W.search(line):
        return "WRITE"
    if RE_READ.search(line):
        return "READ"
    return None


def classify_op(line: str, file_hint: str = "") -> str:
    """行级判读写：dump / `open(...,'w')` / 显式非读模式 → WRITE；显式 r 模式 / load / read → READ。"""
    op = _op_of_line(line)
    if op:
        return op
    # 行上没有明确证据时，才在 ±3 行窗口里找"写"证据（窗口放太宽会把相邻的
    # `open(...,"w")` 吸进来，把 read 行判成 WRITE）
    return file_hint or "UNKNOWN"


def classify_ctx(line: str, window, file_hint: str = "") -> str:
    op = _op_of_line(line)
    if op:
        return op
    for l in window:
        if RE_DUMP.search(l) or RE_OPEN_W.search(l):
            return "WRITE"
    for l in window:
        if RE_READ.search(l):
            return "READ"
    return file_hint or "UNKNOWN"


RE_REPORTS_DIR_ASSIGN = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*.*?[(\"'][^\"']*reports[/\\][^\"']*[\"']")
# 支持 f-string 路径（`f"{stem}_sample_growth.json"`）——踩坑：不认 f 前缀会整片漏抓
RE_ANY_REPORT_STR = re.compile(r"""[rbf]{0,2}["']([^"'\n]*?\.(?:json|md|csv))["']""")


def _collect_reports_dir_vars(files):
    """两遍扫描：先认出 `REPORTS = os.path.join(ROOT,"reports")` 这类目录变量。"""
    dvar = set()
    for text in files:
        for m in RE_REPORTS_DIR_ASSIGN.finditer(text):
            dvar.add(m.group(1))
    return dvar


def scan_source_refs(root=ROOT):
    """扫全仓 .py 对 reports/ 的引用。

    路径不像 `reports/x.json` 直写，而是 `os.path.join(REPORTS, "x.json")`，
    故两遍：① 认目录变量 ② 匹配落在该目录下的文件名字面量。自指已排除。
    """
    corpus = []
    paths = []
    for fp in _iter_source_files(root):
        rel = _rel(fp, root)
        if _is_self(rel):
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        if "reports" not in text.lower():
            continue
        corpus.append((rel, fp, text.splitlines()))
    dvar = _collect_reports_dir_vars(["\n".join(t[2]) for t in corpus])

    file_hint = {}
    for rel, _fp, lines in corpus:
        text = "\n".join(lines)
        has_write = bool(RE_DUMP.search(text)) or any(
            RE_OPEN_W.search(l) for l in lines)
        has_read = bool(RE_READ.search(text))
        if has_write and not has_read:
            file_hint[rel] = "WRITE"
        elif has_read and not has_write:
            file_hint[rel] = "READ"
        else:
            # 双向证据文件不做文件级兜底：宁可 UNKNOWN 也不把 read 行乱判成 WRITE
            file_hint[rel] = ""

    refs = []
    tier_a_names = set()
    def _win(lines, i):
        return lines[max(0, i - 3):i + 3]

    for rel, _fp, lines in corpus:
        for i, line in enumerate(lines, 1):
            for m in RE_PATH_LITERAL.finditer(line):
                p = m.group(1).replace("\\", "/")
                if p.startswith("reports/"):
                    tier_a_names.add(p.split("/")[-1])
                    refs.append({"src": rel, "line": i, "report": p,
                                 "op": classify_ctx(line, _win(lines, i), file_hint.get(rel, "")),
                                 "conf": "DIRECT", "text": line.strip()[:160]})
    # 第二遍：目录变量上下文 + 已确认报告名（同名不同目录时按 basename 归并并标注）。
    # corpus 本身已过滤"文件里出现过 reports 目录"，
    # 故 f-string 拼路径这类整行都不含 reports 的写法也能靠文件级上下文捞回来。
    for rel, _fp, lines in corpus:
        text_all = "\n".join(lines)
        file_ctx = ("reports" in text_all.lower())
        for i, line in enumerate(lines, 1):
            for m in RE_ANY_REPORT_STR.finditer(line):
                name = m.group(1).replace("\\", "/").split("/")[-1]
                ctx_ok = file_ctx or ("reports" in line.lower()) or any(
                    re.search(r"\b%s\b" % re.escape(v), line) for v in dvar)
                if not ctx_ok and name not in tier_a_names:
                    continue
                if any(r["src"] == rel and r["line"] == i for r in refs):
                    continue
                refs.append({"src": rel, "line": i, "report": "reports/" + name,
                             "op": classify_ctx(line, _win(lines, i), file_hint.get(rel, "")),
                             "conf": "INFERRED" if not ctx_ok else "DIRECT",
                             "text": line.strip()[:160]})
    # 第三遍：脚本名与产物同 stem 兜底——写方走 `--out base` + 后缀拼接时正文根本没有
    # 文件名字面量（如 scripts/audit_bridge_auth.py 用 `open(out_base + ".json","w")` 写报告，
    # 文件名只出现在 `--out` 默认值与 docstring 里）。此时整文件归属该 stem 的产物。
    index_by_stem = {}
    for r in refs:
        index_by_stem.setdefault(r["report"].split("/")[-1].rsplit(".", 1)[0], set()).add(
            r["report"].split("/")[-1])
    for rel, _fp, lines in corpus:
        own_stem = os.path.basename(rel)[:-3] if rel.endswith(".py") else ""
        if not own_stem or own_stem not in index_by_stem:
            continue
        has_write = any(RE_DUMP.search(l) or RE_OPEN_W.search(l) for l in lines)
        has_read = any(RE_READ.search(l) for l in lines)
        hint = "WRITE" if (has_write or has_read) and not (
            has_read and not has_write) else ("READ" if has_read else "")
        if not hint:
            continue
        for name in sorted(index_by_stem[own_stem]):
            # 仅当已有同 (src,产物) 且确实判成 WRITE 时才跳过：
            # 若那条只是 UNKNOWN（行级证据不足），仍要由 STEM 规则补上"这是生产者"的结论
            if any(r["src"] == rel and r["report"].endswith(name) and r["op"] == "WRITE"
                   for r in refs):
                continue
            refs.append({"src": rel, "line": 0, "report": "reports/" + name,
                         "op": hint, "conf": "STEM", "text": "<stem 归属，见脚本本身>"})
    return refs


# ---------------------------------------------------------------------------
# 3) 值级传播（跨 report 共享字面量）
# ---------------------------------------------------------------------------
# 只有"控制类键"的布尔值才算信号（`"HIGH": true` 这种分类标签满地都是，全是噪声）
CONTROL_FLAG_KEYS = {
    "suggest", "drift", "drift_alert", "alert", "retrain", "retrain_suggest",
    "enabled", "active", "valid", "stale", "ok", "pass", "fail", "healthy",
    "degraded", "live", "flap", "overdue", "missing", "critical", "warn",
    "is_edge", "no_edge", "inconclusive", "confirmed", "verified", "disabled",
    "should_train", "gate", "state", "critical_risk", "high_risk",
}
# 强控制键才会被判 HIGH：它们描述"系统状态"而非"这份报告自己的结论"
CONTROL_STRONG_KEYS = {
    "suggest", "drift", "drift_alert", "alert", "retrain", "retrain_suggest",
    "live", "flap", "overdue", "is_edge", "no_edge", "inconclusive",
    "should_train", "critical_risk", "high_risk",
}
RE_KV_BOOL = re.compile(r"""["']([A-Za-z_][A-Za-z0-9_]*)["']\s*:\s*(true|false|True|False)""")


def extract_literals(text: str):
    """抽报告里的"可传播字面量"：控制类布尔开关 + 数值（排除日期片段）。"""
    vals = []
    stripped = RE_DATE.sub(" ", text)
    for m in RE_KV_BOOL.finditer(stripped):
        key = m.group(1).lower()
        if key in CONTROL_FLAG_KEYS:
            vals.append(("FLAG", "%s=%s" % (key, m.group(2).lower())))
    for m in RE_NUM.finditer(stripped):
        try:
            vals.append(("NUM", repr(float(m.group(0)))))
        except ValueError:
            pass
    for m in RE_INT.finditer(stripped):
        v = m.group(0)
        if not v.startswith("-"):
            vals.append(("NUM", v))
    return vals


def build_propagation(index, root=ROOT):
    """value -> [report names]。分两级信号：

  - FLAG（布尔开关）：>=2 份产物共享即算传播。强控制键（suggest/drift/retrain/...）跨
    >=3 份才判 HIGH；弱判定键（pass/fail/ok/valid 这类分档标签）跨多少份都只 MEDIUM
    ——因为它们是各产物自己算出来的，不构成"上游翻转不跟"的链路。
  - NUM（数值）：跨 >=4 份产物只作背景噪声，不判高危（否则 0.5/2 之类会淹没结论）。
    """
    buckets = {}
    per_report = {}
    for it in index:
        text = _read_text(os.path.join(root, "reports", it["name"]))
        if not text:
            continue
        seen = set()
        for kind, val in extract_literals(text):
            if (kind, val) in seen:
                continue
            seen.add((kind, val))
            buckets.setdefault((kind, val), []).append(it["name"])
        per_report[it["name"]] = sorted(seen)
    flags, nums = {}, {}
    for (kind, val), names in buckets.items():
        uniq = sorted(set(names))
        if len(uniq) < 2:
            continue
        if kind == "FLAG":
            key = val.split("=", 1)[0]
            if len(uniq) >= 3 and key in CONTROL_STRONG_KEYS:
                flags["FLAG:%s" % val] = uniq          # 强控制键跨产物 = 真传播链
            elif len(uniq) >= 6:
                flags["FLAG:%s" % val] = uniq          # 弱判定键只作信息共享
        elif len(uniq) >= 4:
            nums["NUM:%s" % val] = uniq
    return {"flags": flags, "nums": nums}, per_report


# ---------------------------------------------------------------------------
# 4) 伪活引用 / 手写快照 / 死产物
# ---------------------------------------------------------------------------
def _mean_old(ts_list):
    return min(ts_list) if ts_list else 0.0


def find_pseudo_live(refs, index, root=ROOT):
    """下游产物落后于它的上游输入 = 伪活引用（读者当成"最新消费结果"，实际要人工重跑）。

    两条规则：(a) 直连——同一脚本既读某报告又写产物；(b) 生产者级——脚本读过的任何
    报告都算其全部产物的上游（覆盖"读 A 写 B/C"但 A、B 不同行的情况）。
    """
    idx = index if isinstance(index, dict) else {it["name"]: it for it in index}
    by_src = {}
    for r in refs:
        by_src.setdefault(r["src"], []).append(r)
    out = []

    def _emit(src, input_name, output_names, in_mt, out_mts):
        out.append({
            "src": src,
            "input": input_name,
            "output": sorted(set(output_names)),
            "input_mtime": idx[input_name]["mtime_iso"],
            "output_mtime": datetime.fromtimestamp(min(out_mts)).strftime("%Y-%m-%d %H:%M"),
            "lag_days": round((in_mt - min(out_mts)) / 86400.0, 2),
        })

    for src, rlist in by_src.items():
        written = [r["report"].split("/")[-1] for r in rlist if r["op"] == "WRITE"]
        if not written:
            continue
        out_mts = [idx[w]["mtime"] for w in written if w in idx]
        if not out_mts:
            continue
        min_out = min(out_mts)
        read_names = sorted({r["report"].split("/")[-1] for r in rlist
                             if r["op"] in ("READ", "UNKNOWN")})
        # (a) 直连
        for rn in read_names:
            if rn in idx and idx[rn]["mtime"] > min_out + 1:
                _emit(src, rn, written, idx[rn]["mtime"], out_mts)
        # (b) 生产者级
        for w in sorted(set(x for x in written if x in idx)):
            newer = [n for n in read_names if n in idx and idx[n]["mtime"] > idx[w]["mtime"] + 1]
            if newer:
                _emit(src, newer[0], [w], idx[newer[0]]["mtime"], [idx[w]["mtime"]])
    # 同一 (脚本, 上游) 可能对应多份产物（.json + .md），合并成一条，避免刷屏
    merged = {}
    for p in out:
        key = (p["src"], p["input"], p["input_mtime"])
        if key in merged:
            merged[key]["output"] = sorted(set(merged[key]["output"]) | set(p["output"]))
        else:
            merged[key] = dict(p)
    return sorted(merged.values(), key=lambda x: (-x["lag_days"], x["src"]))


def names_of(propagation, name):
    """该产物共享了哪些值组（用于报告里统计"共享了多少份"）。"""
    hit = []
    for kind, bucket in propagation.items():
        if isinstance(bucket, dict):
            for k, names in bucket.items():
                if name in names:
                    hit.extend(names)
    return sorted(set(hit))


STRIP_PREFIX_TOKENS = {"monitor", "report", "model", "cur", "current", "new", "is",
                       "has", "no", "pred", "last", "gate", "calib", "sys"}


def norm_flag(key: str):
    """把 `monitor_retrain_suggest` / `retrain_suggest` 归一到 `('retrain','suggest')`。

    踩坑：下游产物常给抄来的键加前缀（`monitor_drift_alert`），只比全名会漏掉整条链路。
    """
    toks = [t for t in key.lower().split("_") if t]
    while toks and toks[0] in STRIP_PREFIX_TOKENS:
        toks = toks[1:]
    return tuple(toks)


def find_value_copy(refs, index, root=ROOT):
    """证伪"活引用"：同一脚本既读上游报告 A 又写出产物 B，且 B 里的控制键值 literal 也出现在 A 里。

    T38③ 就是这一类：`audit_stale_reports.py` 读 `monitor_status.json`（retrain_gate.suggest）
    再判 `INVALIDATED_BY_RETRAIN`。这里不靠猜，而是直接比对文本里 `key: value` 是否同源。
    """
    idx = {it["name"]: it for it in index}
    rdir = os.path.join(root, "reports")
    by_src = {}
    for r in refs:
        by_src.setdefault(r["src"], []).append(r)
    out = []
    for src, rlist in by_src.items():
        # UNKNOWN 一并按"可能"计入（ polarity 判不准时宁可检测也不放过），
        # 但真正的 VALUE_COPY 认定仍要求Literal 同源，不会凭关系名乱判。
        written = sorted({r["report"].split("/")[-1] for r in rlist
                          if r["op"] in ("WRITE", "UNKNOWN")})
        read = sorted({r["report"].split("/")[-1] for r in rlist
                       if r["op"] in ("READ", "UNKNOWN")})
        for a in read:
            if a not in idx:
                continue
            a_txt = _read_text(os.path.join(rdir, a))
            for b in written:
                if b == a or b not in idx:
                    continue
                if a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]:
                    continue      # json/md 同族兄弟不是"抄"链
                # 方向常识：值只能从新流到旧（input mtime 必须晚于 output），
                # 否则"抄"会指向未来，属于误判。
                if idx[a]["mtime"] <= idx[b]["mtime"]:
                    continue
                b_txt = _read_text(os.path.join(rdir, b))
                if not a_txt or not b_txt:
                    continue
                a_flags = {}
                for m in RE_KV_BOOL.finditer(a_txt):
                    a_flags.setdefault(frozenset(norm_flag(m.group(1))), m.group(2).lower())
                hits = []
                for m in RE_KV_BOOL.finditer(b_txt):
                    key, val = m.group(1), m.group(2).lower()
                    if key.lower() not in CONTROL_FLAG_KEYS:
                        continue
                    b_tok = frozenset(norm_flag(key))
                    # 子集/超集归一：monitor_retrain_suggest ↔ retrain_suggest 视为同一个开关
                    same = [v for t, v in a_flags.items()
                            if v == val and (t <= b_tok or b_tok <= t)]
                    if same:
                        hits.append("%s=%s(<-上游 %s)" % (key, val, ",".join(sorted(b_tok))))
                # 判定类别字符串同源也算（如 INVALIDATED_BY_RETRAIN 抄自上游的 verdict 语法）
                a_cats = set(re.findall(r"""["']([A-Z][A-Z0-9_]{4,})["']""", a_txt))
                for cat in set(re.findall(r"""["']([A-Z][A-Z0-9_]{4,})["']""", b_txt)):
                    if cat in a_cats and cat not in hits:
                        hits.append(":%s" % cat)
                if hits:
                    out.append({"src": src, "input": a, "output": b,
                                "copied_flags": sorted(set(hits))})
    return out


def classify_reports(index, refs, propagation, pseudo_live, value_copy=None):
    """每份报告一个风险标签。

    HIGH：① 无生产者但被消费（手写快照，上游变了它不变）② 开关值被 >=3 份产物共享
          ③ 伪活引用（落后于输入）。
    MEDIUM：无消费者（死产物）或手写快照但无人消费。
    OK：有生产者和消费者，且值不跨产物传播。
    """
    producers, consumers, weak_consumers = {}, {}, {}
    for r in refs:
        name = r["report"].split("/")[-1]
        if r["op"] == "WRITE":
            producers.setdefault(name, []).append("%s:%s" % (r["src"], r["line"]))
        elif r["op"] in ("READ", "UNKNOWN"):
            label = "%s:%s[%s]" % (r["src"], r["line"], r["op"])
            # 只有 UNKNOWN 极性的"消费"不算证据——读行判不出来时，那条引用更可能
            # 是写方自己的路径赋值（`OUT = join(REPORTS,"x.json")`）。宁可降级也不虚报。
            (consumers if r["op"] == "READ" else weak_consumers).setdefault(
                name, []).append(label)
    stale_inputs = {}
    for p in pseudo_live:
        stale_inputs.setdefault(p["src"], []).extend(p["input"])
    copied_products = {}
    for c in (value_copy or []):
        copied_products.setdefault(c["output"], []).append(
            "%s ← %s(%s)" % (c["src"], c["input"], ",".join(c["copied_flags"])))

    out = []
    for it in index:
        name = it["name"]
        flags = []
        sev = "OK"
        confirmed_cons = consumers.get(name) or []
        weak_cons = weak_consumers.get(name) or []
        if not producers.get(name):
            flags.append("NO_PRODUCER(手写或一次性产物，上游变了它不变)")
            sev = "MEDIUM"
            if confirmed_cons:
                sev = "HIGH"        # 只有确实有人读它，才值得升 HIGH
            elif weak_cons:
                flags.append("CONSUMER_POLARITY_UNKNOWN(疑似被消费但读/写判不准，待人工确认)")
        if not confirmed_cons and not weak_cons:
            flags.append("NO_CONSUMER(死产物，无人二次消费)")
            sev = "MEDIUM" if sev == "OK" else sev
        hit_flags = [k for k, names in propagation["flags"].items() if name in names]
        if hit_flags:
            n_share = len(names_of(propagation, name))
            flags.append("PROPAGATED_FLAG(%s；与 %d 份产物共享同一开关值，上游翻转不自动跟)"
                         % (",".join(hit_flags)[:60], n_share))
            sev = "HIGH"
        if copied_products.get(name):
            flags.append("VALUE_COPY(被脚本抄走上游控制键值，非重算: %s)"
                         % "; ".join(copied_products[name])[:120])
            sev = "HIGH"
        if stale_inputs.get(name):
            flags.append("PSEUDO_LIVE(被消费但落后于输入，须人工重跑)")
            sev = "HIGH"
        out.append({
            "name": name,
            "severity": sev,
            "flags": flags,
            "producers": producers.get(name, []),
            "consumers": consumers.get(name, []),
            "weak_consumers": weak_consumers.get(name, []),
            "value_copy": copied_products.get(name, []),
            "mtime": it["mtime_iso"],
            "size_kb": round(it["size"] / 1024.0, 1),
        })
    order = {"HIGH": 0, "MEDIUM": 1, "OK": 2}
    return sorted(out, key=lambda x: (order.get(x["severity"], 3), x["name"]))


# ---------------------------------------------------------------------------
# 5) 报告渲染
# ---------------------------------------------------------------------------
def render_md(rep):
    L = []
    L.append("# T42 reports/ 产物二次消费风险盘点（只读）")
    L.append("")
    L += ["- as_of: `%s`" % rep["as_of"],
          "- 口径: reports/ 顶层 %d 份 .json/.md；源码扫描面 %s"
          % (rep["scope"]["n_reports"], ", ".join(rep["scope"]["src_dirs"])),
          "- 红线: 只读 / 不重训 / 不碰调度 / 零 events.db 写入 / 不改任何引用处",
          "- 本报告由 automation T42 生成，未修改任何被盘点文件", "", "## 1 概览", ""]
    s = rep["summary"]
    L += ["- 报告总数 `%d`；HIGH `%d` / MEDIUM `%d` / OK `%d`"
          % (s["total"], s["by_severity"].get("HIGH", 0),
             s["by_severity"].get("MEDIUM", 0), s["by_severity"].get("OK", 0)),
          "- 源码引用 `reports/` 共 `%d` 处（WRITE %d / READ %d / UNKNOWN %d）"
          % (s["refs_total"], s["refs_write"], s["refs_read"], s["refs_unknown"]),
          "- 伪活引用（输出落后于输入）`%d` 条" % s["pseudo_live"],
          "- 布尔开关跨产物共享 `%d` 组 / 数值背景噪声 `%d` 组"
          % (s["propagated_flag_groups"], s["propagated_num_groups"]), ""]
    L += ["## 2 高风险产物", ""]
    hi = [r for r in rep["reports"] if r["severity"] == "HIGH"]
    if not hi:
        L.append("- 无")
    for r in hi:
        L.append("### %s" % r["name"])
        L += ["- mtime %s / %.1fKB" % (r["mtime"], r["size_kb"]),
              "- flags: %s" % ("; ".join(r["flags"]) or "-"),
              "- producers: %s" % (", ".join(r["producers"]) or "-"),
              "- consumers: %s" % (", ".join(r["consumers"]) or "-"),
              "- 疑似消费(极性未定): %s" % (", ".join(r.get("weak_consumers") or []) or "-"), ""]
    L += ["## 3 伪活引用（必须人工重跑）", ""]
    if not rep["pseudo_live"]:
        L.append("- 无")
    for p in rep["pseudo_live"][:60]:
        L.append("- `%s` 读 `%s`(mtime %s) → 产出 %s(mtime %s) 落后 %.1f 天"
                 % (p["src"], p["input"], p["input_mtime"],
                    ",".join(p["output"]), p["output_mtime"], p["lag_days"]))
    if len(rep["pseudo_live"]) > 60:
        L.append("- ... 其余 %d 条见 json" % (len(rep["pseudo_live"]) - 60))
    L += ["", "## 4 值级传播（跨产物共享字面量）", "",
          "### 4.1 布尔开关（>=2 份产物共享即传播）", ""]
    for k, names in list(rep["propagation"]["flags"].items())[:40]:
        L.append("- `%s` → %d 份: %s" % (k, len(names), ", ".join(names[:8])))
    L += ["", "### 4.2 数值（>=4 份产物共享，背景噪声）", ""]
    for k, names in list(rep["propagation"]["nums"].items())[:15]:
        L.append("- `%s` → %d 份: %s" % (k, len(names), ", ".join(names[:6])))
    L += ["", "## 5 建议（仅建议，本轮不改任何生产文件）", "", rep["recommendation"], ""]
    L += ["## 6 全量产物清单（按风险序）", ""]
    for r in rep["reports"]:
        L.append("- [%s] %s (%s, %.1fKB, %d prod/%d cons)"
                 % (r["severity"][:4], r["name"], r["mtime"], r["size_kb"],
                    len(r["producers"]), len(r["consumers"])))
    return "\n".join(L) + "\n"


def build_report(index, refs, propagation, pseudo_live, reports, value_copy=None):
    sev = {}
    for r in reports:
        sev[r["severity"]] = sev.get(r["severity"], 0) + 1
    ref_ops = {"WRITE": 0, "READ": 0, "UNKNOWN": 0}
    for r in refs:
        ref_ops[r["op"]] = ref_ops.get(r["op"], 0) + 1
    rep = {
        "task": "T42 reports/ 产物二次消费风险只读盘点",
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scope": {"n_reports": len(index), "src_dirs": SRC_SCAN_DIRS + SRC_SCAN_EXTRA_FILES},
        "redlines": ["只读 / 不重训 / 不碰调度 / 零 events.db 写入 / 不改任何引用处"],
        "summary": {
            "total": len(index),
            "by_severity": sev,
            "refs_total": len(refs),
            "refs_write": ref_ops.get("WRITE", 0),
            "refs_read": ref_ops.get("READ", 0),
            "refs_unknown": ref_ops.get("UNKNOWN", 0),
            "pseudo_live": len(pseudo_live),
            "value_copy": len(value_copy or []),
            "propagated_flag_groups": len(propagation["flags"]),
            "propagated_num_groups": len(propagation["nums"]),
        },
        "reports": reports,
        "pseudo_live": pseudo_live,
        "value_copy": value_copy or [],
        "propagation": {
            "flags": dict(sorted(propagation["flags"].items(),
                                 key=lambda kv: (-len(kv[1]), kv[0]))),
            "nums": dict(sorted(propagation["nums"].items(),
                                key=lambda kv: (-len(kv[1]), kv[0]))[:30]),
        },
    }
    rep["findings"] = [
        {"id": "F1", "severity": "HIGH" if rep["summary"]["pseudo_live"] else "OK",
         "title": "伪活引用：consumer 输出落后于输入",
         "detail": ("%d 条：脚本读到的输入报告 mtime 已新于它写出的产物，说明输入变了而产物没跟着"
                    "重跑。读者会把这些产物当成『最新一次消费结果』，实际是必须人工重跑的快照。"
                    % rep["summary"]["pseudo_live"])},
        {"id": "F2", "severity": "HIGH" if rep["summary"]["propagated_flag_groups"] else "OK",
         "title": "值级传播：一个开关被多份产物共享",
         "detail": ("布尔开关 %d 组被 >=2 份产物共享（最多 %d 份）；任一产物翻转不会自动更新其他"
                    "产物，读者无法判断谁最新。含 T38③『monitor 开关 → 89 份报告判死』链路。"
                    % (rep["summary"]["propagated_flag_groups"],
                       max([len(v) for v in rep["propagation"]["flags"].values()] or [0])))},
        {"id": "F3", "severity": "MEDIUM",
         "title": "无生产者 / 无消费者",
         "detail": "NO_PRODUCER = 手写或一次性产物（上游变了它不变）；NO_CONSUMER = 死产物。"},
    ]
    rep["recommendation"] = (
        "① 给 reports/ 产物统一加 `_meta{as_of, source_of_truth, stale_of}`：写方在写出时记录"
        "被读取的上游文件 mtime，下游读到时即可自证时效（生产代码改动，走全量 pytest，本轮不落地）；"
        "② 上述 %d 条伪活引用产生方在每次 run 末尾打印 lag 并在文件名带 as_of，禁止裸文件名被当最新；"
        "③ 对 PROPAGATED_LITERAL 的布尔开关（如 retrain_gate.suggest）改用『判定式引用』而非"
        "『值引用』——下游脚本应重算条件而不是抄一个 true/false；"
        "④ T17 的 INVALIDATED_BY_RETRAIN 判定式收回（T37 R3 已提出），配合 ① 的 stale_of 自动复活。"
        % rep["summary"]["pseudo_live"])
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT, help="仓库根（测试可指向 tmp）")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    index = collect_report_index(root)
    refs = scan_source_refs(root)
    propagation, _per = build_propagation(index, root)
    pseudo_live = find_pseudo_live(refs, index, root)
    value_copy = find_value_copy(refs, index, root)
    reports = classify_reports(index, refs, propagation, pseudo_live, value_copy)
    rep = build_report(index, refs, propagation, pseudo_live, reports, value_copy)
    rdir = os.path.join(root, "reports")
    os.makedirs(rdir, exist_ok=True)
    # 只落本脚本自己的两份产物，且恒在 root/reports/ 下（测试可指向 tmp 而不污染生产）
    with open(os.path.join(rdir, os.path.basename(OUT_JSON)),
              "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    with open(os.path.join(rdir, os.path.basename(OUT_MD)),
              "w", encoding="utf-8") as f:
        f.write(render_md(rep))
    s = rep["summary"]
    print("[OK] reports/report_secondary_consumption_audit.{json,md}")
    print("reports=%d refs=%d(write %d/read %d) pseudo_live=%d flags=%d sev=%s"
          % (s["total"], s["refs_total"], s["refs_write"], s["refs_read"],
             s["pseudo_live"], s["propagated_flag_groups"], s["by_severity"]))


if __name__ == "__main__":
    main()
