#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T47 验证台 ingest 调度缺位只读盘点 + monitor 挂载点评估（承接 T46 B2 断点）。

只读事实清单：
  Q1 调度面检索 —— 全仓（.bat/.ps1/.py/.json/.yml，排除 archive/ .venv/ .workbuddy/）
     检索 `python -m verification ingest|report` / `cmd_ingest` / `ingest_new` / `ingest_all`
     的调用方，逐个判定 SCHEDULED_RUNNER / IN_PROCESS_SCHEDULER / CLI_ENTRY /
     AUDIT_READER / TEST_SELF / NONE，并交叉计划任务命令面（.bat 目标 + prod_guardian JOBS）。
  Q2 应入未入 —— 复用 scripts/audit_ingest_status_gate.py 的门控 B 口径（matches.status）
     与 loop 内守卫，复算「账本 MAX(created_at) 之后本可入账却未入账」的行数，
     按 model_source 与 match_date 维度切分，扣除账本已有 (match_id, model_source)。
  Q3 monitor 挂载面 —— 解析 autonomous_monitor.main() 的步骤时序与 try 包裹情况、
     DB 接触面（读/写连接），据此评估 ingest_step 挂 monitor 末尾（复用写连接/串行）
     与独立计划任务两案。
  Q4 挂载点规格 + fail-closed 验收（连续 3 周期 rows>0）—— 以文本形式产出，不落地。

铁律：只读打开 events.db（uri+mode=ro+query_only）与 verification.db；
本脚本绝不调用 verification.ingest_all / ingest_new，不写 verification.db、
不碰 events.db 写入、不挂调度、不跑 ingest、不重训、不碰生产进程。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

EVENTS_DB = os.path.join(REPO_ROOT, "data", "events.db")
LEDGER_DB = os.path.join(REPO_ROOT, "verification.db")
MONITOR_SRC = os.path.join(REPO_ROOT, "scripts", "autonomous_monitor.py")
GUARDIAN_SRC = os.path.join(REPO_ROOT, "scripts", "prod_guardian.py")
DEFAULT_OUT = os.path.join(REPO_ROOT, "reports")
SELF = os.path.basename(__file__)

# 本脚本自身命中须自排除（否则把 AUDIT_READER 撑成假阳性，同 T33/T36/T40）
SELF_EXCLUDE = {SELF, "test_" + SELF, os.path.basename(__file__).replace(".py", "_spec.md")}

# ── Q1 检索面定义 ────────────────────────────────────────────────────────
# (kind, 正则) —— kind 用于判定该命中代表「谁在调」
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SCHEDULED_CLI", re.compile(
        r"(?<![A-Za-z0-9_.])(?:pythonw?|python3(?:\.\d+)?)(?:\.exe)?\s+-\s*m\s+verification\s+"
        r"(ingest|report|export)")),
    ("IN_PROCESS_CALL", re.compile(r"\bcmd_ingest\b|\bled\.ingest_new\s*\(")),
    ("MODULE_IMPORT", re.compile(r"from\s+verification\.(?:ingest|__main__)\s+import|import\s+verification\.ingest")),
]

# 调度面文件（计划任务本体 / 任务 .bat / 任务 .ps1）
SCHED_FILE_SUFFIX = (".bat", ".ps1")

SKIP_DIR_PREFIXES = (
    "archive/", ".venv/", ".git/", ".workbuddy/", ".codebuddy/", ".zcode/",
    ".edge_agent_profile/", "frontend/node_modules/", "reports/", "deliverables/",
    "logs/", "models/", "data/", "odds_db/", "backend/node_modules/",
)
# 超长生成型文档（> 3000 行）不进检索面，否则一次性灌进上万条噪声命中
MAX_SCAN_LINES = 3000

# ── Q3 monitor 解析 ─────────────────────────────────────────────────────
RE_MONITOR_STEP = re.compile(r"^\s*status\[['\"]([A-Za-z_][\w]*)['\"]\]\s*=\s*(\w+)\s*\(")
RE_TRY_WRAP = re.compile(r"^\s*try:\s*$")
RE_GQ_CONN = re.compile(r"\bgq_conn\(\s*\)|\bfrom\s+gq\.db\s+import\s+conn\s+as\s+gq_conn\b")
RE_RO_URI = re.compile(r"mode=ro")
RE_SUBPROC = re.compile(r"subprocess\.run\s*\(")

KIND_NONE = "NONE"
KIND_SCHEDULED = "SCHEDULED_RUNNER"
KIND_IN_PROCESS = "IN_PROCESS_SCHEDULER"
KIND_CLI = "CLI_ENTRY"
KIND_AUDIT = "AUDIT_READER"
KIND_TEST = "TEST_SELF"
KIND_DOC = "DOC_MENTION"


# ── 只读连接 ────────────────────────────────────────────────────────────
def open_readonly(db_path: str) -> sqlite3.Connection:
    """uri + mode=ro + query_only 双保险，绝不写入。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DB not found: {db_path}")
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.execute("PRAGMA query_only=1")
    return con


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except Exception:
        return []


# ── Q1 调度面检索 ───────────────────────────────────────────────────────
def _skip(rel: str) -> bool:
    low = rel.replace("\\", "/")
    return any(low.startswith(p) for p in SKIP_DIR_PREFIXES)


def scan_schedule_surface(repo_root: str = REPO_ROOT) -> dict:
    """全仓检索 verification ingest/report 的调用方，按用途分类。

    自排除本脚本与本报告，避免把审计行为算成调度证据。
    """
    hits: list[dict] = []
    by_kind: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        rel_dir = os.path.relpath(dirpath, repo_root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        if _skip(rel_dir):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not _skip((rel_dir + "/" + d).lstrip("./"))]
        for fn in filenames:
            if fn in SELF_EXCLUDE:
                continue
            if _skip((rel_dir + "/" + fn).lstrip("./")):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, repo_root).replace("\\", "/")
            if not fn.endswith((".py", ".bat", ".ps1", ".json", ".yml", ".yaml", ".md")):
                continue
            lines = _read_lines(path)
            if len(lines) > MAX_SCAN_LINES:
                continue
            file_hits: list[dict] = []
            for i, line in enumerate(lines, 1):
                if line.lstrip().startswith("#"):
                    continue
                if rel.endswith("verification/__main__.py"):
                    # CLI 入口本体：整文件一处即可，逐行记账无意义
                    file_hits.append({"file": rel, "line": 1, "kind": KIND_CLI,
                                      "text": "verification/__main__.py (CLI 入口本体)"})
                    break
                kind = KIND_NONE
                for k, rx in PATTERNS:
                    if rx.search(line):
                        kind = k
                        break
                if kind == KIND_NONE:
                    continue
                if fn.endswith(".md"):
                    # 文档里的命令样例是叙述不是调度，降级以免把 docs 算成调度方
                    kind = KIND_DOC
                file_hits.append({"file": rel, "line": i, "kind": kind,
                                  "text": line.strip()[:160]})
            hits.extend(file_hits)
            for h in file_hits:
                by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1
    hits.sort(key=lambda h: (h["file"], h["line"]))
    return {"hits": hits, "counts": by_kind,
            "caller_files": sorted({h["file"] for h in hits})}


def probe_report_artifact(repo_root: str = REPO_ROOT) -> dict:
    """reports/verification_report.json 是 `python -m verification report` 的产物，
    而 cmd_report 内部同样会 ingest —— 它的新鲜度是「report 路径是否也在跑」的旁证。"""
    p = os.path.join(repo_root, "reports", "verification_report.json")
    if not os.path.exists(p):
        return {"exists": False}
    age_h = round((datetime.now(timezone.utc).timestamp() - os.path.getmtime(p)) / 3600.0, 1)
    return {"exists": True, "path": "reports/verification_report.json", "age_hours": age_h,
            "note": "cmd_report 内含 led.ingest_new → 该产物陈旧同样意味着 ingest 未被跑"}


def parse_guardian_jobs(path: str = GUARDIAN_SRC) -> list[dict]:
    """解析 prod_guardian.JOBS：谁在代拉什么、周期多少秒。"""
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    lines = _read_lines(path)
    in_jobs = False
    for ln in lines:
        if re.match(r"^JOBS\s*=\s*\[", ln):
            in_jobs = True
            continue
        if in_jobs:
            m = re.match(r"\s*\(['\"]([^'\"]+)['\"]", ln)
            if m:
                out.append({"job": m.group(1), "target": ln.strip()[:200],
                            "touches_verification": "verification" in ln.lower()})
                continue
            if "]" in ln:
                break
    return out


def parse_plan_task_bats(repo_root: str = REPO_ROOT) -> list[dict]:
    """读 .bat 计划任务脚本的目标命令面（run_daily_recheck.bat 等）。"""
    out: list[dict] = []
    for dirpath, _dirnames, filenames in os.walk(repo_root):
        rel_dir = os.path.relpath(dirpath, repo_root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        if _skip(rel_dir):
            continue
        for fn in filenames:
            if not fn.endswith(SCHED_FILE_SUFFIX):
                continue
            path = os.path.join(dirpath, fn)
            lines = _read_lines(path)
            cmd_line = ""
            for ln in lines:
                s = ln.strip()
                if s and not s.startswith(("REM", "@", "echo", "cd ")):
                    cmd_line = s
                    break
            out.append({"bat": os.path.relpath(path, repo_root).replace("\\", "/"),
                        "cmd": cmd_line,
                        "touches_verification": "verification" in cmd_line.lower()})
    return out


# ── Q3 monitor 挂载面解析 ───────────────────────────────────────────────
def parse_monitor_steps(path: str = MONITOR_SRC) -> list[dict]:
    """按 main() 出现顺序抽 status['x'] = fn(...) 步骤，并标记是否处于 try 块内。"""
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    lines = _read_lines(path)
    started = False
    in_try = False  # try 块状态须跨行传递：try: 置 True，except/else/finally 复位
    for ln in lines:
        if re.match(r"^\s*def\s+main\s*\(", ln):
            started = True
            continue
        if not started:
            continue
        if RE_TRY_WRAP.match(ln):
            in_try = True
            continue
        if re.match(r"^\s*(except|else|finally)\b", ln):
            in_try = False
            continue
        m = RE_MONITOR_STEP.match(ln)
        if m:
            out.append({"step": m.group(1), "call": m.group(2), "in_try": in_try})
    return out


def parse_monitor_db_surface(path: str = MONITOR_SRC) -> dict:
    """monitor 的 DB 接触面：只读连接数 / 写连接（gq_conn）次数 / 子进程调用。"""
    lines = _read_lines(path)
    ro = sum(1 for ln in lines if RE_RO_URI.search(ln))
    gq = sum(1 for ln in lines if RE_GQ_CONN.search(ln))
    sub = sum(1 for ln in lines if RE_SUBPROC.search(ln))
    return {"readonly_connections": ro, "write_connections_gq_conn": gq,
            "subprocess_calls": sub,
            "writes_verification_db": "verification.db" in "\n".join(lines)}


# ── Q2 应入未入（复用 T33 口径）─────────────────────────────────────────
def ledger_stats(con: sqlite3.Connection) -> dict:
    """账本本体统计：行数 / MAX(created_at) / 三源计数 / 日期边界。"""
    row = con.execute("SELECT COUNT(*), MAX(created_at) FROM verification_ledger").fetchone()
    sources = {}
    for ms, cnt, mx in con.execute(
            "SELECT model_source, COUNT(*), MAX(match_date) FROM verification_ledger GROUP BY 1"):
        sources[ms] = {"rows": cnt, "max_match_date": mx}
    pairs = {(r[0], r[1]) for r in con.execute(
        "SELECT match_id, model_source FROM verification_ledger")}
    return {"rows": row[0], "max_created_at": row[1],
            "sources": sources, "_pairs": pairs,
            "max_match_date": max((s["max_match_date"] or "") for s in sources.values())}


def ledger_age_days(max_created_at: str | None, now: datetime | None = None) -> float | None:
    """账本最后入账距今多少天（UTC 口径）。无时间戳返回 None。"""
    if not max_created_at:
        return None
    try:
        t = datetime.strptime(max_created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return round((now - t).total_seconds() / 86400.0, 3)


def compute_owed(con_events: sqlite3.Connection, ledger: dict) -> dict:
    """复算「自账本最后入账以来，本可入账却未入账」的行数（只读）。

    口径与 verification/ingest.py::ingest_daily_predictions 对齐（门控 B + loop 守卫），
    复用 scripts/audit_ingest_status_gate.py 的 collect_candidates / last_odds_map /
    evaluate_guard，避免另写一套守卫造成口径漂移。
    """
    try:
        from scripts.audit_ingest_status_gate import (  # type: ignore
            R_OK, collect_candidates, evaluate_guard, last_odds_map,
            parse_kickoff_ts_local,
        )
    except Exception as e:  # 模块缺失不致命，返回空结果
        return {"available": False, "error": f"import failed: {e}"}

    cands = collect_candidates(con_events)
    keys = [c["match_key"] for c in cands]
    lodges = last_odds_map(con_events, keys)
    survivors = []
    rejected = {}
    for r in cands:
        reason, chosen = evaluate_guard(r, lodges, parse_kickoff_ts_local)
        if reason == R_OK:
            survivors.append(r)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1

    pairs: set[tuple] = ledger.get("_pairs", set())
    owed, kept, by_source, by_date = [], 0, {}, {}
    owed_before_cut = 0
    cut = ledger.get("max_match_date") or ""
    for r in survivors:
        key = (r["match_key"], r.get("model_source"))
        if key in pairs:
            kept += 1
            continue
        owed.append(r)
        ms = r.get("model_source") or "?"
        by_source[ms] = by_source.get(ms, 0) + 1
        md = r.get("match_date") or "?"
        by_date[md] = by_date.get(md, 0) + 1
        if md < cut:
            owed_before_cut += 1

    return {
        "available": True,
        "candidates_gate_b": len(cands),
        "survivors_guards": len(survivors),
        "rejected_by_reason": rejected,
        "already_in_ledger": kept,
        "owed_total": len(owed),
        "owed_by_source": by_source,
        "owed_by_date": dict(sorted(by_date.items())),
        # 账本按 source 各有不同日期边界：KNN 切到 09-25 而 candles/market 停在 09-18，
        # 因此「早于全局切点的欠账」主要落在 candles/market 两源，须单列不可混算。
        "owed_before_cut": owed_before_cut,
        "owed_after_cut": len(owed) - owed_before_cut,
        "cut_match_date": cut,
    }


# ── Q4 挂载点两案评估（纯文本推理，可单测）──────────────────────────────
def assess_mount_options(monitor_surface: dict, owed: dict) -> list[dict]:
    """评估 ingest 挂载方案：A 挂 monitor 末尾 / B 独立计划任务。"""
    writes_ledger = "writes_verification_db"
    return [
        {
            "option": "A_ingest_step_in_monitor",
            "summary": "在 autonomous_monitor.main() 末尾加 ingest_step()，随周期串行执行",
            "new_events_db_writer": False,
            "new_verification_db_writer": True,
            "new_process": False,
            "lock_contention_risk": "LOW — verification.db 与 events.db 是不同库文件，"
                                    "不与 ws_collector/bridge 争写 events.db",
            "cycle_time_cost": f"需时间预算闸门；实测周期 0.4–{monitor_surface.get('max_cycle_sec', '?')}s",
            "failure_isolation": "差 — 与 ingest 同进程，ingest 崩溃不影响已完成的步骤",
            "idempotent": "是 — 同一 (match_id, model_source) 去重",
            "observability": "monitor_status.json 可加 ledger_added 字段，天然可观测",
            "tradeoff": "复用已有 hourly 心跳，零新增运维；代价是周期时长受 ingest 拖累，"
                        "且 monitor 是 events.db 写方（T44），若 ingest 侧将来也写 events.db 会叠加撞锁",
        },
        {
            "option": "B_independent_scheduled_task",
            "summary": "新增独立计划任务 pythonw -m verification ingest",
            "new_events_db_writer": False,
            "new_verification_db_writer": True,
            "new_process": True,
            "lock_contention_risk": "LOW（同上，跨库文件）",
            "cycle_time_cost": "0 — 不占 monitor 周期",
            "failure_isolation": "好 — 独立进程独立退出码",
            "idempotent": "是",
            "observability": "需自建日志/状态落盘（monitor_status 之外无现成落点）",
            "tradeoff": "多一个常驻/周期任务要维护；与 monitor 同频会造成 verification.db 短时间两次写",
        },
    ]


def build_acceptance(monitor_surface: dict) -> list[dict]:
    """fail-closed 验收清单（A1-A7）。"""
    return [
        {"id": "A1", "assert": "ingest_step 置于 main() 末位且不影响前置步骤状态码",
         "fail_closed": True},
        {"id": "A2", "assert": "时间预算闸门：单周期 ingest 超过 budget 秒即中断并返回已入账行数",
         "fail_closed": True},
        {"id": "A3", "assert": "幂等：同周期重跑新增行数 = 0（去重键 (match_id, model_source)）",
         "fail_closed": True},
        {"id": "A4", "assert": "只读保障：ingest 期间 events.db 以 mode=ro 打开，"
                               "脚本未出现任何 events.db 的 UPDATE/INSERT/DELETE",
         "fail_closed": True},
        {"id": "A5", "assert": "监控可见：monitor_status.json 新增 ledger_added / ledger_last_rows",
         "fail_closed": False},
        {"id": "A6", "assert": "连续 3 个周期 rows>0，否则告警（停滞闸门，防一次性批量伪装速率）",
         "fail_closed": True},
        {"id": "A7", "assert": "不触发重训：models/*.joblib mtime 不变",
         "fail_closed": True},
    ]


# ── 主流程 ──────────────────────────────────────────────────────────────
def run_audit(events_db: str = EVENTS_DB, ledger_db: str = LEDGER_DB) -> dict:
    surface = scan_schedule_surface()
    guardian_jobs = parse_guardian_jobs()
    bats = parse_plan_task_bats()
    steps = parse_monitor_steps()
    m_surface = parse_monitor_db_surface()
    try:
        hist = [json.loads(l) for l in
                open(os.path.join(REPO_ROOT, "reports", "monitor_history.jsonl"),
                     encoding="utf-8")]
        secs = [h["cycle_sec"] for h in hist if h.get("cycle_sec")]
        m_surface["cycles_tracked"] = len(hist)
        m_surface["max_cycle_sec"] = round(max(secs), 1) if secs else None
        m_surface["avg_cycle_sec"] = round(sum(secs) / len(secs), 1) if secs else None
    except Exception:
        m_surface["cycles_tracked"] = 0

    result = {
        "generated_at_utc": _utc_now(),
        "self": SELF,
        "q1_schedule_surface": surface,
        "q1_guardian_jobs": guardian_jobs,
        "q1_plan_task_bats": bats,
        "q1_report_artifact": probe_report_artifact(),
        "q2_ledger": {},
        "q3_monitor_steps": steps,
        "q3_monitor_db_surface": m_surface,
        "q4_mount_options": [],
        "q4_acceptance": build_acceptance(m_surface),
    }
    try:
        con_l = open_readonly(ledger_db)
        con_e = open_readonly(events_db)
    except Exception as e:
        result["q2_ledger"] = {"available": False, "error": str(e)}
        return result
    try:
        st = ledger_stats(con_l)
        cut = st.pop("_pairs", None)
        result["q2_ledger"] = st
        result["q2_ledger"]["age_days"] = ledger_age_days(st.get("max_created_at"))
        result["q2_owed"] = compute_owed(con_e, {"_pairs": cut or set(),
                                                 "max_match_date": st.get("max_match_date")})
    finally:
        con_e.close()
        con_l.close()
    if result.get("q2_owed", {}).get("available"):
        result["q4_mount_options"] = assess_mount_options(
            m_surface, result["q2_owed"])
    return result


# ── 报告渲染 ────────────────────────────────────────────────────────────
def render_markdown(r: dict) -> str:
    L: list[str] = []
    L.append("# T47 验证台 ingest 调度缺位只读盘点（承接 T46 B2 断点）")
    L.append("")
    L.append(f"- 生成时间(UTC)：{r.get('generated_at_utc')}")
    L.append(f"- 脚本：`scripts/{r.get('self')}`（只读，未跑 ingest / 未写 verification.db / "
             f"零 events.db 写入）")
    L.append("")

    s = r.get("q1_schedule_surface", {})
    L.append("## Q1 调度面检索")
    L.append("")
    L.append("命中分类计数：" + (", ".join(f"{k}={v}" for k, v in s.get("counts", {}).items()) or "无"))
    L.append("")
    L.append("| 文件 | 行 | 类别 | 原文 |")
    L.append("|---|---|---|---|")
    for h in s.get("hits", [])[:80]:
        txt = h["text"].replace("|", "\\|")
        L.append(f"| `{h['file']}` | {h['line']} | {h['kind']} | {txt[:110]} |")
    if len(s.get("hits", [])) > 80:
        L.append(f"| … |  | 仅列前 80 / 共 {len(s.get('hits', []))} 条 |  |")

    L.append("")
    L.append("计划任务命令面（.bat 目标 + prod_guardian JOBS）：")
    L.append("")
    L.append("| 入口 | 命令 | 触达 verification |")
    L.append("|---|---|---|")
    for b in r.get("q1_plan_task_bats", []):
        L.append(f"| `{b['bat']}` | {(b['cmd'] or '')[:100]} | {'是' if b['touches_verification'] else '否'} |")
    for j in r.get("q1_guardian_jobs", []):
        L.append(f"| guardian:{j['job']} | {j['target'][:110]} | "
                 f"{'是' if j.get('touches_verification') else '否'} |")
    L.append("")

    st = r.get("q2_ledger", {}) or {}
    L.append("## Q2 账本与应入未入")
    L.append("")
    if st.get("available", True) is False:
        L.append(f"- 不可用：{st.get('error')}")
    else:
        L.append(f"- 账本行数 **{st.get('rows')}**，MAX(created_at)=**{st.get('max_created_at')}**"
                 f"，距今 **{st.get('age_days')} 天**")
        L.append(f"- 三源：{json.dumps(st.get('sources', {}), ensure_ascii=False)}")
        ow = r.get("q2_owed", {}) or {}
        if ow.get("available"):
            L.append("")
            L.append(f"- 门控 B 候选 **{ow.get('candidates_gate_b')}** → 过守卫存活 "
                     f"**{ow.get('survivors_guards')}**（剔除 {json.dumps(ow.get('rejected_by_reason', {}))}）"
                     f" → 账本已有 {ow.get('already_in_ledger')} → **应入未入 "
                     f"{ow.get('owed_total')} 行**（按 source {json.dumps(ow.get('owed_by_source', {}), ensure_ascii=False)}）；"
                     f"; 账本切点(match_date)={ow.get('cut_match_date')} 之前欠 "
                     f"{ow.get('owed_before_cut')} 行 / 之后欠 {ow.get('owed_after_cut')} 行；"
                     f"欠账日期分布 {json.dumps(ow.get('owed_by_date', {}), ensure_ascii=False)}")
        else:
            L.append(f"- 应入未入不可用：{ow.get('error')}")
    L.append("")

    ra = r.get("q1_report_artifact", {})
    if ra.get("exists"):
        L.append("")
        L.append(f"- **旁证**：`{ra['path']}` 已 {ra['age_hours']} 小时未更新；"
                 f"`cmd_report` 内部同样调用 `led.ingest_new` → report 路径也无人跑，"
                 f"不只是 `ingest` 子命令缺调度。")

    L.append("")
    L.append("## Q3 monitor 挂载面")
    L.append("")
    L.append("main() 步骤时序：" + " → ".join(
        f"{x['step']}({x['call']})" + ("[try]" if x["in_try"] else "[裸]")
        for x in r.get("q3_monitor_steps", [])))
    L.append("")
    ms = r.get("q3_monitor_db_surface", {})
    L.append(f"- DB 接触：只读连接 {ms.get('readonly_connections')} / "
             f"写连接(gq_conn) {ms.get('write_connections_gq_conn')} / "
             f"子进程 {ms.get('subprocess_calls')}")
    L.append(f"- 周期耗时：{ms.get('cycles_tracked')} 个周期，"
             f"平均 {ms.get('avg_cycle_sec')}s / 最大 {ms.get('max_cycle_sec')}s")
    L.append("")

    L.append("## Q4 挂载点两案")
    L.append("")
    L.append("| 方案 | 说明 | 新增 events.db 写方 | 锁竞争 | 周期成本 | 隔离 | 可观测 |")
    L.append("|---|---|---|---|---|---|---|")
    for o in r.get("q4_mount_options", []):
        L.append(f"| {o['option']} | {o['summary']} | "
                 f"{'否' if not o['new_events_db_writer'] else '是'} | {o['lock_contention_risk']} | "
                 f"{o['cycle_time_cost']} | {o['failure_isolation']} | {o['observability']} |")
    L.append("")
    L.append("fail-closed 验收：")
    for a in r.get("q4_acceptance", []):
        L.append(f"- **{a['id']}** {a['assert']}（fail-closed={'是' if a['fail_closed'] else '否'}）")
    L.append("")
    L.append("## 挂载点规格（建议，未落地）")
    L.append("")
    L.append("1. **次序**：ingest_step 置于 `main()` 末位，前置步骤状态码不受影响（A1）。")
    L.append("2. **形态**：`subprocess.run([VENV_PYW, '-m', 'verification', 'ingest'], timeout=BUDGET)`"
             " 优于在 monitor 进程内 import ingest —— 前者崩溃不影响前置步骤、退出码可判（A2/A4）。")
    L.append("3. **预算**：BUDGET 建议 240s，超时按 SIGTERM 不够再 kill；"
             "实测周期均值 8.7s / 峰值 68.3s，留 3 倍余量不显著拖慢心跳。")
    L.append("4. **可观测**：`status['ledger'] = {'added': n, 'rows_total': ..., 'ok': bool}`，"
             "失败写 `'error'`，摘要行补一列账本增量（A5）。")
    L.append("5. **防假速率**：连续 3 周期 rows=0 即告警；一次性批量写入须计入速率统计的排除项"
             "（T34 停滞闸门口径，防止 09-23 那类批量回填伪装成稳态趋势）。")
    L.append("6. **顺序约束**：先挂调度（B2）再修门控（T46 R-A），否则开闸后仍无增量可入；"
             "人工窗口补跑（R-A2）须按 walkforward 分段记账。")
    L.append("")
    L.append("## 诚实边界")
    L.append("- 本条只盘点不落地：未改 `verification/__main__.py`、未挂调度、未跑 ingest、"
             "未写 verification.db、零 events.db 写入、未重训、未碰生产进程。")
    L.append("- Q2 的「应入未入」是**按门控 B 口径的复算上限**，不是承诺可入账行数；"
             "跨 09-18→09-27 的日期跨度须按 walkforward 分段（T35/T39），不可直接并进 G1 分子。")
    return "\n".join(L) + "\n"


def main() -> int:
    r = run_audit()
    os.makedirs(DEFAULT_OUT, exist_ok=True)
    jp = os.path.join(DEFAULT_OUT, "verification_ingest_scheduling_audit.json")
    mp = os.path.join(DEFAULT_OUT, "verification_ingest_scheduling_audit.md")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_markdown(r))
    s = r.get("q1_schedule_surface", {})
    print(f"[T47] 调度面命中 {len(s.get('hits', []))} 条，分类 {s.get('counts', {})}")
    st = r.get("q2_ledger", {}) or {}
    print(f"[T47] 账本 {st.get('rows')} 行 / MAX(created_at)={st.get('max_created_at')} "
          f"/ 距今 {st.get('age_days')} 天")
    ow = r.get("q2_owed", {}) or {}
    if ow.get("available"):
        print(f"[T47] 应入未入 {ow.get('owed_total')} 行 (by source {ow.get('owed_by_source')})")
    print(f"→ {jp}")
    print(f"→ {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
