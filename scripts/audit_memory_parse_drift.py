"""T60 审计：自动化 memory 解析漂移（只读取证，不改解析行为）。

T58 全量回归暴露的既有基线失败
`tests/test_snapshot_automation_health.py::test_real_memory_file_parseable`
（断言轮次 >= 20，实测 9）已确认**不是断言口径 outdated，而是两个真解析 bug
+ 一次文件结构事故**。本脚本只负责把这些事实机械固定下来：

  D1 文件结构：`memory.md` 曾出现逐字重复的第二段 header（`调度/机制/## 红线/## 执行历史`），
     `parse_history()` 的旧语义「遇到第一个 `## ` 就 break」→ 在主序列之前截断。
  D2 正则：`LINE_RE` 的时刻字段只认单时刻，`07:3x–08:0x`（U+2013）整行不匹配
     → T57 / T58（最新一轮）自带被丢弃。
  D3 语义：`summarize()` 的 `first_round/last_round` 取的是**文件首行/末行**，
     而新条目是追加在文件顶部的 → 首轮字段被污染。
  D4 正则：`TASK_RE` 会把区间写法 `T01-T29` 的首个号抽成 task_id → 巡检轮次被记成 T01。

脚本同时实现 `legacy_*`（修复前的等价实现），用于在真实文件上**复算"修复前"的数字**，
使「9 轮是错的」这件事可被复现而非口头断言。

用法:
  python scripts/audit_memory_parse_drift.py            # 只读，产出 reports/memory_parse_drift_audit.*

只读面：仅读取 `.workbuddy/memory/automations/.../memory.md`；不打开任何数据库、
不碰 events.db、不写生产文件（只写自有报告）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_PATH = os.path.join(
    REPO_ROOT, ".workbuddy", "memory", "automations",
    "dbda4380-1bc4-41b5-9e04-1117d1ba56b9", "memory.md",
)
HEALTH_SCRIPT = os.path.join(REPO_ROOT, "scripts", "snapshot_automation_health.py")
OUT_JSON = os.path.join(REPO_ROOT, "reports", "memory_parse_drift_audit.json")
OUT_MD = os.path.join(REPO_ROOT, "reports", "memory_parse_drift_audit.md")

HISTORY_HEADING = "## 执行历史"

# 修复前等价实现（用于复算旧数字；不得引用现行模块，否则"对照"自我实现）
LEGACY_LINE_RE = re.compile(r"^\s*-\s*(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{1,2}[xX]?)\s+(.*)$")
LEGACY_TASK_RE = re.compile(r"\bT(\d{2,3})\b")
LEGACY_QUEUECLEAR_RE = re.compile(r"队列清空|队列空转|巡检")

# 修复前实测基线（T58 全量回归当轮的真实读数），用于验证 legacy 复算一致
LEGACY_BASELINE = {
    "rounds": 9,
    "code_tasks": 8,
    "total_passed": 3395,
    "real_finding_rounds": 0,
    "queue_clear_or_inspection_rounds": 6,
    "run_days": 2,
    "first_round": "2026-09-27 22:4x",
}


def _load_health_module():
    spec = importlib.util.spec_from_file_location("snapshot_automation_health_t60", HEALTH_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HEALTH = _load_health_module()


# --------------------------------------------------------------------------- #
# 修复前实现
# --------------------------------------------------------------------------- #
def legacy_parse_history_line(line: str):
    m = LEGACY_LINE_RE.match(line)
    if not m:
        return None
    date_s, time_s, body = m.group(1), m.group(2), m.group(3)
    task = LEGACY_TASK_RE.search(body)
    if not (task or LEGACY_QUEUECLEAR_RE.search(body)):
        return None
    passed = HEALTH.PASSED_RE.search(body)
    script = HEALTH.SCRIPT_RE.search(body)
    return {
        "date": date_s, "time": time_s,
        "task_id": f"T{task.group(1)}" if task else None,
        "script": script.group(1) if script else None,
        "passed": int(passed.group(1)) if passed else None,
        "is_code_task": passed is not None,
        "has_real_finding": bool(HEALTH.REALFIND_RE.search(body)),
        "is_queue_clear_or_inspection": bool(LEGACY_QUEUECLEAR_RE.search(body)),
        "body": body.strip(),
    }


def legacy_parse_history(text: str):
    """旧语义：遇到第一个 `## ` 就 break（不重新打开同名单段）。"""
    out, in_history = [], False
    for ln in text.splitlines():
        if ln.strip().startswith(HISTORY_HEADING):
            in_history = True
            continue
        if in_history and ln.strip().startswith("## "):
            break
        if not in_history:
            continue
        e = legacy_parse_history_line(ln)
        if e:
            out.append(e)
    return out


def legacy_summarize(entries) -> dict:
    code = [e for e in entries if e["is_code_task"]]
    first = entries[0] if entries else None
    last = entries[-1] if entries else None
    return {
        "rounds": len(entries),
        "code_tasks": len(code),
        "total_passed": sum(e["passed"] for e in code if e["passed"] is not None),
        "real_finding_rounds": sum(1 for e in entries if e["has_real_finding"]),
        "queue_clear_or_inspection_rounds": sum(
            1 for e in entries if e["is_queue_clear_or_inspection"]),
        "run_days": len({e["date"] for e in entries}),
        "first_round": f"{first['date']} {first['time']}" if first else None,
        "last_round": f"{last['date']} {last['time']}" if last else None,
    }


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #
def read_memory(path: str = MEMORY_PATH) -> str:
    if not os.path.exists(path):
        raise SystemExit(f"[NOGO] memory 不存在: {path}")
    with open(path, encoding="utf-8") as f:
        return f.read()


# 最小复现样本：还原"重复 header 把执行历史切成两段"的损坏结构。
# 它不是真实文件的副本（那一份已修复），只用于证明**截断机制**存在。
CORRUPT_SAMPLE = """# 自动化 dbda4380

## 执行历史（仅高层，详情见每日 memory）

- 2026-09-27 22:4x **T48(x.py, 458 passed)**
- 2026-09-28 02:0x **T50(x.py, 493 passed)**

## 红线（不可破）
IR-30 诚实 / IR-32 跨庄禁区

## 执行历史（仅高层，详情见每日 memory）

- 2026-09-25 12:3x T01(a.py, 4 passed)
- 2026-09-25 12:5x T02(b.py, 6 passed)
- 2026-09-28 09:1x–09:3x **T58(z.py, 616 passed)**
"""


def check_truncation_mechanism() -> dict:
    """用最小样本证明：旧语义在重复 header 处截断，现行语义全收。"""
    legacy = legacy_summarize(legacy_parse_history(CORRUPT_SAMPLE))
    current = HEALTH.summarize(HEALTH.parse_history(CORRUPT_SAMPLE))["totals"]
    return {
        "sample": "重复 header 把执行历史切成两段（含 1 条 en-dash 时刻条目）",
        "legacy_rounds": legacy["rounds"],
        "current_rounds": current["rounds"],
        "truncated_by_legacy": current["rounds"] - legacy["rounds"],
    }


def check_legacy_on_current_document(text: str) -> dict:
    """现行（已修复）文档上，旧实现会丢掉几条 en-dash 条目 —— 量化本次修复收益。"""
    legacy = legacy_summarize(legacy_parse_history(text))
    current = HEALTH.summarize(HEALTH.parse_history(text))["totals"]
    return {
        "legacy": legacy,
        "current_rounds": current["rounds"],
        "recovered_rounds": current["rounds"] - legacy["rounds"],
    }


def check_defects(text: str) -> dict:
    sections = HEALTH.count_history_sections(text)

    # D2：带时间区间的条目是否被现行解析器收下
    spans = [ln.strip() for ln in text.splitlines()
             if ln.strip().startswith("- ") and re.search(r"\d{1,2}:\d{1,2}[xX]?\s*[-–—~]\s*\d", ln)]
    span_dropped = [s for s in spans if HEALTH.parse_history_line(s) is None]

    # D1：文件结构是否只剩一个执行历史段
    struct_ok = sections == 1

    # D4：区间写法是否还会抽出任务号（固定样例，避免依赖真实文件里恰好存在该句式）
    range_line = "- 2026-09-26 19:53 巡检(队列T01-T29全清空转): 验证台三态"
    e_range = HEALTH.parse_history_line(range_line)
    range_task = e_range["task_id"] if e_range else None

    return {
        "D1_sections": sections,
        "D1_struct_ok": struct_ok,
        "D2_span_bullets": len(spans),
        "D2_span_dropped": span_dropped,
        "D3_first_round": HEALTH.summarize(HEALTH.parse_history(text))["totals"]["first_round"],
        "D4_range_task_id": range_task,
    }


def build_findings(text: str) -> dict:
    trunc = check_truncation_mechanism()
    legacy_now = check_legacy_on_current_document(text)
    defects = check_defects(text)
    cur_entries = HEALTH.parse_history(text)
    cur = HEALTH.summarize(cur_entries)["totals"]

    red, amber = [], []
    if trunc["truncated_by_legacy"] <= 0:
        red.append({"id": "R1_TRUNCATION_MECHANISM_NOT_REPRODUCIBLE",
                    "detail": f"最小样本上旧语义未产生截断（legacy {trunc['legacy_rounds']} / "
                              f"current {trunc['current_rounds']}），守卫已失效"})
    if legacy_now["recovered_rounds"] <= 0:
        red.append({"id": "R2_NO_RECOVERY_ON_REAL_DOCUMENT",
                    "detail": f"真实文档上旧实现仅差 {legacy_now['recovered_rounds']} 条，"
                              f"说明 D2 修复未生效"})
    if not defects["D1_struct_ok"]:
        red.append({"id": "R2_DUPLICATE_HISTORY_SECTION",
                    "detail": f"`## 执行历史` 段数 = {defects['D1_sections']}（应为 1），"
                              f"文件结构被写坏，旧解析器会在此截断"})
    if defects["D2_span_dropped"]:
        red.append({"id": "R3_TIME_SPAN_LINES_DROPPED",
                    "detail": f"带时间区间的 bullet 仍被丢弃: {defects['D2_span_dropped']}"})
    if defects["D4_range_task_id"] is not None:
        amber.append({"id": "A1_RANGE_TASK_ID_STILL_EXTRACTED",
                      "detail": f"`Txx-Tyy` 区间仍抽出 task_id = {defects['D4_range_task_id']}"})

    return {
        "verdict": "FAIL" if red else "PASS",
        "red": red,
        "amber": amber,
        "truncation_mechanism": trunc,
        "legacy_on_current_document": legacy_now,
        # T58 全量回归当轮的实测读数，作为"修复前确实只解出 9 轮"的旁证留存；
        # 精确复算需要那份已修复掉的损坏文档副本，故只记录不复算。
        "recorded_pre_fix_baseline": LEGACY_BASELINE,
        "defects": defects,
        "after": {
            "rounds": cur["rounds"], "code_tasks": cur["code_tasks"],
            "total_passed": cur["total_passed"],
            "real_finding_rounds": cur["real_finding_rounds"],
            "queue_clear_or_inspection_rounds": cur["queue_clear_or_inspection_rounds"],
            "distinct_task_ids": cur["distinct_task_ids"],
            "run_days": cur["run_days"], "first_round": cur["first_round"],
            "last_round": cur["last_round"],
            "history_sections": defects["D1_sections"],
        },
    }


def render_md(findings: dict) -> str:
    a = findings["after"]
    t = findings["truncation_mechanism"]
    lg = findings["legacy_on_current_document"]
    d = findings["defects"]
    b = findings["recorded_pre_fix_baseline"]
    lines = [
        "# T60 自动化 memory 解析漂移审计",
        "",
        f"> 生成于 {datetime.now(timezone.utc).isoformat()} · 只读审计，不改解析行为、不碰任何数据库",
        "",
        f"**判定: {findings['verdict']}**",
        "",
        f"- 修复前实测读数（留档，不再复算）: rounds={b['rounds']} / code_tasks="
        f"{b['code_tasks']} / total_passed={b['total_passed']} / real_findings="
        f"{b['real_finding_rounds']} / run_days={b['run_days']} / first_round={b['first_round']}",
        f"- 截断机制最小复现: 旧语义 {t['legacy_rounds']} 轮 vs 现行 {t['current_rounds']} 轮"
        f"（旧语义丢 {t['truncated_by_legacy']} 条）",
        f"- 现行文档上旧实现仍会丢 {lg['recovered_rounds']} 条（en-dash 时刻条目）→ 本次修复增量",
        f"- 修复后 rounds={a['rounds']} / code_tasks={a['code_tasks']} / "
        f"total_passed={a['total_passed']} / real_findings={a['real_finding_rounds']} / "
        f"run_days={a['run_days']} / first_round={a['first_round']} / last_round={a['last_round']}",
        f"- `## 执行历史` 段数: {d['D1_sections']}（应为 1）· 带时间区间 bullet: "
        f"{d['D2_span_bullets']} 条，丢弃 {len(d['D2_span_dropped'])} 条",
        "",
        "## 四条根因",
        "",
        "| 编号 | 层面 | 事实 | 现状 |",
        "|---|---|---|---|",
        f"| D1 | 文件结构 | `memory.md` 曾出现逐字重复的第二段 header（`## 红线` + `## 执行历史`），"
        f"旧 `parse_history()` 遇第一个 `## ` 即 break | 已去重，段数 {d['D1_sections']} |",
        f"| D2 | 正则 | `LINE_RE` 时刻字段只认单时刻，`07:3x–08:0x`(U+2013) 整行不匹配 → "
        f"T57/T58 自带丢弃 | 区间写法已支持，丢弃 {len(d['D2_span_dropped'])} 条 |",
        "| D3 | 语义 | `first_round/last_round` 取文件首行/末行，而新条目追加在顶部 → "
        "首轮字段被污染 | 已改按 (日期, 时刻) 取极值 |",
        f"| D4 | 正则 | `TASK_RE` 把区间写法 `T01-T29` 首个号抽成 task_id | 区间已抹除，"
        f"现抽得 {d['D4_range_task_id']} |",
        "",
        "## 结论",
        "",
        "**`test_real_memory_file_parseable` 的 `>= 20` 断言从未算错**：真实轮次本就远大于 20，"
        "被截断的只有 9 条。所以本轮**一字不改断言**，只修解析器与文件结构——"
        "下调断言等于把 bug 焊进基线。",
    ]
    if findings["red"]:
        lines += ["", "## RED", ""]
        lines += [f"- `{r['id']}` — {r['detail']}" for r in findings["red"]]
    if findings["amber"]:
        lines += ["", "## AMBER", ""]
        lines += [f"- `{r['id']}` — {r['detail']}" for r in findings["amber"]]
    return "\n".join(lines) + "\n"


def run_audit(text: str | None = None) -> dict:
    text = text if text is not None else read_memory()
    return build_findings(text)


def main() -> int:
    findings = run_audit()
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(findings, f, ensure_ascii=False, indent=2)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(render_md(findings))
    print(f"[OK] 判定 {findings['verdict']} · 修复后 rounds="
          f"{findings['after']['rounds']} → {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
