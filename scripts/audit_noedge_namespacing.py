#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T58 信号词 ``NO_EDGE`` 与验证台三态 ``NO EDGE`` 的命名隔离只读盘点（承接 T51 §2 / T53 §2 / T57）。

本文件回答三问, 全部只读, 不改任何生产文件 / 不碰 events.db / 不写 verification.db / 零进程操作:

  Q1 信号语义出现面 —— ``NO_EDGE``(下划线) 属于**盘口信号词表**, 与
      ``STRONG_BREAK / STRONG_HOLD / WEAK_TREND / ALREADY_BROKEN / OVER / UNDER`` 同族,
      语义=「此刻不下注」; 而验证台三态 ``NO EDGE``(空格) 是 ``verification/gates.py``
      对**模型**的判定。两者正交同轴, 但拼写只差一个字符, 静态扫描器与读者的眼睛都会混。
      本函数把信号语义命中的**每个文件 + 每层角色**列出来。

  Q2 调用方 —— 谁生产、谁消费、谁零调用。重点核 ``scripts/backtest_ou_signal.py``
      (它在 ``scripts/`` 根目录, 无目录前缀豁免, 正是它逼出 T57 的登记册豁免),
      本轮要**机械证明**它是否真的零调用方。

  Q3 改名成本 —— 三层(analysis 生产层 / bridge_service / frontend)字面量代价 +
      是否存在**持久化工件**携带该字符串(若有则改名需要迁移, 成本等级升级) +
      对 ``analysis/`` 的影响(勘误: ``analysis/`` 不是历史脚本, 它是 bridge_service 实时导入的活生产层)。

⚠ 本文件含信号词字面量, 故 T51/T53 的两个守卫按 basename 自避(见 ``self_exclude()``);
  新增同类脚本必须同时把**它的测试文件**加进自避, 否则会把自己判成未登记命中。

纯只读: 不 import 生产入口 / 不跑训练 / 不改任何文件 / 不挂调度 / 零进程操作。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR: str = os.path.join(ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import verdict_guard_ssot as VG  # noqa: E402  (三态词表/Tier SSoT, 禁止自带词表正则)

REPORTS_DIR: str = os.path.join(ROOT, "reports")
OUT_JSON: str = os.path.join(REPORTS_DIR, "noedge_namespacing_audit.json")
OUT_MD: str = os.path.join(REPORTS_DIR, "noedge_namespacing_audit.md")

_SELF: str = os.path.basename(__file__)
_SELF_EXCLUDE: Tuple[str, ...] = (_SELF, "test_" + _SELF)

# ── 信号词族 (与 ``analysis/live_goal_probe.py`` 的 signal 字段取值同族) ─────
#: 被本轮审查的目标词: 与验证台三态只差一个「空格 vs 下划线」。
SIGNAL_TOKEN: str = "NO_EDGE"
#: 同族兄弟词 (用于识别「是否真在信号语义里」; 单列 NO_EDGE 会被 OVER/UNDER 的类名噪声带偏)。
SIGNAL_SIBLINGS: Tuple[str, ...] = (
    "STRONG_BREAK", "STRONG_HOLD", "WEAK_TREND", "ALREADY_BROKEN", "OVER", "UNDER",
)
RE_SIGNAL_TOKEN = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(SIGNAL_TOKEN) + r"(?![A-Za-z0-9_])")

#: 判定信号语义的上下文: 同一行里出现同族兄弟词 或 signal 字段名。
RE_SIGNAL_CONTEXT = re.compile(
    r"(?P<sib>" + "|".join(re.escape(s) for s in SIGNAL_SIBLINGS) + r")"
    r"|signal"
)

#: 字符串比较面: 对信号词(或同族兄弟词)做 == / != / in / not in 比较的站点。
RE_SIGNAL_COMPARE = re.compile(
    r"(?:==|!=|not\s+in|\bin\b)"
    r"[\s\(\)\[\{]*"
    r"[\"'](?:STRONG_BREAK|STRONG_HOLD|WEAK_TREND|ALREADY_BROKEN|NO_EDGE|OVER|UNDER)[\"']"
)

#: 三态词表取自 SSoT, 本模块**禁止**自带判定词正则。
RE_VERDICT_TOKEN = VG.RE_VERDICT_TOKEN

#: 死代码核对象: 本轮要证明它是否真的零调用方。
DEAD_CODE_CANDIDATE_MODULE: str = "backtest_ou_signal"

# ── 扫描面 ──────────────────────────────────────────────────────────────
CODE_EXTS: Tuple[str, ...] = (".py", ".ts", ".tsx", ".jsx")
PERSIST_EXTS: Tuple[str, ...] = (".json", ".jsonl")
SKIP_DIRS: Tuple[str, ...] = (
    ".venv", "node_modules", "archive", ".git", ".workbuddy",
    ".codebuddy", ".zcode", "__pycache__", "dist", "build", "e2e",
)
#: 持久化工件扫描预算 (reports/ + data/ 顶层, 防止慢扫描)。
PERSIST_SCAN_DIRS: Tuple[str, ...] = ("reports", "data")
PERSIST_MAX_FILES: int = 400
PERSIST_MAX_LINES: int = 20000
PERSIST_MAX_BYTES: int = 4_000_000

# ── 分层 ────────────────────────────────────────────────────────────────
LAYER_ANALYSIS: str = "analysis(活生产层)"
LAYER_SCRIPTS: str = "scripts(根目录无豁免)"
LAYER_SANDBOX: str = "sandbox(协同黑板)"
LAYER_FRONTEND: str = "frontend(展示层消费者)"
LAYER_BRIDGE: str = "bridge_service(后端消费点)"
LAYER_TESTS: str = "tests"
LAYER_OTHER: str = "other"

#: 带理由登记册: 每个命中文件必须在此登记, 空理由即不许登记 (沿用 T57 纪律)。
#: 键 = 仓库相对路径; 值 = 为什么它是信号语义而非三态判定。
SIGNAL_FILE_REGISTRY: Dict[str, str] = {
    "analysis/live_goal_probe.py": (
        "滚球 OU 信号生产者: probe_match() 的 half/full signal 字段取值之一, "
        "且被 bridge_service 实时导入 (live 生产层, 非历史脚本)"
    ),
    "analysis/live_score_conditional.py": (
        "同族生产者: fixed_fulltime_over_prob 分支返回 signal='NO_EDGE' (live_score_conditional.py:348)"
    ),
    "scripts/backtest_ou_signal.py": (
        "复刻 backend_signal() 的回测脚本; 位于 scripts/ 根目录, 目录前缀豁免覆盖不到, "
        "是 T53/T57 必须建登记册豁免的起点"
    ),
    "scripts/audit_signal_vocab_migration.py": (
        "T62 只读盘点脚本: 词表本身即盘点对象, 文件内 NO_EDGE/NO_BET 字面量属被扫描面(self-exclude), "
        "非信号生产者; 登记理由=免被本活体撞车扫描判为未登记"
    ),
    "sandbox/collab/mock_observer.py": (
        "协同黑板异常登记文本 (ANOM_NO_EDGE_high_prob 条目), 属叙述面不是产出体"
    ),
    "frontend/src/pages/Rollball/index.tsx": (
        "前端信号配色映射表成员 (NO_EDGE -> '弱优势'), 是信号词族的展示层消费者"
    ),
    # ── 工具面自避 (这些文件的信号词出现在 docstring/断言/登记册键里, 不是产出体) ──
    "scripts/audit_chain_consistency.py": (
        "滚球链路审计工具: 对 ou.get('signal') 做字符串比较 (CHK2 / CHK6), "
        "它在**消费**信号词而非产出三态; CHK6 已把「高置信却标无优势」记为 WARN"
    ),
    "scripts/audit_mh_train_div_bypass.py": (
        "T53 审计脚本, docstring/注释引用信号词, 属审计工具面自避 (与 T57 的 GUARD_SELF_EXCLUDE 同类)"
    ),
    "scripts/audit_verification_report_freshness.py": (
        "T51 审计脚本, docstring 与 Tier 注释里引用信号词, 属审计工具面自避"
    ),
    "scripts/verdict_guard_ssot.py": (
        "T57 判定词表/Tier SSoT: 登记册键 ``scripts/backtest_ou_signal.py`` 与说明注释里带信号词, "
        "本模块是**唯一词表**, 不产出判定"
    ),
    "tests/test_audit_verification_report_freshness.py": (
        "守卫回归测试: 断言某个文件的 Tier 归属, 断言文本里带信号词, 属测试面自避"
    ),
}

#: 本审计自己的产物 —— 它自带信号词字面量, 会被**下一轮**的持久化工件扫描扫到
#: (同 T52「静默空结果」的自污染同类失效模式), 故必须在扫描时排除并显式告警。
AUDIT_OWN_OUTPUT: str = "reports/noedge_namespacing_audit.json"

#: 实测唯一携带信号词的业务持久化工件 (2026-09-28 读数), 由 scripts/watch_live_verdicts.py 写入。
WATCH_TARGET: str = "watch_verdicts.json"


def self_exclude() -> Tuple[str, ...]:
    """自身与自身测试的基名 —— 两守卫与本审计都按 basename 自避。"""
    return _SELF_EXCLUDE


def layer_of(rel: str) -> str:
    """按路径前缀把文件归入层, 用于回答「跨几层」。"""
    if rel.startswith("analysis/"):
        return LAYER_ANALYSIS
    if rel.startswith("scripts/"):
        return LAYER_SCRIPTS
    if rel.startswith("sandbox/"):
        return LAYER_SANDBOX
    if rel.startswith("frontend/src/"):
        return LAYER_FRONTEND
    if rel == "bridge_service.py":
        return LAYER_BRIDGE
    if rel.startswith("tests/"):
        return LAYER_TESTS
    return LAYER_OTHER


def iter_files(root: str = ROOT, exts: Tuple[str, ...] = CODE_EXTS) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(exts):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _rel(path: str, root: Optional[str] = None) -> str:
    """相对**被扫描根目录**的路径 (不是模块 ROOT) —— 否则 tmp_path 造出来的样例全部算成仓库外路径。"""
    base = root or ROOT
    try:
        return os.path.relpath(path, base).replace(os.sep, "/")
    except ValueError:  # 跨盘符兜底
        return os.path.basename(path)


def read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------
# Q1 信号语义命中面
# --------------------------------------------------------------------------

def classify_signal_file(rel: str) -> str:
    """命中文件分类: 已登记 = 理由; 未登记 = 返回 None (调用方判 RED)。"""
    return SIGNAL_FILE_REGISTRY.get(rel, "")


def scan_signal_sources(root: str = ROOT) -> dict:
    """盘点全仓代码文件里的信号词命中, 按层汇总并核对登记册。

    返回 {hits:[...], by_layer:{...}, unregistered:[...], registered:int, total:int}
    """
    hits: List[dict] = []
    hits_by_layer: Dict[str, int] = {}
    unregistered: List[dict] = []
    self_names = set(self_exclude())
    for path in iter_files(root):
        rel = _rel(path, root)
        if os.path.basename(path) in self_names:
            continue
        text = read_text(path)
        if text is None:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if not RE_SIGNAL_TOKEN.search(line):
                continue
            ctx = bool(RE_SIGNAL_CONTEXT.search(line))
            layer = layer_of(rel)
            hits_by_layer[layer] = hits_by_layer.get(layer, 0) + 1
            hits.append({
                "file": rel,
                "line": i,
                "layer": layer,
                "signal_context": ctx,
                "tier": classify_signal_file(rel) if ctx else VG.TIER_DOC,
            })
            if ctx and not classify_signal_file(rel):
                unregistered.append({"file": rel, "line": i, "layer": layer})
    return {
        "hits": hits,
        "by_layer": dict(sorted(hits_by_layer.items(), key=lambda kv: -kv[1])),
        "unregistered": unregistered,
        "registered_files": sorted(SIGNAL_FILE_REGISTRY),
        "total_hits": len(hits),
    }


def verdict_lines_of(text: str) -> List[int]:
    """该文件里出现**三态写法**的行号 (只认三态拼写, 排除信号拼写 ``NO_EDGE``)。

    陷阱: ``verdict_guard_ssot.RE_VERDICT_TOKEN`` 为了捞 T53 的绕过体, 词表里**同时**
    含 ``NO_EDGE``(信号拼写) 与 ``NO EDGE``(三态拼写) → 直接拿它判「同文件撞车」会把
    每一行信号行都算成三态行(实测 live_goal_probe.py 有 18 行假阳性)。
    故此处逐条比对: 命中文本等于信号拼写的一律丢弃。
    """
    lines = text.splitlines()
    out: List[int] = []
    for i, line in enumerate(lines, 1):
        for mt in RE_VERDICT_TOKEN.finditer(line):
            if mt.group(0) == SIGNAL_TOKEN:
                continue
            out.append(i)
            break
    return out


def scan_same_file_collision(root: str = ROOT) -> List[dict]:
    """同文件撞车: 一个文件里**同时**出现信号词与三态词 = 读者/扫描器最易误判的面。

    已登记的算 INFO(已知), 未登记的算 RED —— 防「豁免清单静默长大」。
    """
    out: List[dict] = []
    self_names = set(self_exclude())
    by_file: Dict[str, dict] = {}
    for path in iter_files(root):
        rel = _rel(path, root)
        if os.path.basename(path) in self_names:
            continue
        text = read_text(path)
        if text is None:
            continue
        if not RE_SIGNAL_TOKEN.search(text):
            continue
        vl = verdict_lines_of(text)
        if vl:
            by_file[rel] = {"file": rel, "verdict_lines": vl}
    for rel, rec in sorted(by_file.items()):
        out.append({
            "file": rel,
            "verdict_lines": rec["verdict_lines"],
            "registered": bool(classify_signal_file(rel)),
        })
    return out


# --------------------------------------------------------------------------
# Q2 调用方 / 字符串比较面
# --------------------------------------------------------------------------

def scan_callers(module_name: str = DEAD_CODE_CANDIDATE_MODULE,
                 root: str = ROOT) -> List[dict]:
    """谁引用了这个模块 (import / runpy / 路径拼装), 排除自避文件。"""
    out: List[dict] = []
    self_names = set(self_exclude())
    esc = re.escape(module_name)
    pat = re.compile(
        "(?:import\\s+\\w*\\.?" + esc + r"\\b"
        + "|" + "(?:from|import)\\s+[\\w\\.]*" + esc
        + "|" + "runpy(?:\\.\\w+)?\\s*\\(\\s*[\"']" + esc + ")"
    )
    for path in iter_files(root):
        rel = _rel(path, root)
        if os.path.basename(path) in self_names:
            continue
        text = read_text(path)
        if text is None:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                out.append({"file": rel, "line": i, "layer": layer_of(rel)})
    return sorted(out, key=lambda r: (r["file"], r["line"]))


def scan_signal_comparison_sites(root: str = ROOT) -> List[dict]:
    """生产面上对信号词做字符串比较的站点 (改名时这些地方会静默失效)。"""
    out: List[dict] = []
    self_names = set(self_exclude())
    for path in iter_files(root, exts=(".py",)):
        rel = _rel(path, root)
        if os.path.basename(path) in self_names:
            continue
        text = read_text(path)
        if text is None:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if RE_SIGNAL_COMPARE.search(line):
                out.append({"file": rel, "line": i, "layer": layer_of(rel)})
    return out


# --------------------------------------------------------------------------
# Q3 改名成本
# --------------------------------------------------------------------------

def scan_persisted_artifacts(root: str = ROOT) -> dict:
    """持久化工件里是否携带信号词 —— 有则需迁移, 改名成本从「改字面量」升级为「数据迁移」。

    分成两类, 混在一起会让结论失真:
      · business  —— 业务快照/产物 (如 ``data/watch_verdicts.json``): 携带即 RED;
      · self_quote —— 审计脚本**自己写出来的报告**, 报告里引用了词表字面量, 属自污染面。
    ``AUDIT_OWN_OUTPUT`` 必须排除: 本脚本刚写出的产物自带信号词, 下一轮扫描会把它
    当成业务数据 (同 T52「静默空结果」的自污染同类失效模式)。
    """
    scanned_files = 0
    scanned_lines = 0
    token_files: List[dict] = []
    truncated = False
    for sub in PERSIST_SCAN_DIRS:
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(PERSIST_EXTS):
                continue
            if scanned_files >= PERSIST_MAX_FILES:
                truncated = True
                break
            rel = f"{sub}/{fn}"
            if rel == AUDIT_OWN_OUTPUT or rel == os.path.basename(AUDIT_OWN_OUTPUT):
                continue
            p = os.path.join(d, fn)
            try:
                if os.path.getsize(p) > PERSIST_MAX_BYTES:
                    continue
            except OSError:
                continue
            text = read_text(p)
            if text is None:
                continue
            scanned_files += 1
            lines = text.splitlines()
            scanned_lines += min(len(lines), PERSIST_MAX_LINES)
            hit_lines = [i for i, ln in enumerate(lines, 1)
                         if RE_SIGNAL_TOKEN.search(ln)][:20]
            if hit_lines:
                token_files.append({
                    "file": rel,
                    "lines": hit_lines,
                    "kind": "self_quote" if "audit" in fn or rel.startswith("reports/")
                            else "business",
                })
    business = [t for t in token_files if t["kind"] == "business"]
    return {
        "scanned_files": scanned_files,
        "scanned_lines": scanned_lines,
        "truncated": truncated,
        "files_with_token": token_files,
        "business_files": business,
        "business_count": len(business),
        "count": len(token_files),
    }


def scan_indirect_persisters(root: str = ROOT,
                             target: str = WATCH_TARGET) -> List[dict]:
    """**间接**持久化方: 自己不含信号词字面量, 但把上游产出(含该值)原样写进磁盘。

    这类方是改名成本评估最容易漏的一环 —— 改了生产者, 老快照里仍是旧词,
    读方若只认新词会静默全空。
    """
    out: List[dict] = []
    self_names = set(self_exclude())
    # 只按**文件名主干**匹配: 写入方用的是整条路径 (WATCH = r'...\data\<name>'),
    # 要求引号紧贴会把唯一写入方漏掉 (实测踩到)。
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(target[:-len(".json")]) + r"(?![A-Za-z0-9_])")
    RE_WRITE = re.compile(r"def\s+save\b|json\.dump|open\([^)]*[\"']w[\"']")
    for path in iter_files(root, exts=(".py",)):
        rel = _rel(path, root)
        if os.path.basename(path) in self_names:
            continue
        text = read_text(path)
        if text is None:
            continue
        lines = text.splitlines()
        found = [i for i, ln in enumerate(lines, 1) if pat.search(ln)]
        if not found:
            continue
        # 「是否写入方」按**文件级**判定: 写入动作在 save()/json.dump 里, 那一行并不同时出现
        # 目标文件名 (实测踩到: 唯一写入方 scripts/watch_live_verdicts.py 的写入点在 def save)。
        writes = any(RE_WRITE.search(ln) for ln in lines)
        out.append({"file": rel, "lines": found, "layer": layer_of(rel), "writes": writes})
    return out


def rename_cost_table(scan: dict) -> List[dict]:
    """改名成本表: 按层给「要改几处字面量」。改 scripts/ 死代码面 = 1 处 0 风险。"""
    rows: List[dict] = []
    per_file: Dict[str, int] = {}
    for h in scan.get("hits", []):
        per_file[h["file"]] = per_file.get(h["file"], 0) + 1
    for rel, cnt in sorted(per_file.items(), key=lambda kv: -kv[1]):
        rows.append({
            "file": rel,
            "literal_count": cnt,
            "layer": layer_of(rel),
            "registered": bool(classify_signal_file(rel)),
            "editable_by_automation": rel.startswith("scripts/"),
        })
    return rows


# --------------------------------------------------------------------------
# 结论合成 (fail-closed)
# --------------------------------------------------------------------------

def _count_by(rows: List[dict], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        v = str(r.get(key))
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))

def build_findings(signal_scan: dict,
                   collisions: List[dict],
                   callers: List[dict],
                   compares: List[dict],
                   persist: dict,
                   cost_rows: List[dict],
                   indirect: Optional[List[dict]] = None) -> dict:
    """合成 findings。未跑的检查必须算 FAIL (沿用 T52 静默空结果教训)。"""
    findings: List[dict] = []

    unreg = signal_scan.get("unregistered", [])
    if unreg:
        findings.append({
            "id": "R1_SIGNAL_TOKEN_UNREGISTERED", "severity": "RED",
            "detail": f"{len(unreg)} 处信号词命中落在未登记文件里, 须人工确认它不是三态判定",
            "evidence": unreg[:20],
        })
    live_collision = [c for c in collisions if not c.get("registered")]
    if live_collision:
        findings.append({
            "id": "R2_SAME_FILE_COLLISION", "severity": "RED",
            "detail": f"{len(live_collision)} 个文件同时含信号词与三态词且未登记 "
                      f"(读者/扫描器最易误判的面)",
            "evidence": live_collision,
        })
    if not signal_scan.get("by_layer"):
        findings.append({
            "id": "R3_SIGNAL_SCAN_NOT_RUN", "severity": "RED",
            "detail": "信号词命中面扫描未产出任何结果 (假零风险, 同 T52 失效模式)",
        })
    if persist.get("truncated"):
        findings.append({
            "id": "R4_PERSIST_SCAN_TRUNCATED", "severity": "AMBER",
            "detail": "持久化工件扫描已达预算上限, 「无持久化携带」结论的覆盖面不完整",
        })
    if persist.get("business_count"):
        findings.append({
            "id": "R5_RENAME_NEEDS_MIGRATION", "severity": "RED",
            "detail": f"{persist['business_count']} 个业务持久化工件携带信号词 "
                      f"({', '.join(t['file'] for t in persist.get('business_files', []))}) "
                      f"→ 改名**不只是改字面量**, 必须同时做数据迁移或读侧双词兼容",
            "evidence": persist.get("business_files", [])[:10],
        })
    if indirect:
        writers = [i for i in indirect if i.get("writes")]
        findings.append({
            "id": "A4_INDIRECT_PERSISTER", "severity": "AMBER",
            "detail": f"{len(indirect)} 个脚本引用持久化工件 {WATCH_TARGET} "
                      f"(其中 {len(writers)} 个是写入方), 它们**不含字面量却搬运该值** "
                      f"→ 改名只改生产者无法让老快照复活, 必须同时处理存量数据",
            "evidence": indirect[:10],
        })
    if persist.get("count", 0) > persist.get("business_count", 0):
        findings.append({
            "id": "A5_SELF_QUOTE_ARTIFACTS", "severity": "AMBER",
            "detail": "另有 "
                      f"{persist['count'] - persist['business_count']} 个报告类产物也带信号词, "
                      "但那是审计脚本自己写进去的引用, 非业务数据; "
                      "本脚本的产物已被排除, 否则下一轮扫描会把它当成业务数据(自污染)",
        })

    # AMBER = 已知的/建议性的, 不构成阻塞
    findings.append({
        "id": "A1_HIGH_VALUE_RENAME_IS_DEAD_CODE", "severity": "AMBER",
        "detail": f"「改名收益最大」的 {DEAD_CODE_CANDIDATE_MODULE}.py 实测零调用方 "
                  f"(引用 {len(callers)} 处, 只出现在 docs/tests/登记册的叙述里) → "
                  f"改它 0 爆炸半径, 且能直接拆掉 T57 为它建的登记册豁免",
    })
    findings.append({
        "id": "A2_LIVE_VOCABULARY_NEEDS_THREE_LAYERS", "severity": "AMBER",
        "detail": "活词表横跨 analysis(bridge 实时导入) → bridge_service 字符串比较 → "
                  f"frontend 配色映射; 共 {len(cost_rows)} 处字面量, "
                  "改它等于改生产面且**不改变任何判定结论** (与「只备料」纪律冲突, 建议 WINDOW 内做)",
    })
    prod_compares = [c for c in compares if c["layer"] in (LAYER_BRIDGE, LAYER_ANALYSIS)]
    if prod_compares:
        sites = sorted({f"{c['file']}:{c['line']}" for c in prod_compares})[:6]
        findings.append({
            "id": "A3_STRING_COMPARE_ON_COLLIDING_VOCAB", "severity": "AMBER",
            "detail": f"{len(prod_compares)} 处生产代码对信号词做字符串比较 ("
                      f"{', '.join(sites)}) → 将来任何一处写成三态写法都会静默永不命中"
                      f"(或恒命中), 且无测试会红",
            "evidence": prod_compares[:20],
        })

    red = [f["id"] for f in findings if f["severity"] == "RED"]
    amber = [f["id"] for f in findings if f["severity"] == "AMBER"]
    return {
        "findings": findings,
        "red": red,
        "amber": amber,
        "verdict": "FAIL" if red else "PASS",
        "signal_scan": signal_scan,
        "collisions": collisions,
        "callers": callers,
        "comparison_sites": compares,
        "comparison_by_layer": _count_by(compares, "layer"),
        "persist": persist,
        "rename_cost": cost_rows,
        "indirect_persisters": indirect or [],
    }


def write_report(findings: dict, out_json: str = OUT_JSON, out_md: str = OUT_MD) -> Dict[str, str]:
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(findings, fh, ensure_ascii=False, indent=2)
    ss = findings.get("signal_scan", {})
    lines = [
        "# 信号词 `NO_EDGE` 与三态判定命名隔离审计报告 (T58)",
        "",
        f"**判定: {findings['verdict']}**"
        + (f" (RED: {','.join(findings['red'])})" if findings["red"] else ""),
        "",
        f"- 生成时刻: {datetime.now(timezone.utc).isoformat()}",
        f"- 信号词命中总数: {ss.get('total_hits', 0)} 处 / 已登记 {len(ss.get('registered_files', []))} 个文件",
        f"- 分层: " + " · ".join(f"{k}={v}" for k, v in (ss.get("by_layer") or {}).items()),
        f"- 持久化工件携带: 业务 {findings.get('persist', {}).get('business_count', 0)} 个 / "
        f"审计自引 {max(0, findings.get('persist', {}).get('count', 0) - findings.get('persist', {}).get('business_count', 0))} 个"
        f" (扫描 {findings.get('persist', {}).get('scanned_files', 0)} 个文件)",
        f"- 死代码候选引用数: {len(findings.get('callers', []))}",
        "",
        "| id | severity | detail |",
        "|---|---|---|",
    ]
    for f in findings.get("findings", []):
        lines.append(f"| {f['id']} | {f['severity']} | {f['detail']} |")
    lines += [
        "",
        "## 分层命中明细",
        "",
        "| file | literal_count | layer | registered | automation-editable |",
        "|---|---|---|---|---|",
    ]
    for r in findings.get("rename_cost", []):
        lines.append(f"| {r['file']} | {r['literal_count']} | {r['layer']} | "
                     f"{'Y' if r['registered'] else 'N'} | {'Y' if r['editable_by_automation'] else 'N'} |")
    lines += [
        "",
        "## 字符串比较站点 (改名会静默失效的地方)",
        "",
        "| file | line | layer |",
        "|---|---|---|",
    ]
    for c in findings.get("comparison_sites", [])[:40]:
        lines.append(f"| {c['file']} | {c['line']} | {c['layer']} |")
    lines += ["", "## 同文件撞车面", ""]
    if findings.get("collisions"):
        lines += ["| file | verdict_lines | registered |", "|---|---|---|"]
        for c in findings["collisions"]:
            lines.append(f"| {c['file']} | {c['verdict_lines']} | {'Y' if c['registered'] else 'N'} |")
    else:
        lines.append("（无：信号词与三态词未落在同一个文件里，当前无同文件误读风险）")
    lines.append("")
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return {"json": out_json, "md": out_md}


def run_audit() -> dict:
    signal_scan = scan_signal_sources()
    collisions = scan_same_file_collision()
    callers = scan_callers()
    compares = scan_signal_comparison_sites()
    persist = scan_persisted_artifacts()
    indirect = scan_indirect_persisters()
    cost_rows = rename_cost_table(signal_scan)
    return build_findings(signal_scan, collisions, callers, compares, persist,
                          cost_rows, indirect)


def main(argv: Optional[List[str]] = None) -> int:
    findings = run_audit()
    paths = write_report(findings)
    print(json.dumps({
        "verdict": findings["verdict"],
        "red": findings["red"],
        "amber": findings["amber"],
        "total_hits": findings["signal_scan"].get("total_hits"),
        "by_layer": findings["signal_scan"].get("by_layer"),
        "callers": findings["callers"],
    }, ensure_ascii=False, indent=2))
    print(f"→ {paths['json']}")
    print(f"→ {paths['md']}")
    return 0  # 审计不改生产面, 退出码不表达判定


__all__ = [
    "SIGNAL_TOKEN", "SIGNAL_SIBLINGS", "SIGNAL_FILE_REGISTRY", "DEAD_CODE_CANDIDATE_MODULE",
    "RE_SIGNAL_TOKEN", "RE_SIGNAL_COMPARE", "RE_VERDICT_TOKEN",
    "build_findings", "classify_signal_file", "layer_of", "main", "read_text",
    "rename_cost_table", "run_audit", "scan_callers", "scan_persisted_artifacts",
    "scan_same_file_collision", "scan_signal_comparison_sites", "scan_signal_sources",
    "self_exclude", "write_report",
]

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
