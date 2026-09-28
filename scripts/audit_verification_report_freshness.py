#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T51 验证台报告「时效 + 三态不变式」只读审计 (承接 T38③ / T42 / T47)。

审计三个问题, 全部只读, 不改 gates.py / 不跑验证台 / 不写 events.db / 不碰调度:

  Q1 三态出口   三态只允许 {EDGE / NO EDGE / INCONCLUSIVE} 且**唯一出口**是
                ``verification/gates.py::Gates.verdict``; 任何绕过它直接写死
                三态字面量的渲染面, 一旦将来改了 gates 语义就会静默撒谎。
  Q2 IR-30 EDGE 一旦出现 EDGE, 报告必须同时带「胜率/隐含概率/edge_pp」与
                「G6 零信息机械对照三元组」; 否则 EDGE 不可被任何人独立复核
                (09-23 去水事故的根本教训)。
  Q3 时效       ``reports/verification_report.json`` 停在 2026-09-25T04:26:34Z,
                而 T47 已机械证明 verification CLI 的 ingest / report 入口**零
                调度方** → 报告必然过期, 下游 (看板/交接单/审计脚本) 却把旧
                generated_at 当普通文本渲染, 读者无从判断新旧。

  T57 变更: 判定词表与 Tier 表已上收到 ``scripts/verdict_guard_ssot.py`` (与
  ``audit_mh_train_div_bypass.py`` 共用 SSoT); 词表由「两侧带引号」放宽为
  「裸词边界」, 因此能捞到旧词表漏掉的 ``mh_train_walkforward.py`` /
  ``mh_train_walkforward_x.py``。放宽的代价是噪声面上涨(注释里的 EDGE 也命中),
  压噪声改由 Tier 表负责; 登记册每条必须写理由, 空理由即不许登记。

  ⚠ 本文件禁止写出 ``python -m verification ...`` 形状的命令行字面量 ——
    T47 的 ``SCHEDULED_CLI`` 正则按该形状识别调度方, 本文件的注释里写一次就会把
    T47 的 ``SCHEDULED_CLI`` 从 0 打成 1, 使 T47「零调度方」结论当场失效
    (本轮实测复现, 已改为只陈述子命令名)。

纯只读: 不跑 ingest / 不写 verification.db / 零 events.db 写入 / 不改任何生产文件 /
不挂调度 / 零进程操作。
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

import verdict_guard_ssot as VG  # noqa: E402  (T57 判定词表/Tier SSoT)

REPORTS: str = os.path.join(ROOT, "reports")
REPORT_JSON: str = os.path.join(REPORTS, "verification_report.json")
DASHBOARD_JS: str = os.path.join(ROOT, "scripts", "build_dashboard.py")
VERIFICATION_PKG: str = os.path.join(ROOT, "verification")

#: 三态合法枚举 (IR-30 口径: 只允许这三条, 不接受 NO_EDGE / UNKNOWN / 空串)。
ALLOWED_VERDICTS: Tuple[str, ...] = ("EDGE", "NO EDGE", "INCONCLUSIVE")

#: IR-30: 报 EDGE 必须同时可复核的字段 (胜率 / 隐含概率 / edge_pp + G6 机械对照)。
IR30_EDGE_REQUIRED: Tuple[str, ...] = (
    "win_rate",
    "implied",
    "edge_pp",
    "mech_fav_roi",
    "paired_excess",
    "paired_excess_ci_low",
)

#: 时效闸门 (小时): > WARN 提示过期, > HARD 视为不得当最新结论引用。
FRESH_WARN_HOURS: float = 24.0
FRESH_HARD_HOURS: float = 168.0

#: 扫描时跳过的目录 (与 T44/T47 同款跳过表)。
SKIP_DIRS: Tuple[str, ...] = (
    ".venv", "node_modules", "archive", ".git", ".workbuddy",
    ".codebuddy", ".zcode", "__pycache__", "e2e", "dist",
)

#: 判定词表 SSoT 在 ``scripts/verdict_guard_ssot.py`` (T57 合流, 与 T53 共用同一份)。
#: 本模块**禁止**自带判定词正则 —— 放宽版能捞到 T51 旧词表漏掉的
#: ``mh_train_walkforward.py`` / ``mh_train_walkforward_x.py``（行尾无引号写法）。
RE_VERDICT_LITERAL = VG.RE_VERDICT_TOKEN
#: 精确版只作低召回对照用, 生产扫描不用（守卫回归用例会验证本模块未自带词表）。
RE_VERDICT_QUOTED = VG.RE_VERDICT_QUOTED

#: 分层豁免 (沿用 T30-D「拒绝现在就开零容忍」的立场, 但**必须留证不静默**);
#: Tier 命名与判定函数的 SSoT 同样在 ``scripts/verdict_guard_ssot.py`` (T57 合流)。
#: 本模块保留旧的模块级名字做薄封装, 真身统一到 SSoT, 避免两份清单互相打脸。
RENDER_CONSUMER_ALLOWED: Tuple[str, ...] = VG.RENDER_CONSUMER_ALLOWED
NOISE_EXPECTED_PREFIXES: Tuple[str, ...] = VG.NOISE_PREFIXES
DOC_META_PREFIXES: Tuple[str, ...] = VG.DOC_META_PREFIXES
DOC_META_MARKERS: Tuple[str, ...] = VG.DOC_META_MARKERS

#: 扫描排除: 本脚本、它的测试, 以及 T57 的共享 Tier/词表模块
#: (三者都含判定词字面量, 按 basename 自避而非写死名字)。
_BASE = os.path.basename(__file__)
_SELF_EXCLUDE: Tuple[str, ...] = (_BASE, "test_" + _BASE) + tuple(VG.GUARD_SELF_EXCLUDE)

#: 扫描 py 文件的扩展名。
PY_EXT: Tuple[str, ...] = (".py",)


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def self_exclude() -> Tuple[str, ...]:
    """自身与自身测试的基名 —— 字面量扫描必须排除 (同 T33/T36/T44 的 self-exclude 坑)。

    只排除脚本自己会让**它的测试文件**变成"未知产出体" (实测踩到: 本轮 21 条
    unexpected 里有 10 条来自本测试文件), 故按 basename 一起排除。
    """
    return _SELF_EXCLUDE


def iter_python_files(root: str = ROOT) -> List[str]:
    """递归列出可扫描的 .py 文件 (跳过 SKIP_DIRS)。"""
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]
        for fn in filenames:
            if fn.endswith(PY_EXT):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def read_text(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _rel(path: str) -> str:
    try:
        return os.path.relpath(path, ROOT).replace(os.sep, "/")
    except ValueError:  # 跨盘符兜底
        return os.path.basename(path)


def parse_ts(value) -> Optional[datetime]:
    """解析报告里的时间戳。支持 ``...Z`` 与 ``isoformat()`` 的 ``+00:00``。"""
    if not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Q2/Q3: 报告侧判定
# --------------------------------------------------------------------------

def load_report(path: str = REPORT_JSON) -> Optional[dict]:
    """读取报告 JSON; 缺失/损坏返回 None (绝不抛)。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def report_age_hours(data: dict, now: Optional[datetime] = None) -> Optional[float]:
    """报告相对“此刻”的小时龄。generated_at 缺失 → None。"""
    ts = parse_ts(data.get("generated_at"))
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    return round((now - ts).total_seconds() / 3600.0, 3)


def classify_freshness(age_hours: Optional[float],
                       warn: float = FRESH_WARN_HOURS,
                       hard: float = FRESH_HARD_HOURS) -> str:
    """时效分级: FRESH / STALE_WARN / STALE_HARD / UNKNOWN。"""
    if age_hours is None:
        return "UNKNOWN"
    if age_hours > hard:
        return "STALE_HARD"
    if age_hours > warn:
        return "STALE_WARN"
    return "FRESH"


def check_verdict_enum(data: dict) -> List[dict]:
    """三态不变式: 任何模型的 verdict 必须落在 ALLOWED_VERDICTS 内。"""
    bad: List[dict] = []
    for src, m in (data.get("models") or {}).items():
        if not isinstance(m, dict):
            bad.append({"model": src, "verdict": None, "issue": "NOT_A_DICT"})
            continue
        v = m.get("verdict")
        if v not in ALLOWED_VERDICTS:
            bad.append({"model": src, "verdict": v, "issue": "VERDICT_NOT_IN_ENUM"})
    return bad


def check_edge_fields(data: dict) -> List[dict]:
    """IR-30 数据面: EDGE 条目必须同时带胜率/隐含/edge_pp + G6 机械对照。"""
    gaps: List[dict] = []
    for src, m in (data.get("models") or {}).items():
        if not isinstance(m, dict) or m.get("verdict") != "EDGE":
            continue
        missing = [f for f in IR30_EDGE_REQUIRED if f not in m]
        if missing:
            gaps.append({"model": src, "missing": missing})
    return gaps


def check_renderer_edge_fields() -> List[str]:
    """IR-30 静态面 (真正可强制的一层): 渲染器能否产出 IR30_EDGE_REQUIRED。

    ``verification/report.py::render_json`` 目前只写 14 个字段, 不含
    ``mech_fav_roi/paired_excess/paired_excess_ci_low``, 也不含胜率/隐含/edge_pp
    → 所以即便 gates 判出 EDGE, JSON 里也**没有任何数字可供独立复核**。
    返回缺失字段名列表 (空=达标)。
    """
    src = read_text(os.path.join(VERIFICATION_PKG, "report.py")) or ""
    return [f for f in IR30_EDGE_REQUIRED if f not in src]


def classify_literal_file(rel: str) -> str:
    """把一个含三态字面量的文件分到 Tier 之一。

    T57: 判定函数已上收到 ``verdict_guard_ssot.classify_verdict_file`` (SSoT),
    本函数只做同名薄封装 —— 两份 Tier 表分叉正是本轮要治的病。
    """
    return VG.classify_verdict_file(rel)


def scan_verdict_literal_sources(root: str = ROOT) -> dict:
    """盘点全仓 .py 的三态字面量并按 Tier 分类。

    关键事实: ``analysis/live_goal_probe.py`` 等把 ``NO_EDGE`` 当**盘口信号**用,
    与验证台三态同形异义 → 全仓裸扫必被噪声淹没; 但也不能因此不给守卫,
    故按 Tier 分类并**把未分类项显式列成 RED**, 防止豁免清单悄悄长大。
    """
    buckets: Dict[str, List[dict]] = {}
    self_names = self_exclude()
    for path in iter_python_files(root):
        rel = _rel(path)
        if os.path.basename(path) in self_names:
            continue
        text = read_text(path)
        if text is None:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for mt in RE_VERDICT_LITERAL.finditer(line):
                # 放宽版词表无捕获组 (T57) → 取整词 (group(0)); 旧版取 group(1)。
                rec = {"file": rel, "line": i, "token": mt.group(0)
                       if mt.re.groups == 0 else mt.group(1),
                       "tier": classify_literal_file(rel)}
                buckets.setdefault(rec["tier"], []).append(rec)
    out = {k: v for k, v in buckets.items()}
    # 桶键统一取 SSoT 的 Tier 常量 (T57 合流: T53 用 expected/unexpected, 见同名键)。
    out["emitters_inside_package"] = buckets.get(VG.TIER_EMITTER, [])
    out["render_consumers"] = buckets.get(VG.TIER_RENDER, [])
    out["noise_known"] = buckets.get(VG.TIER_NOISE, [])
    out["doc_meta"] = buckets.get(VG.TIER_DOC, [])
    out["known_false_positive"] = buckets.get(VG.TIER_FALSE_POSITIVE, [])
    out["expected"] = buckets.get(VG.TIER_EXPECTED, [])
    out["unexpected"] = buckets.get(VG.TIER_UNEXPECTED, [])
    out["unexpected_count"] = len(out["unexpected"])
    return out


def scan_render_fallback(path: str = DASHBOARD_JS) -> dict:
    """看板渲染面: 未知三态是否被**静默**降级为中性样式 (fail-open 渲染)。

    ``build_dashboard.py`` 的 ``cls(v)`` 只认三个合法值, 其余落 ``t-na``(灰) ——
    非法 verdict 不会变成红色告警, 读者看不见异常。
    """
    src = read_text(path) or ""
    m = re.search(r"function cls\(v\)\{return(?P<body>.*?);\}", src)
    body = m.group("body") if m else ""
    mapped = sum(1 for v in ALLOWED_VERDICTS if f"'{v}'" in body or f'"{v}"' in body)
    return {
        "file": _rel(path),
        "found": bool(m),
        "allowed_mapped": mapped,
        "expected_mapped": len(ALLOWED_VERDICTS),
        "fallback_is_neutral": ("t-na" in body),
        "fail_open": bool(m) and mapped < len(ALLOWED_VERDICTS),
    }


def scan_consumers(root: str = ROOT) -> List[dict]:
    """谁读 reports/verification_report.json, 以及是否带任何时效判定。"""
    out: List[dict] = []
    me = set(self_exclude())
    needle = "verification_report.json"
    for path in iter_python_files(root):
        rel = _rel(path)
        if os.path.basename(path) in me or rel.startswith("verification/"):
            continue
        text = read_text(path) or ""
        for i, line in enumerate(text.splitlines(), 1):
            if needle in line:
                out.append({
                    "file": rel,
                    "line": i,
                    "checks_freshness": any(k in line for k in ("stale", "STALE", "age", "freshness")),
                })
    return out


# --------------------------------------------------------------------------
# 结论合成 (fail-closed)
# --------------------------------------------------------------------------

def build_findings(enum_violations: List[dict],
                   edge_gaps: List[dict],
                   renderer_missing: List[str],
                   freshness: str,
                   render: dict,
                   consumers: Optional[List[dict]] = None,
                   age_hours: Optional[float] = None,
                   literals: Optional[dict] = None) -> dict:
    """合成 findings 与 fail-closed 结论。**

    RED (阻塞, 必须修)   : R1 三态越界 / R2 渲染器无法产出 IR-30 字段 / R4 fail-open 渲染
    AMBER (已知根因)     : R3 报告过期 (根因=零调度, 见 T47)
    INFO                 : 消费点清单
    **任何 RED 或缺项即 FAIL, 不因其它项绿而放行。**
    """
    findings: List[dict] = []

    if enum_violations:
        findings.append({
            "id": "R1_VERDICT_OUT_OF_ENUM", "severity": "RED",
            "detail": f"{len(enum_violations)} 个模型 verdict 不在三态枚举内",
            "evidence": enum_violations,
        })
    if renderer_missing:
        findings.append({
            "id": "R2_RENDERER_CANNOT_PROVE_EDGE", "severity": "RED",
            "detail": "render_json 不产出 IR-30 必带字段, EDGE 不可独立复核: " + ",".join(renderer_missing),
            "evidence": renderer_missing,
        })
    if freshness == "UNKNOWN":
        findings.append({
            "id": "R3_REPORT_UNDATABLE", "severity": "RED",
            "detail": "报告缺 generated_at (或无法解析) → 任何读者都无法判断新旧, 视为不合格",
        })
    elif freshness in ("STALE_WARN", "STALE_HARD"):
        findings.append({
            "id": "R3_REPORT_EXPIRED", "severity": "AMBER",
            "detail": f"报告时效={freshness} (age={age_hours}h, 阈值 warn={FRESH_WARN_HOURS}h/hard={FRESH_HARD_HOURS}h); "
                      "根因=verification 零调度方 (T47 机械证明), 非数据损坏",
        })
    if render.get("fail_open"):
        findings.append({
            "id": "R4_RENDER_FAIL_OPEN", "severity": "RED",
            "detail": f"{render.get('file')} 的 cls() 只映射 {render.get('allowed_mapped')}/"
                      f"{render.get('expected_mapped')} 个合法三态, 其余静默落中性样式",
        })
    if edge_gaps:
        findings.append({
            "id": "R5_EDGE_MISSING_PROOF_FIELDS", "severity": "RED",
            "detail": "数据面出现 EDGE 但缺复核字段", "evidence": edge_gaps,
        })
    if literals is None:
        # fail-closed: 「这项没查」不能等于「这项通过」(同 T52 静默空结果的教训)。
        findings.append({
            "id": "R6_VERDICT_LITERAL_SCAN_NOT_RUN", "severity": "RED",
            "detail": "未执行三态字面量分层扫描 → 无法证明没有绕过 gates.verdict 的产出体",
        })
    unexpected = (literals or {}).get("unexpected_count", 0) if literals is not None else 0
    if unexpected:
        findings.append({
            "id": "R6_UNKNOWN_VERDICT_EMITTER", "severity": "RED",
            "detail": f"{unexpected} 处三态字面量落在未分层文件里 (既非 verification 包内 "
                      f"亦非已知噪声/元工具面), 须人工确认它不产出结论",
            "evidence": (literals or {}).get("unexpected", [])[:20],
        })

    red = [f for f in findings if f["severity"] == "RED"]
    amber = [f for f in findings if f["severity"] == "AMBER"]
    return {
        "findings": findings,
        "red": [f["id"] for f in red],
        "amber": [f["id"] for f in amber],
        "verdict": "FAIL" if red else "PASS",
        "consumers": consumers or [],
        "render": render,
        "freshness": freshness,
        "age_hours": age_hours,
        "literals": literals or {},
    }


def live_regression() -> dict:
    """活体回归: 真实报告必须能被解析, 且三态不变式必须可判定。"""
    data = load_report()
    if data is None:
        return {"exists": False, "verdict": "SKIP"}
    age = report_age_hours(data)
    violations = check_verdict_enum(data)
    return {
        "exists": True,
        "generated_at": data.get("generated_at"),
        "models": sorted((data.get("models") or {}).keys()),
        "age_hours": age,
        "freshness": classify_freshness(age),
        "enum_violations": violations,
        "verdict": "OK" if not violations else "FAIL",
    }


def write_report(findings: dict, out_json: str, out_md: str) -> Dict[str, str]:
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(findings, fh, ensure_ascii=False, indent=2)
    lines = [
        "# 验证台报告「时效 + 三态不变式」审计报告 (T51)",
        "",
        f"**判定: {findings['verdict']}**"
        + (f" (RED: {','.join(findings['red'])})" if findings["red"] else ""),
        "",
        f"- 报告时效: {findings['freshness']} (age={findings['age_hours']}h)",
        f"- 看板渲染: {findings['render'].get('file')} mapped="
        f"{findings['render'].get('allowed_mapped')}/{findings['render'].get('expected_mapped')} fail_open="
        f"{findings['render'].get('fail_open')}",
        f"- 消费点: {len(findings.get('consumers', []))} 处",
        "",
        "| id | severity | detail |",
        "|---|---|---|",
    ]
    lit = findings.get("literals") or {}
    if lit:
        lines.append(f"| 三态字面量 Tier | — | EMITTER {len(lit.get('emitters_inside_package', []))} / "
                     f"RENDER {len(lit.get('render_consumers', []))} / NOISE "
                     f"{len(lit.get('noise_known', []))} / DOC {len(lit.get('doc_meta', []))} / "
                     f"EXPECTED {len(lit.get('expected', []))} / FALSE_POSITIVE "
                     f"{len(lit.get('known_false_positive', []))} / UNEXPECTED "
                     f"{lit.get('unexpected_count', 0)} |")
    for f in findings.get("findings", []):
        lines.append(f"| {f['id']} | {f['severity']} | {f['detail']} |")
    lines.append("")
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return {"json": out_json, "md": out_md}


def run_audit() -> dict:
    data = load_report() or {}
    age = report_age_hours(data)
    fresh = classify_freshness(age)
    enum_viol = check_verdict_enum(data)
    edge_gaps = check_edge_fields(data)
    renderer_missing = check_renderer_edge_fields()
    render = scan_render_fallback()
    consumers = scan_consumers()
    literals = scan_verdict_literal_sources()
    return build_findings(enum_viol, edge_gaps, renderer_missing, fresh, render,
                          consumers, age, literals)


def main(argv: Optional[List[str]] = None) -> int:
    findings = run_audit()
    out_json = os.path.join(REPORTS, "verification_report_freshness_audit.json")
    out_md = os.path.join(REPORTS, "verification_report_freshness_audit.md")
    paths = write_report(findings, out_json, out_md)
    print(json.dumps({k: findings[k] for k in ("verdict", "red", "amber", "freshness", "age_hours")},
                     ensure_ascii=False, indent=2))
    print(f"→ {paths['json']}")
    print(f"→ {paths['md']}")
    return 0 if findings["verdict"] == "PASS" else 0  # 审计不改生产面, 退出码不表达判定


__all__ = [
    "ALLOWED_VERDICTS", "IR30_EDGE_REQUIRED", "FRESH_WARN_HOURS", "FRESH_HARD_HOURS",
    "RE_VERDICT_LITERAL", "RE_VERDICT_QUOTED",
    "build_findings", "check_edge_fields", "check_renderer_edge_fields",
    "check_verdict_enum", "classify_freshness", "load_report", "live_regression",
    "report_age_hours", "run_audit", "scan_consumers", "scan_render_fallback",
    "scan_verdict_literal_sources", "write_report",
]

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
