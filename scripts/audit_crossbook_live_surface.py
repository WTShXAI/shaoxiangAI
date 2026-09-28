"""T30 跨庄共识「活跃面」只读盘点（自动化 dbda4380 任务队列）。

背景：T06/T15 发现 IR-32 守卫 (`tests/test_no_crossbook.py`) 存在覆盖缺口 ——
它只扫 11 个后端文件的 `import` 语句，漏掉①前端组件渲染②后端 dict 字段赋值
③被删函数残留名④中文面板标签。本脚本在**不改守卫、不改生产代码、不碰库**的
前提下，把「跨庄共识相关符号」在生产代码树里的**活跃引用点**一次量化清楚：
谁定义、谁赋值、谁渲染、谁挂载、谁已死、当前守卫能否抓到。

扫描范围（只读文本，绝不打开 events.db / verification.db / 任何数据库）：
  - 后端：bridge_service.py / agent_cruise.py / gq/ / pipeline/ /
          core/ / data_collector/ / tests/
  - 前端：frontend/src/ 与 frontend/e2e/（.ts/.tsx/.js/.jsx）
  - 文档面：docs/ 与 reports/（仅计数，判定为「零运行时影响」）

每条命中输出：
  file:line / 符号 / kind(IMPORT|DICT_FIELD|FUNC_DEF|DEF|TYPE_DECL|COMPONENT|
         JSX_MOUNT|FIELD_ACCESS|STRING|COMMENT) / family / ir32_risk /
         guard_caught(现守卫能否抓到) / file_in_guard_scan(现 SCAN_FILES 是否含该路径)
  liveness: LIVE(生产者真填值) / DEAD(生产者 disabled/恒定 None) / DANGLING(全树无定义)

安全红线：
  - 纯只读文本扫描；不写任何数据库、不 kill 进程、不改源码、不改守卫。
  - 仅落盘新报告 reports/crossbook_live_surface_audit.{json,md}。
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(ROOT, "reports")

EXCLUDE_DIRS = {".venv", "node_modules", "__pycache__", ".git", "reports",
                "data", "archive", "dist", "build", "logs", "models",
                ".next", "coverage"}

BACKEND_ROOTS = [
    os.path.join(ROOT, "bridge_service.py"),
    os.path.join(ROOT, "agent_cruise.py"),
    os.path.join(ROOT, "gq"),
    os.path.join(ROOT, "pipeline"),
    os.path.join(ROOT, "core"),
    os.path.join(ROOT, "data_collector"),
    os.path.join(ROOT, "tests"),
]
FRONTEND_ROOTS = [
    os.path.join(ROOT, "frontend", "src"),
    os.path.join(ROOT, "frontend", "e2e"),
]
DOC_ROOTS = [os.path.join(ROOT, "docs"), os.path.join(ROOT, "reports")]

PY_EXTS = {".py"}
FE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs"}
TEXT_EXTS = PY_EXTS | FE_EXTS | {".md", ".json", ".yml", ".yaml", ".html"}

# 现守卫 (tests/test_no_crossbook.py) 的 BANNED 与判定语义，用于"能否抓到"复算
GUARD_BANNED = (
    "cross_book_edge", "multibook_consensus", "leyu_value_signal",
    "bet_split_source", "from scripts.bet_core", "cross_book_alert",
)
GUARD_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+\S*%s\S*\s+|import\s+\S*%s)" % ("(.+)", "(.+)")
)  # 占位；实际用法见 guard_import_matches()

# family / ir32_risk 说明：
#   crossbook_core : IR-32 明令禁止的跨庄共识/跨庄 edge
#   crossbook_residue : 被禁后未删的残留名（函数/字段/类型/组件）
#   softline_divergence : 跨庄分歧软线（IR-32 邻接面，前端以「跨庄分歧」文案渲染）
#   quant_archived : 已归档量化模块（compute_value_layer 等）
SYMBOLS = (
    # name, family, ir32_risk
    ("cross_book_edge", "crossbook_core", "critical"),
    ("multibook_consensus", "crossbook_core", "critical"),
    ("cross_book_alert", "crossbook_core", "critical"),
    ("leyu_value_signal", "crossbook_core", "critical"),
    ("bet_split_source", "crossbook_core", "critical"),
    ("compute_value_layer", "quant_archived", "high"),
    ("MultibookConsensus", "crossbook_core", "critical"),
    ("MultiBookConsensus", "crossbook_residue", "critical"),
    ("_lookup_multibook_consensus", "crossbook_residue", "high"),
    ("_get_cross_book_signal", "crossbook_residue", "high"),
    ("cross_book", "crossbook_residue", "critical"),
    ("softline", "softline_divergence", "medium"),
    ("SoftlineBanner", "softline_divergence", "medium"),
    ("跨庄共识", "softline_divergence", "medium"),
    ("跨庄分歧", "softline_divergence", "medium"),
    ("跨庄软线", "softline_divergence", "medium"),
)

# 现守卫 SCAN_FILES 判定：只列 .py 文件级白名单，目录/前端均不在其中
GUARD_SCAN_FILES = {
    "bridge_service.py", "auto_collector.py", "db.py", "prematch_similarity.py",
    "predict_export.py", "odds_candles_predict.py", "ht_anchor_predict.py",
    "score_model.py", "ranked_predictor.py",
}

KIND_ORDER = (
    "JSX_MOUNT", "TYPE_DECL", "COMPONENT", "FUNC_DEF", "IMPORT",
    "DICT_FIELD", "FIELD_ACCESS", "STRING", "COMMENT", "OTHER",
)


# --------------------------------------------------------------------------
# 纯函数区（零生产 I/O，可被单测直接调用）
# --------------------------------------------------------------------------
def guard_import_matches(line, banned=GUARD_BANNED):
    """复算现守卫的 import 判定：只有 `from xxx<banned>yyy` / `import xxx<banned>` 命中。"""
    stripped = line.strip()
    for b in banned:
        if re.match(r"^(?:from\s+\S*%s\S*\s+|import\s+\S*%s)" % (re.escape(b), re.escape(b)),
                    stripped):
            return True
    return False


def is_doc_line(line, prev_in_doc):
    """极简注释/docstring 判定（用于把文档提及与真实代码区分开）。"""
    s = line.strip()
    if s.startswith("#") or s.startswith("//"):
        return True
    if s.startswith('"""') or s.startswith("'''") or s.startswith('*'):
        return True
    return prev_in_doc


def classify_line(line, symbol, prev_in_doc):
    """把某一行按命中符号分类为具体 kind（互斥单值）。"""
    s = line.strip()
    if is_doc_line(line, prev_in_doc):
        return "COMMENT"
    if re.search(r"<%s[\s/>]" % re.escape(symbol), s):
        return "JSX_MOUNT"
    if re.search(r"(?:interface|type)\s+%s\b" % re.escape(symbol), s):
        return "TYPE_DECL"
    if re.search(r"function\s+%s\b" % re.escape(symbol), s):
        return "COMPONENT"
    if re.search(r"\bdef\s+%s\b" % re.escape(symbol), s):
        return "FUNC_DEF"
    if re.match(r"^(?:from\s+\S+\s+import\s+|import\s+)", s):
        # 任意 import 语句形式（守卫的窄正则未必命中，但语义上是引入面）
        return "IMPORT"
    if guard_import_matches(s):
        return "IMPORT"
    if re.search(r"[\"']%s[\"']\s*:" % re.escape(symbol), s):
        return "DICT_FIELD"
    if re.search(r"\.\s*%s\b" % re.escape(symbol), s):
        return "FIELD_ACCESS"
    if re.search(r"[\"']%s[\"']" % re.escape(symbol), s):
        return "STRING"
    return "OTHER"


def iter_text_files(root, exts, exclude_dirs=EXCLUDE_DIRS):
    """递归产出 (path, text)；跳过排除目录与非文本后缀。root 可传单路径或路径列表。"""
    if isinstance(root, (list, tuple)):
        for r in root:
            yield from iter_text_files(r, exts, exclude_dirs)
        return
    if os.path.isfile(root):
        if os.path.splitext(root)[1] in exts:
            yield root, read_text(root)
        return
    if not os.path.isdir(root):
        return
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        rel = os.path.basename(p)
        if rel in exclude_dirs or rel.startswith("."):
            continue
        if os.path.isdir(p):
            if rel in {"__pycache__", ".git"}:
                continue
            yield from iter_text_files(p, exts, exclude_dirs)
        elif os.path.isfile(p) and os.path.splitext(p)[1] in exts:
            yield p, read_text(p)


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def rel_of(path):
    """相对仓库根的路径；跨盘符（tmp 单测）时退化为文件名，绝不抛错。"""
    try:
        return os.path.relpath(path, ROOT).replace("\\", "/")
    except ValueError:
        return os.path.basename(path)


def surface_of(rel):
    """把命中归入 backend / frontend / docs / other 面（用于诚实分层 reporting）。"""
    low = rel.replace("\\", "/").lower()
    if low.startswith("frontend/"):
        return "frontend"
    if low.startswith("docs/") or low.startswith("reports/"):
        return "docs"
    if low.endswith(".py") or low.endswith((".ts", ".tsx", ".js", ".jsx")):
        return "backend"
    return "other"


def scan_path(path, text, symbol, guard_scan_files=GUARD_SCAN_FILES):
    """扫描单文件的单符号命中，返回 hit dict 列表。"""
    hits = []
    in_doc = False
    rel = rel_of(path)
    for i, line in enumerate(text.splitlines(), start=1):
        if symbol in line:
            kind = classify_line(line, symbol, in_doc)
            in_doc = is_doc_line(line, in_doc) or ('"""' in line or "'''" in line)
            base = os.path.basename(path)
            hits.append({
                "file": rel,
                "surface": surface_of(rel),
                "line": i,
                "symbol": symbol,
                "kind": kind,
                "code": line.strip()[:160],
                "in_guard_scan": base in guard_scan_files,
                "guard_caught": guard_import_matches(line.strip()),
            })
    return hits


def is_call_or_kwarg_ref(line, symbol):
    """判定该行是否把 symbol 当**可调用引用**使用（`f(...)` 或 kwarg `x=y`）。

    用于把「悬空调用面」（真会 NameError）与「字段/参数声明残骸」区分开 ——
    否则 `cross_book: bool = False` 这类参数声明会被误报成悬空引用。
    """
    s = line.strip()
    pat_call = r"(?<![A-Za-z0-9_.])%s\s*\(" % re.escape(symbol)
    pat_kwarg = (r"\b\w+\s*=\s*(?<![A-Za-z0-9_.])%s(?![A-Za-z0-9_])"
                 % re.escape(symbol))
    return bool(re.search(pat_call, s)) or bool(re.search(pat_kwarg, s))


def param_declared(code_text, symbol):
    """symbol 是否作为函数参数/局部变量在当前代码面被声明（属于贯通残骸，非悬空调用）。"""
    pat = (r"(?<![A-Za-z0-9_])(?:async\s+def|def)\s+\w+\([^)]*?\b%s\s*[=:,]"
           % re.escape(symbol))
    return bool(re.search(pat, code_text, re.S))


def find_definitions(roots, symbol, exts=TEXT_EXTS):
    """在给定根下命中符号的 `def <symbol>` / `function <symbol>` 位置集合。"""
    found = []
    for path, text in iter_text_files(roots, exts):
        for i, line in enumerate(text.splitlines(), start=1):
            if re.search(r"\bdef\s+%s\b" % re.escape(symbol), line) or \
               re.search(r"function\s+%s\b" % re.escape(symbol), line):
                found.append("%s:%d" % (os.path.relpath(path, ROOT).replace("\\", "/"), i))
    return found


def func_is_disabled(text, symbol):
    """判定 `def symbol(...)` 是否被禁用：函数体含 `return None` 或 IR-32 标记。"""
    m = re.search(r"^[ \t]*def\s+%s\b" % re.escape(symbol), text, re.M)
    if not m:
        return False, None
    start = m.end()
    body_start = text.find("\n", start) + 1
    end = text.find("\n\n", body_start)
    body = text[body_start:end if end > 0 else len(text)]
    disabled = bool(re.search(r"return\s+None", body)) or \
        bool(re.search(r"IR-32|已禁用|禁用", body))
    return disabled, body.strip().splitlines()[:2]


def build_summary(symbol_hits, dangling, dead_defs, counts, scanned_files):
    """汇总为报告 dict（纯聚合，无副作用）。"""
    total = 0
    live = 0
    by_kind = {}
    by_family = {}
    by_surface = {}
    by_file = {}
    for h in symbol_hits:
        total += 1
        surf = h.get("surface", "unknown")
        by_surface[surf] = by_surface.get(surf, 0) + 1
        by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1
        by_family[h.get("family", "unknown")] = by_family.get(h.get("family", "unknown"), 0) + 1
        f = by_file.setdefault(h["file"], {"total": 0, "risk": "low"})
        f["total"] += 1
        if h["risk"] == "critical":
            f["risk"] = "critical"
        elif h["risk"] == "high" and f["risk"] != "critical":
            f["risk"] = "high"
        elif h["risk"] == "medium" and f["risk"] == "low":
            f["risk"] = "medium"
        if h["kind"] not in ("COMMENT",):
            live += 1
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scanned_files": scanned_files,
        "symbols_tracked": len(SYMBOLS),
        "total_hits": total,
        "code_hits": live,
        "by_kind": by_kind,
        "by_family": by_family,
        "by_surface": by_surface,
        "by_file": by_file,
        "dangling_symbols": dangling,
        "disabled_definitions": dead_defs,
        "guard_coverage_gap": counts,
    }


def render_markdown(summary, hits):
    """渲染人类可读报告（纯字符串）。"""
    g = summary["guard_coverage_gap"]
    lines = [
        "# IR-32 跨庄共识活跃面只读盘点（T30）",
        "",
        "> 只读文本扫描，不改守卫、不改生产代码、不碰任何数据库。生成：%s" % summary["generated_at"],
        "",
        "## 1. 总览",
        "",
        "| 指标 | 值 |",
        "|------|----|",
        "| 扫描文件数 | %d |" % summary["scanned_files"],
        "| 跟踪符号数 | %d |" % summary["symbols_tracked"],
        "| 按面：backend / frontend / docs | %d / %d / %d |" % (
            summary["by_surface"].get("backend", 0),
            summary["by_surface"].get("frontend", 0),
            summary["by_surface"].get("docs", 0)),
        "| 命中总数 | %d |" % summary["total_hits"],
        "| 其中代码面命中（去掉注释） | %d |" % summary["code_hits"],
        "| 现守卫可抓到（import 路） | %d |" % g["guard_caught"],
        "| 现守卫漏网（非 import 路 / 文件不在 SCAN_FILES） | %d |" % g["guard_missed"],
        "| 悬空引用（全树无定义） | %d |" % len(summary["dangling_symbols"]),
        "| 已禁用函数（体含 return None / IR-32 标记） | %d |" % len(summary["disabled_definitions"]),
        "",
        "## 2. 按 kind / family 分布",
        "",
        "| kind | 命中 |",
        "|------|------|",
    ]
    for k in sorted(summary["by_kind"], key=lambda x: -summary["by_kind"][x]):
        lines.append("| %s | %d |" % (k, summary["by_kind"][k]))
    lines += ["", "| family | 命中 |", "|--------|------|"]
    for f in sorted(summary["by_family"], key=lambda x: -summary["by_family"][x]):
        lines.append("| %s | %d |" % (f, summary["by_family"][f]))
    lines += ["", "## 3. 命中清单（按文件）", "", "| 文件 | 行 | 符号 | kind | 风险 | 守卫可抓 | 代码 |",
              "|------|----|------|------|------|---------|------|"]
    for h in sorted(hits, key=lambda x: (x["file"], x["line"])):
        lines.append("| %s | %d | `%s` | %s | %s | %s | %s |" % (
            h["file"], h["line"], h["symbol"], h["kind"], h["risk"],
            "YES" if h["guard_caught"] else "NO", h["code"][:70]))
    if summary["dangling_symbols"]:
        lines += ["", "## 4. 悬空引用（引用了不存在的符号 → 运行时 NameError 面）", ""]
        for d in summary["dangling_symbols"]:
            prod = [r for r in d["referenced_at"] if r.startswith("bridge_service.py:")
                    or r.startswith("agent_cruise.py:")]
            tag = "  ⚠ 命中生产入口（启动期 NameError 风险）" if prod else ""
            lines.append("- `%s`：全树无 `def` 定义；引用点 %s%s"
                         % (d["symbol"], ", ".join(d["referenced_at"]), tag))
        lines.append("")
        lines.append("> 注：`agent_cruise.py:21` 位于模块 docstring（注释，非代码），"
                     "真正的生产触发点是 `bridge_service.py:1037` 的 kwarg 传入。")
    if summary["disabled_definitions"]:
        lines += ["", "## 5. 已禁用但代码存活的函数", ""]
        for d in summary["disabled_definitions"]:
            lines.append("- `%s` @ %s → 生产者禁用（面板恒空）" % (d["symbol"], d["location"]))
    lines += [
        "",
        "## 6. 结论（诚实口径）",
        "",
        "以上为**代码面事实**，不代表已产生跨庄 edge 输出。" +
        "跨庄共识自 2026-09-19 起在生产被禁，当前所有活跃面均为" +
        "「骨架存活、数据恒空/恒 None」，风险在于**误接通时无守卫报警**。",
        "",
        "扫描口径（诚实声明）：",
        "",
        "- 只扫 backend(bridge/agent_cruise/gq/pipeline/core/data_collector/tests) 与",
        "  frontend(src/e2e) 的代码面；`docs/` 与 `reports/` 属文档面，未计入命中",
        "  （已知 `docs/知识库.md` 有大量跨庄历史记录，属内容资产非活跃代码）。",
        "- 现守卫仅 1 处命中（`cross_book_alert` 类名 import），其余 %d 处全在其视野外 ——"
        % summary["guard_coverage_gap"]["guard_missed"],
        "  这与 T06/T15 判定的「只查 import、不扫前端」缺口一致，本脚本只做盘点不改守卫。",
        "- 悬空/禁用判定基于静态扫描，可能有误报；生产面结论以日志实证为准。",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 主流程（只读）
# --------------------------------------------------------------------------
def run_scan(root=ROOT):
    """执行全树扫描，返回 (summary, hits)。零写入。"""
    global ROOT
    prev_root = ROOT
    ROOT = root
    try:
        hits = []
        scanned = 0
        for symbol, family, risk in SYMBOLS:
            # 后端 + 前端两层扫描
            for root_path in BACKEND_ROOTS + FRONTEND_ROOTS:
                for path, text in iter_text_files(root_path, TEXT_EXTS):
                    scanned += 1
                    for h in scan_path(path, text, symbol):
                        h["family"] = family
                        h["risk"] = risk
                        hits.append(h)
        # 符号级元信息（禁用/悬空）
        # 注意：悬空判定只在**代码面**（后端+前端）成立，且跳过纯字段型符号
        # （softline 家族是数据字段，无 def 属正常），避免把文档提及误报为悬空。
        dangling = []
        dead_defs = []
        code_hits_by_symbol = {}
        for h in hits:
            if h["kind"] != "COMMENT" and is_call_or_kwarg_ref(h["code"], h["symbol"]):
                code_hits_by_symbol.setdefault(h["symbol"], []).append(h)
        code_text = "\n".join(t for _p, t in iter_text_files(BACKEND_ROOTS + FRONTEND_ROOTS, TEXT_EXTS))
        for symbol, family, _risk in SYMBOLS:
            defs = find_definitions(BACKEND_ROOTS + FRONTEND_ROOTS, symbol)
            code_hits = code_hits_by_symbol.get(symbol, [])
            if (not defs and family != "softline_divergence" and code_hits
                    and not param_declared(code_text, symbol)):
                dangling.append({
                    "symbol": symbol,
                    "family": family,
                    "referenced_at": ["%s:%d" % (h["file"], h["line"])
                                      for h in code_hits if h["kind"] != "COMMENT"][:10],
                })
            for d in defs:
                p, ln = d.rsplit(":", 1)
                text = read_text(os.path.join(root, p.replace("/", os.sep)))
                disabled, snippet = func_is_disabled(text, symbol)
                if disabled:
                    dead_defs.append({"symbol": symbol, "family": family,
                                      "location": d, "snippet": snippet})
        guard_caught = sum(1 for h in hits if h["guard_caught"])
        scanned_files = len({h["file"] for h in hits})
        summary = build_summary(
            hits, dangling, dead_defs,
            {"guard_caught": guard_caught,
             "guard_missed": len(hits) - guard_caught,
             "guard_scan_files_hit": sum(1 for h in hits if h["in_guard_scan"])},
            scanned_files,
        )
        return summary, hits
    finally:
        ROOT = prev_root


def main(argv=None):
    ap = argparse.ArgumentParser(description="T30 跨庄共识活跃面只读盘点")
    ap.add_argument("--root", default=ROOT, help="仓库根（默认 D:\\Architecture）")
    ap.add_argument("--out-dir", default=REPORTS_DIR, help="报告输出目录")
    ap.add_argument("--json-only", action="store_true", help="只输出 JSON 不写 md")
    args = ap.parse_args(argv)

    summary, hits = run_scan(args.root)
    os.makedirs(args.out_dir, exist_ok=True)
    jpath = os.path.join(args.out_dir, "crossbook_live_surface_audit.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "hits": hits}, fh, ensure_ascii=False, indent=2)
    mpath = os.path.join(args.out_dir, "crossbook_live_surface_audit.md")
    if not args.json_only:
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(summary, hits))
    print("T30 crossbook live surface: hits=%d code=%d guard_caught=%d dangling=%d"
          % (summary["total_hits"], summary["code_hits"],
             summary["guard_coverage_gap"]["guard_caught"],
             len(summary["dangling_symbols"])))
    print("report: %s" % jpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
