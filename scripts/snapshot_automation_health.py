"""T26 — 自动化运行连续性快照（只读）。

汇总本自动化 dbda4380 历次执行，输出 reports/automation_health.json。
来源 = 自动化 memory.md 执行历史段落（纯文本解析，零生产 I/O，
不打开 events.db / 不碰服务 / 不写任何库）。

解析目标（memory.md 历史行格式示例）:
  - 2026-09-25 12:3x 建队列(14项) + 示范 T01(p_audit_migrate.py, 4 passed)
  - 2026-09-25 12:5x T02(P-SNAPSHOT lineup 规格, 纯docs)
  - 2026-09-25 16:06 T04(audit_bridge_endpoint_usage.py, 3 passed, ...)
  - 2026-09-26 10:44 巡检(队列空转): ...

提取: 轮次日期/时间 / 任务号 Txx / 脚本名 / passed 数 / 是否含真实发现 /
是否队列清空或巡检。

用法:
  python scripts/snapshot_automation_health.py            # 默认读自动化 memory → reports/automation_health.json
  python scripts/snapshot_automation_health.py --memory X.md --out Y.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MEMORY = os.path.join(
    REPO_ROOT, ".workbuddy", "memory", "automations",
    "dbda4380-1bc4-41b5-9e04-1117d1ba56b9", "memory.md",
)
DEFAULT_OUT = os.path.join(REPO_ROOT, "reports", "automation_health.json")

# 历史行: "- 2026-09-25 12:3x 描述"
# 时刻字段须容忍时间区间写法（"07:3x–08:0x" / "09:1x - 09:3x" / "07:3x—08:0x"）：
# 旧版只认单时刻，凡带区间的整行直接不匹配 → 最新一轮被静默丢弃（T60 根因 D2）。
_TIME_FIELD = r"\d{1,2}:\d{1,2}[xX]?(?:\s*[-–—~]\s*\d{1,2}:\d{1,2}[xX]?)?"
LINE_RE = re.compile(r"^\s*-\s*(\d{4}-\d{2}-\d{2})\s+(" + _TIME_FIELD + r")\s+(.*)$")
# 区间写法 "T01-T49" 不得被当成任务号；先抹掉再找任务号。
TASK_RANGE_RE = re.compile(r"T\d{2,3}\s*-\s*T\d{2,3}")
TASK_RE = re.compile(r"(?<![-\w])T(\d{2,3})(?:-[A-Za-z0-9]{1,3})?(?![\w.-])")
PASSED_RE = re.compile(r"(\d+)\s*passed")
SCRIPT_RE = re.compile(r"([A-Za-z_][\w\-]*\.py)")
REALFIND_RE = re.compile(r"真实发现|真实缺口|关键发现|⚠")
QUEUECLEAR_RE = re.compile(r"队列清空|队列空转|巡检")
HISTORY_HEADING = "## 执行历史"


def parse_history_line(line: str) -> dict | None:
    """解析单条历史 bullet，非历史行返回 None。"""
    m = LINE_RE.match(line)
    if not m:
        return None
    date_s, time_s, body = m.group(1), m.group(2), m.group(3)
    # 仅当行内含任务号或巡检语义才视为执行历史（排除纯章节标题/红线说明行）
    if not (TASK_RE.search(body) or QUEUECLEAR_RE.search(body)):
        return None
    task = TASK_RE.search(TASK_RANGE_RE.sub(" ", body))
    passed = PASSED_RE.search(body)
    script = SCRIPT_RE.search(body)
    return {
        "date": date_s,
        "time": time_s,
        "task_id": f"T{task.group(1)}" if task else None,
        "script": script.group(1) if script else None,
        "passed": int(passed.group(1)) if passed else None,
        "is_code_task": passed is not None,
        "has_real_finding": bool(REALFIND_RE.search(body)),
        "is_queue_clear_or_inspection": bool(QUEUECLEAR_RE.search(body)),
        "body": body.strip(),
    }


def count_history_sections(text: str) -> int:
    """文件里 `## 执行历史` 标题的个数。

    应为 1。>1 说明文件结构被写坏（重复 header / 段落被切断），
    T60 实证：第二段 header 曾让旧解析器在真正的主序列之前就 break。
    """
    return sum(1 for ln in text.splitlines() if ln.strip().startswith(HISTORY_HEADING))


def parse_history(text: str) -> list[dict]:
    """解析整个 memory.md 文本，返回历史条目列表（按出现顺序）。

    段语义（T60 修复）：遇 `## 执行历史` 打开，遇**其他** `## ` 关闭；
    再次遇到 `## 执行历史` 重新打开 → **跨多个同名单段累积**，不再在第一段结尾 break
    （旧版 break 会把"顶部追加块 + 主序列"截成前一段）。
    """
    out: list[dict] = []
    in_history = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(HISTORY_HEADING):
            in_history = True
            continue
        if in_history and s.startswith("## "):
            in_history = False
            continue
        if not in_history:
            continue
        entry = parse_history_line(ln)
        if entry:
            out.append(entry)
    return out


def _time_key(t: str) -> tuple[int, int]:
    """把 "7:3x–08:0x" / "12:3x" 折成可比较的 (时, 分)。"""
    m = re.match(r"(\d{1,2}):(\d{1,2})", t or "")
    return (int(m.group(1)), int(m.group(2))) if m else (99, 99)


def summarize(entries: list[dict]) -> dict:
    code_tasks = [e for e in entries if e["is_code_task"]]
    total_passed = sum(e["passed"] for e in code_tasks if e["passed"] is not None)
    real_findings = [e for e in entries if e["has_real_finding"]]
    queue_events = [e for e in entries if e["is_queue_clear_or_inspection"]]
    task_ids = [e["task_id"] for e in entries if e["task_id"]]
    # 首/末轮按 (日期, 时刻) 取极值，不是取文件首行/末行：
    # 新条目是**追加到文件顶部**的，取文件顺序会让 first_round 指到新条目（T60 根因 D3）。
    ordered = sorted(entries, key=lambda e: (e["date"], _time_key(e["time"])))
    last = ordered[-1] if ordered else None
    first = ordered[0] if ordered else None

    # 连续运行天数（去重日期）
    run_days = sorted({e["date"] for e in entries})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "automation memory.md execution history",
        "history_sections": None,  # 由 main() 回填（真实文件段数，供结构告警）
        "totals": {
            "rounds": len(entries),
            "code_tasks": len(code_tasks),
            "docs_or_eval_tasks": len(entries) - len(code_tasks),
            "total_passed": total_passed,
            "real_finding_rounds": len(real_findings),
            "queue_clear_or_inspection_rounds": len(queue_events),
            "distinct_task_ids": len(set(task_ids)),
            "task_id_list": task_ids,
            "run_days": len(run_days),
            "run_day_list": run_days,
            "first_round": (f"{first['date']} {first['time']}" if first else None),
            "last_round": (f"{last['date']} {last['time']}" if last else None),
        },
        "by_day": _by_day(entries),
        "entries": entries,
    }


def _by_day(entries: list[dict]) -> list[dict]:
    days: dict[str, dict] = {}
    for e in entries:
        d = days.setdefault(e["date"], {
            "date": e["date"], "rounds": 0, "code_tasks": 0,
            "passed": 0, "real_findings": 0, "queue_events": 0,
        })
        d["rounds"] += 1
        if e["is_code_task"]:
            d["code_tasks"] += 1
        if e["passed"]:
            d["passed"] += e["passed"]
        if e["has_real_finding"]:
            d["real_findings"] += 1
        if e["is_queue_clear_or_inspection"]:
            d["queue_events"] += 1
    return [days[k] for k in sorted(days)]


def main() -> int:
    ap = argparse.ArgumentParser(description="自动化运行连续性快照（只读）")
    ap.add_argument("--memory", default=DEFAULT_MEMORY, help="自动化 memory.md 路径")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON 路径")
    args = ap.parse_args()

    if not os.path.exists(args.memory):
        raise SystemExit(f"[NOGO] memory 不存在: {args.memory}")
    with open(args.memory, encoding="utf-8") as f:
        text = f.read()

    entries = parse_history(text)
    report = summarize(entries)
    sections = count_history_sections(text)
    report["history_sections"] = sections

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    t = report["totals"]
    print(f"[OK] 解析 {t['rounds']} 轮 / 代码任务 {t['code_tasks']} / "
          f"累计 passed {t['total_passed']} / 真实发现 {t['real_finding_rounds']} / "
          f"队列清空或巡检 {t['queue_clear_or_inspection_rounds']} / 运行天数 {t['run_days']}")
    if sections != 1:
        print(f"[WARN] 执行历史段数 = {sections}（应为 1）：memory.md 结构疑似损坏，"
              f"解析器已跨段累积，但请检查重复 header")
    print(f"[OK] 输出 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
