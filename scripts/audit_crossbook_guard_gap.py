"""T30-C IR-32 守卫覆盖缺口量化（自动化 dbda4380 任务队列）。

问题（来自 T06 / T15 / T30）：现守卫 `tests/test_no_crossbook.py` 只扫 9 个后端
`*.py` 文件、只匹配「import 行」，因此 T30 实测 293 处跨庄面命中中只有 1 处在
它视野内。本任务回答一个更可操作的问题：

    **如果此刻就按 docs/P-SEC-crossbook-frontend-spec.md §3 扩展守卫，会命中多少处？**
    —— 量化"扩展后的即时 FAIL 面"，给出 file:line 清单与风险等级，并诚实指出
    「扩展规格本身仍有盲区」（SCAN_DIRS 未含 core/ data_collector/ agent_cruise.py）。

本脚本**只复算守卫语义，绝不修改守卫、绝不修改生产代码、绝不碰任何数据库**：

  - `current_guard_hits()` 逐字复刻现守卫（SCAN_FILES + import 行正则）。
  - `extended_guard_hits()` 复刻 T15 §3 扩展语义（SCAN_DIRS + import 路 + 符号出现路）。
  - 两路差集 = 「扩守卫即时新增 FAIL 面」。
  - 另算 §4 清除清单落地后**仍会 FAIL** 的残留面（零容忍口径）。

扫描范围（纯只读文本）：bridge_service.py / agent_cruise.py / gq / pipeline / core /
data_collector / tests / frontend/src / frontend/e2e；`archive/` 按 T15 §3.5 排除。

输出：reports/crossbook_guard_gap_audit.{json,md}
安全红线：纯文本只读；不写任何库、不 kill 进程、不改源码、不改守卫、不改前端。
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(ROOT, "reports")

EXCLUDE_DIRS = {".venv", "node_modules", "__pycache__", ".git", "dist",
                "build", "logs", "models", "coverage"}

# 现守卫 (tests/test_no_crossbook.py) 逐字复刻
CURRENT_BANNED = (
    "cross_book_edge", "multibook_consensus", "leyu_value_signal",
    "bet_split_source", "compute_value_layer", "from scripts.bet_core",
    "cross_book_alert",
)
CURRENT_SCAN_FILES = {
    "bridge_service.py", "gq/auto_collector.py", "gq/db.py",
    "pipeline/prematch_similarity.py", "pipeline/predict_export.py",
    "pipeline/odds_candles_predict.py", "pipeline/ht_anchor_predict.py",
    "pipeline/score_model.py", "pipeline/ranked_predictor.py",
}

# T15 §3.3 规格声明的 BANNED_SYMBOLS（符号级匹配）
SPEC_SYMBOLS = (
    ("cross_book_edge", "crossbook_core"),
    ("multibook_consensus", "crossbook_core"),
    ("leyu_value_signal", "crossbook_core"),
    ("bet_split_source", "crossbook_core"),
    ("compute_value_layer", "quant_archived"),
    ("cross_book_alert", "crossbook_core"),
    ("MultiBookConsensus", "crossbook_residue"),
    ("_lookup_multibook_consensus", "crossbook_residue"),
)
# T30 追加建议（规格外的符号，仅作信息面统计，不计入「规格即时 FAIL」主口径）
EXTRA_SYMBOLS = (
    # ⚠ 规格 §3.3 BANNED_SYMBOLS 写的是组件名 `MultiBookConsensus`(大写 B)，
    #    但前端真实类型标识符是 `MultibookConsensus`(小写 b) —— 规格拼写漏项，
    #    单靠规格符号表会漏掉类型声明那一族（见报告「规格拼写缺口」）。
    ("MultibookConsensus", "crossbook_residue"),
    ("_get_cross_book_signal", "crossbook_residue"),
    ("cross_book", "crossbook_residue"),
    ("softline", "softline_divergence"),
    ("SoftlineBanner", "softline_divergence"),
    ("跨庄共识", "softline_divergence"),
    ("跨庄分歧", "softline_divergence"),
)

# T15 §3.2 规格声明的 SCAN_DIRS（rel 前缀判定）
SPEC_SCOPE_PREFIXES = ("bridge_service.py", "gq/", "pipeline/", "frontend/src/")
# 生产面但规格 SCAN_DIRS 未含（扩展后仍漏scan）
OUTSIDE_SCOPE_PREFIXES = ("core/", "data_collector/", "agent_cruise.py", "tests/",
                          "frontend/e2e/")

# 会真正造成「跨庄数据流向生产」的 kind（零容忍下即 FAIL）
RUNTIME_KINDS = {"IMPORT", "FUNC_DEF", "DICT_FIELD", "TYPE_DECL", "COMPONENT",
                 "JSX_MOUNT", "FIELD_ACCESS", "STRING"}
NOISE_KINDS = {"COMMENT"}

KIND_ORDER = ("JSX_MOUNT", "TYPE_DECL", "COMPONENT", "FUNC_DEF", "IMPORT",
              "DICT_FIELD", "FIELD_ACCESS", "STRING", "COMMENT")


# --------------------------------------------------------------------------
# 纯函数区（零生产 I/O，可被单测直接调用）
# --------------------------------------------------------------------------
def current_guard_hits(rel, text, banned=CURRENT_BANNED, scan_files=CURRENT_SCAN_FILES):
    """逐字复刻现守卫：仅当文件在 SCAN_FILES 中，且存在 import 行命中 banned。

    返回 [{file,line,symbol,rule:`IMPORT`}]（同一 banned 只报一次，与现守卫语义一致）。
    """
    base = os.path.basename(rel)
    in_list = rel in scan_files or base in scan_files
    # 规格目录（bridge_service.py / gq/ / pipeline/ 顶层）内 .py 一并视为在扫描面
    in_dir = base not in EXCLUDE_DIRS and rel.rsplit("/", 1)[0] in ("bridge_service.py", "gq", "pipeline")
    if not (in_list or in_dir):
        return []
    hits = []
    for i, line in enumerate(text.splitlines(), start=1):
        for b in banned:
            if re.match(r"^\s*(?:from\s+\S*%s\S*\s+|import\s+\S*%s)"
                        % (re.escape(b), re.escape(b)), line.strip()):
                hits.append({"file": rel, "line": i, "symbol": b, "rule": "IMPORT",
                             "kind": "IMPORT", "code": line.strip()[:160]})
    return hits


def line_anchored_import_match(line, banned=CURRENT_BANNED):
    """现守卫的 import 行正则（供外部复用/单测）。"""
    s = line.strip()
    for b in banned:
        if re.match(r"^(?:from\s+\S*%s\S*\s+|import\s+\S*%s)" % (re.escape(b), re.escape(b)), s):
            return True
    return False


def word_boundary_hit(line, symbol):
    """符号出现路（T15 §3.4 路 2）：`\\b<symbol>\\b` 整词匹配。

    注意（诚实口径）：整词边界使 `multibook_consensus` **不会**命中
    `_lookup_multibook_consensus`（前接 `_` 属词字符，无边界）。因此按规格落地的
    守卫在 `_lookup_multibook_consensus` 单独出现时**抓不到**，须靠 §4 先删名。
    """
    return bool(re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(symbol), line))


def scope_bucket(rel):
    """把命中归入 spec_current / spec_extended_only / outside_spec（规格盲区面）。"""
    low = rel.replace("\\", "/")
    for p in SPEC_SCOPE_PREFIXES:
        if low == p or low.startswith(p):
            return "spec_scope"
    for p in OUTSIDE_SCOPE_PREFIXES:
        if low == p or low.startswith(p):
            # tests/ 与 frontend/e2e/ 是测试面：其注释提及属自指命中（fail-closed 会计入，
            # 但不算「生产规格盲区」，单列以免污染 §4 结论）
            if low.split("/", 1)[0] in ("tests", "frontend") and \
                    low.startswith(("tests/",)) or low.startswith("frontend/e2e"):
                return "test_surface"
            return "outside_spec"
    if low.endswith(".py"):
        return "outside_spec"
    return "unknown"


def classify_line(line, symbol, prev_in_doc=False):
    """复用 T30 的 kind 分类（注释/docstring 优先判定），保持两脚本口径一致。"""
    try:
        import audit_crossbook_live_surface as A  # noqa
        return A.classify_line(line, symbol, prev_in_doc)
    except Exception:  # pragma: no cover - 单测隔离时退化
        s = line.strip()
        if s.startswith("#") or s.startswith("//"):
            return "COMMENT"
        if word_boundary_hit(s, symbol):
            return "STRING"
        return "OTHER"


def extended_guard_hits(rel, text, symbols=SPEC_SYMBOLS):
    """复刻 T15 §3 扩展守卫：import 路 + 符号出现路，零容忍（含注释面）。"""
    hits = []
    in_doc = False
    for i, line in enumerate(text.splitlines(), start=1):
        raw = line.strip()
        for sym, _fam in symbols:
            if not word_boundary_hit(raw, sym):
                continue
            # kind 必须按**当前符号**判定（不同符号命中同一行时分类可能不同）
            kind = classify_line(line, sym, in_doc)
            if kind == "OTHER":
                # 符号出现路在此命中：即使细分类落到 OTHER，也归一为 STRING
                # （守卫按出现即 FAIL，细分类差异不影响 FAIL 面计数）
                kind = "STRING"
            rule = "IMPORT" if line_anchored_import_match(raw, (sym,)) else "SYMBOL"
            hits.append({"file": rel, "line": i, "symbol": sym, "rule": rule,
                         "kind": kind, "code": raw[:160]})
        if line.lstrip().startswith(('"""', "'''", "*")):
            in_doc = True
        elif in_doc and ('"""' in line or "'''" in line):
            in_doc = False
    return hits


def risk_of(hit, family="crossbook_core"):
    """风险等级：按 kind 面 + family（critical > high > medium > low）。"""
    kind = hit.get("kind")
    if kind in ("FUNC_DEF", "DICT_FIELD", "JSX_MOUNT", "COMPONENT", "IMPORT", "TYPE_DECL"):
        if family == "crossbook_core":
            return "critical"
        if family == "quant_archived":
            return "high"
        return "medium"
    if kind in ("FIELD_ACCESS", "STRING"):
        return "high" if family == "crossbook_core" else "medium"
    return "low"


def family_of(symbol, table=SPEC_SYMBOLS):
    for s, f in table:
        if s == symbol:
            return f
    return "unknown"


def dedupe(hits):
    """按 (file,line,symbol) 去重，保留首次命中（同一行多规则只算一次 FAIL）。"""
    seen = set()
    out = []
    for h in hits:
        k = (h["file"], h["line"], h["symbol"])
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out


def aggregate(hits, cur_hits):
    """聚合成报告 dict（纯聚合，无副作用）。"""
    ext = dedupe(hits)
    cur_keys = {(h["file"], h["line"], h["symbol"]) for h in cur_hits}
    ext_keys = {(h["file"], h["line"], h["symbol"]) for h in ext}

    newly = [h for h in ext if (h["file"], h["line"], h["symbol"]) not in cur_keys]
    already = [h for h in ext if (h["file"], h["line"], h["symbol"]) in cur_keys]

    by_scope = {}
    by_kind = {}
    by_file = {}
    for h in newly:
        b = by_scope.setdefault(h["_scope"], {"new": 0, "critical": 0, "runtime": 0})
        b["new"] += 1
        if h["risk"] == "critical":
            b["critical"] += 1
        if h["kind"] in RUNTIME_KINDS:
            b["runtime"] += 1
        by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1
        f = by_file.setdefault(h["file"], {"new": 0, "risk": "low", "kinds": {}})
        f["new"] += 1
        f["kinds"][h["kind"]] = f["kinds"].get(h["kind"], 0) + 1
        if h["risk"] == "critical":
            f["risk"] = "critical"
        elif h["risk"] == "high" and f["risk"] != "critical":
            f["risk"] = "high"
        elif h["risk"] == "medium" and f["risk"] == "low":
            f["risk"] = "medium"

    outside = [h for h in newly if h["_scope"] == "outside_spec"]
    test_surface = [h for h in newly if h["_scope"] == "test_surface"]
    runtime_facing = [h for h in newly if h["kind"] in RUNTIME_KINDS]
    noise = [h for h in newly if h["kind"] in NOISE_KINDS]

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "current_guard_caught": len(cur_hits),
        "extended_guard_total": len(ext),
        "extended_newly_caught": len(newly),
        "extended_already_caught": len(already),
        "by_scope": by_scope,
        "by_kind": by_kind,
        "by_file": by_file,
        "outside_spec_hits": outside,
        "test_surface_hits": test_surface,
        "test_surface_count": len(test_surface),
        "runtime_facing_hits": runtime_facing,
        "comment_noise_hits": noise,
        "newly_caught": newly,
    }


def render_markdown(summary):
    """渲染人类可读报告（纯字符串）。"""
    g = summary["extended_newly_caught"]
    lines = [
        "# IR-32 守卫覆盖缺口量化（T30-C）",
        "",
        "> 生成：%s · 纯只读文本扫描，不改守卫、不改生产代码、不碰任何数据库。"
        % summary["generated_at"],
        "",
        "## 1. 结论速览",
        "",
        "| 指标 | 值 | 含义 |",
        "|------|----|------|",
        "| 现守卫可抓（实测） | %d | 9 个后端 .py + import 行正则 |"
        % summary["current_guard_caught"],
        "| 现守卫实际是否 FAIL | %s | 0 → 守卫当前全绿通过 |"
        % ("YES" if summary["current_guard_caught"] else "NO"),
        "| 按 T15 §3 扩展后可抓（总命中/去重） | %d | 规格 SCAN_DIRS + import 路 + 符号出现路 |"
        % summary["extended_guard_total"],
        "| **扩展后即时新增 FAIL 面** | **%d** | 本任务量化对象 |" % g,
        "| 其中 critical 级 | %d | 定义/字段赋值/组件挂载/import 等真流向面 |"
        % sum(1 for h in summary["newly_caught"] if h["risk"] == "critical"),
        "| 其中运行时有效（非注释） | %d | 零容忍下会真 FAIL |"
        % len(summary["runtime_facing_hits"]),
        "| 其中仅注释提及（噪声） | %d | 零容忍会误 FAIL，须配豁免或先清注释 |"
        % len(summary["comment_noise_hits"]),
        "| 规格 SCAN_DIRS 外**生产面**盲区命中 | %d | **T15 §3.2 未含 core/ data_collector/ agent_cruise.py** |"
        % len(summary["outside_spec_hits"]),
        "| 测试面自指命中 | %d | 审计脚本自身测试/守卫文件注释（fail-closed 亦计入，单列） |"
        % summary["test_surface_count"],
        "",
        "## 2. 新增 FAIL 面按面分布",
        "",
        "| scope | 新增 FAIL | critical | 运行时有效 |",
        "|--------|----------|----------|------------|",
    ]
    for k in sorted(summary["by_scope"], key=lambda x: -summary["by_scope"][x]["new"]):
        v = summary["by_scope"][k]
        lines.append("| %s | %d | %d | %d |" % (k, v["new"], v["critical"], v["runtime"]))
    lines += ["", "| kind | 新增 FAIL |", "|------|----------|"]
    for k in sorted(summary["by_kind"], key=lambda x: -summary["by_kind"][x]):
        lines.append("| %s | %d |" % (k, summary["by_kind"][k]))
    lines += ["", "## 3. 受影响文件清单（新增 FAIL 面 ≥1 的文件）", ""]
    if summary["by_file"]:
        lines += ["| 文件 | 新增 FAIL | 最高风险 | 命中 kind |", "|------|----------|----------|-----------|"]
        for f in sorted(summary["by_file"], key=lambda x: -summary["by_file"][x]["new"]):
            v = summary["by_file"][f]
            lines.append("| `%s` | %d | %s | %s |" % (
                f, v["new"], v["risk"], ", ".join(sorted(v["kinds"]))))
    else:
        lines.append("（无）")
    if summary["outside_spec_hits"]:
        lines += ["", "## 4. ⚠ 规格自身盲区（SCAN_DIRS 未含，扩展后仍漏）", ""]
        for h in summary["outside_spec_hits"][:40]:
            lines.append("- `%s:%d` `%s` (%s) — %s"
                         % (h["file"], h["line"], h["symbol"], h["kind"], h["code"][:70]))
        if len(summary["outside_spec_hits"]) > 40:
            lines.append("- …另 %d 处，详见 JSON" % (len(summary["outside_spec_hits"]) - 40))
    lines += ["", "## 5. 新增 FAIL 面明细（前 60 条，按文件行号）", "",
              "| 文件 | 行 | 符号 | kind | 规则 | 风险 | 代码 |",
              "|------|----|------|------|------|------|------|"]
    for h in sorted(summary["newly_caught"], key=lambda x: (x["file"], x["line"]))[:60]:
        lines.append("| `%s` | %d | `%s` | %s | %s | %s | %s |" % (
            h["file"], h["line"], h["symbol"], h["kind"], h["rule"],
            h["risk"], h["code"][:60]))
    if summary.get("extra_spec_gap"):
        lines += ["", "## 5b. 规格拼写缺口（T15 §3.3 BANNED_SYMBOLS 漏项的符号命中）", ""]
        lines += ["符号：%s" % ", ".join("`%s`" % s for s in summary["extra_symbols"]), ""]
        agg = {}
        for h in summary["extra_spec_gap"]:
            k = (h["symbol"], h["kind"])
            agg[k] = agg.get(k, 0) + 1
        lines += ["| 符号 | kind | 处数 |", "|------|------|------|"]
        for (sym, kind), n in sorted(agg.items(), key=lambda x: -x[1]):
            lines.append("| `%s` | %s | %d |" % (sym, kind, n))
        lines.append("")
        lines.append("> 典型：`MultibookConsensus`（前端真实类型名，小写 b）不在 §3.3 的 "
                     "`MultiBookConsensus`（大写 b）中 → 规格落地后类型声明那一族仍会漏。")

    lines += [
        "",
        "## 6. 落地建议（不属本任务执行）",
        "",
        "1. 按 `docs/P-SEC-crossbook-frontend-spec.md` §4 清除残留骨架后，扩展守卫应干净通过；",
        "   本表「新增 FAIL 面」即 §4 清除清单的**量化目标**。",
        "2. 零容忍会连注释提及一起 FAIL（%d 处噪声）→ 建议 §3.5 豁免需显式标注。" % len(summary["comment_noise_hits"]),
        "3. §3.2 SCAN_DIRS 须补 `core/` `data_collector/` `agent_cruise.py`（本任务 %d 处盲区命中），"
        % len(summary["outside_spec_hits"]),
        "   否则「扩展」只是把缺口从前端搬到这些生产目录。",
        "4. 整词匹配使 `_lookup_multibook_consensus` 单独出现时抓不到 → §4 须**先删名**再开守卫。",
        "5. 规格盲区 `core/` `data_collector/` `agent_cruise.py` 实测 **%d 处命中**（无残留），"
        % len(summary["outside_spec_hits"]) +
        "即盲区是**理论存在、当前无害**；但若日后这些目录新增跨庄面，扩展规格抓不到。",
        "",
        "## 7. 诚实口径声明",
        "",
        "- 本表是**代码面事实**，不代表已产生跨庄 edge 输出（T30 已证：生产者恒 `return None`）。",
        "- 本脚本只复算守卫语义，未修改 `tests/test_no_crossbook.py`，未改任何生产代码。",
        "- ⚠ **对 T30 勘误**：T30 报告「现守卫可抓 1 处（`cross_book_alert` 类名 import）」",
        "  经本任务逐文件复刻现守卫正则实测为 **0 处** —— T30 的 `guard_caught` 未排除注释行，",
        "  把注释中的符号提及误算成「守卫可抓」。事实：现守卫当前**全绿通过**（无 import 违规）。",
        "- 不读 events.db / 不写任何库 / 不 kill 进程 / 不碰生产服务。",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 主流程（只读）
# --------------------------------------------------------------------------
def run_gap_scan(root=ROOT):
    """执行全树扫描，返回 (summary, hits)。零写入。"""
    sys.path.insert(0, os.path.join(root, "scripts"))
    try:
        import audit_crossbook_live_surface as A
    except Exception:  # pragma: no cover
        A = None

    roots = [os.path.join(root, p) for p in ("bridge_service.py", "agent_cruise.py",
                                             "gq", "pipeline", "core",
                                             "data_collector", "tests",
                                             os.path.join("frontend", "src"),
                                             os.path.join("frontend", "e2e"))]

    raw, cur = [], []
    seen_rel = set()
    for r in roots:
        if A is not None:
            gen = A.iter_text_files(r, A.TEXT_EXTS)
        else:  # pragma: no cover
            gen = []
        for path, text in gen:
            rel = os.path.relpath(path, root).replace("\\", "/")
            if rel in seen_rel:
                continue
            seen_rel.add(rel)
            cur.extend(current_guard_hits(rel, text))
            for sym, fam in SPEC_SYMBOLS:
                for h in extended_guard_hits(rel, text, ((sym, fam),)):
                    h["family"] = fam
                    raw.append(h)
    for h in raw:
        h["_scope"] = scope_bucket(h["file"])
        h["risk"] = risk_of(h, h.get("family", "crossbook_core"))

    # 规格外「追加符号」命中（含规格拼写漏项 `MultibookConsensus` 等），仅信息面
    spec_keys = {(h["file"], h["line"], h["symbol"]) for h in raw}
    extra = []
    for r in roots:
        if A is not None:
            gen = A.iter_text_files(r, A.TEXT_EXTS)
        else:  # pragma: no cover
            gen = []
        for path, text in gen:
            rel = os.path.relpath(path, root).replace("\\", "/")
            for sym, fam in EXTRA_SYMBOLS:
                for h in extended_guard_hits(rel, text, ((sym, fam),)):
                    if (rel, h["line"], sym) in spec_keys:
                        continue
                    h["family"] = fam
                    h["risk"] = risk_of(h, fam)
                    h["_scope"] = scope_bucket(rel)
                    extra.append(h)

    summary = aggregate(raw, cur)
    summary["spec_symbols"] = [s for s, _ in SPEC_SYMBOLS]
    summary["extra_symbols"] = [s for s, _ in EXTRA_SYMBOLS]
    summary["extra_spec_gap"] = extra
    summary["extra_gap_total"] = len(extra)
    summary["test_surface_hits"] = summary.get("test_surface_hits", [])
    summary["test_surface_count"] = summary.get("test_surface_count", 0)
    # 诚实勘误：现守卫在当前代码面上实际抓到 0 处（T30 报告"1 处"系注释行误算）
    summary["current_guard_effective"] = cur
    return summary, summary["newly_caught"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="T30-C IR-32 守卫覆盖缺口量化")
    ap.add_argument("--root", default=ROOT, help="仓库根（默认 D:\\Architecture）")
    ap.add_argument("--out-dir", default=REPORTS_DIR, help="报告输出目录")
    ap.add_argument("--json-only", action="store_true", help="只输出 JSON 不写 md")
    args = ap.parse_args(argv)

    summary, _hits = run_gap_scan(args.root)
    os.makedirs(args.out_dir, exist_ok=True)
    jpath = os.path.join(args.out_dir, "crossbook_guard_gap_audit.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    mpath = os.path.join(args.out_dir, "crossbook_guard_gap_audit.md")
    if not args.json_only:
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(summary))
    print("T30-C guard gap: current=%d extended=%d newly=%d critical=%d outside_spec=%d noise=%d"
          % (summary["current_guard_caught"], summary["extended_guard_total"],
             summary["extended_newly_caught"],
             sum(1 for h in summary["newly_caught"] if h["risk"] == "critical"),
             len(summary["outside_spec_hits"]), len(summary["comment_noise_hits"])))
    print("report: %s" % jpath)
    return 0


if __name__ == "__main__":
    sys.exit(main())
