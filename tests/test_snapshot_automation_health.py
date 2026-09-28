"""T26 — snapshot_automation_health 解析 + 实跑断言（零生产 I/O）。"""
import importlib.util
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "snapshot_automation_health.py")
MEMORY = os.path.join(REPO, ".workbuddy", "memory", "automations",
                      "dbda4380-1bc4-41b5-9e04-1117d1ba56b9", "memory.md")
OUT = os.path.join(REPO, "reports", "automation_health.json")

_spec = importlib.util.spec_from_file_location("snapshot_automation_health_t26", SCRIPT)
MOD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MOD)

SAMPLE = """
# 自动化 memory
## 执行历史
- 2026-09-25 12:3x 建队列(14项) + 示范 T01(p_audit_migrate.py, 4 passed)
- 2026-09-25 16:06 T04(audit_bridge_endpoint_usage.py, 3 passed, 真实发现 x)
- 2026-09-26 10:44 巡检(队列空转): 验证台三态全 NO EDGE
- 2026-09-26 11:46 T24(audit_invalidated_report_links.py, 11 passed)
## 红线
- 不可破: events.db 零写入
"""


def test_parse_history_line_code():
    e = MOD.parse_history_line("- 2026-09-25 16:06 T04(audit_x.py, 3 passed, 真实发现)")
    assert e is not None
    assert e["date"] == "2026-09-25"
    assert e["time"] == "16:06"
    assert e["task_id"] == "T04"
    assert e["script"] == "audit_x.py"
    assert e["passed"] == 3
    assert e["is_code_task"] is True
    assert e["has_real_finding"] is True
    assert e["is_queue_clear_or_inspection"] is False


def test_parse_history_line_inspection():
    e = MOD.parse_history_line("- 2026-09-26 10:44 巡检(队列空转): 验证台三态")
    assert e is not None
    assert e["task_id"] is None
    assert e["is_code_task"] is False
    assert e["is_queue_clear_or_inspection"] is True


def test_parse_history_line_ignores_non_history():
    assert MOD.parse_history_line("- 不可破: events.db 零写入") is None
    assert MOD.parse_history_line("## 执行历史") is None


def test_parse_history_scoped_to_history_section():
    entries = MOD.parse_history(SAMPLE)
    assert all("events.db 零写入" not in e["body"] for e in entries)
    assert len(entries) == 4
    assert entries[0]["task_id"] == "T01"
    assert entries[-1]["task_id"] == "T24"


def test_summarize():
    entries = MOD.parse_history(SAMPLE)
    s = MOD.summarize(entries)
    t = s["totals"]
    assert t["rounds"] == 4
    assert t["code_tasks"] == 3
    assert t["total_passed"] == 18  # 4+3+11
    assert t["real_finding_rounds"] == 1
    assert t["queue_clear_or_inspection_rounds"] == 1
    assert t["run_days"] == 2
    assert t["first_round"] == "2026-09-25 12:3x"
    assert t["last_round"] == "2026-09-26 11:46"
    assert len(s["by_day"]) == 2


def test_real_memory_file_parseable():
    if not os.path.exists(MEMORY):
        import pytest
        pytest.skip("memory.md 不存在（离线环境）")
    with open(MEMORY, encoding="utf-8") as f:
        text = f.read()
    entries = MOD.parse_history(text)
    assert len(entries) >= 20, "历史轮次应 >= 20"
    s = MOD.summarize(entries)
    assert s["totals"]["code_tasks"] > 0
    assert s["totals"]["total_passed"] > 0


def test_main_writes_output(tmp_path):
    import sys
    from unittest import mock

    sample = tmp_path / "sample_mem.md"
    sample.write_text(SAMPLE, encoding="utf-8")
    out = tmp_path / "automation_health.json"
    saved = sys.argv
    try:
        with mock.patch.object(sys, "argv", ["x", "--memory", str(sample), "--out", str(out)]):
            assert MOD.main() == 0
    finally:
        sys.argv = saved
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["totals"]["rounds"] == 4
    assert data["history_sections"] == 1


# ---------------------------------------------------------------------------
# T60 fail-closed 守卫（解析漂移 / 静默丢弃）
# ---------------------------------------------------------------------------

def _real_memory_text() -> str:
    import pytest
    if not os.path.exists(MEMORY):
        pytest.skip("memory.md 不存在（离线环境）")
    with open(MEMORY, encoding="utf-8") as f:
        return f.read()


def test_real_memory_has_exactly_one_history_section():
    """`## 执行历史` 段只能有 1 个：重复 header 会把主序列切断（T60 根因 D1）。"""
    assert MOD.count_history_sections(_real_memory_text()) == 1


def test_parse_history_accumulates_all_history_sections():
    """多个同名单段必须全部累积，不能在第一段结尾 break。"""
    text = (
        "# t\n"
        "## 执行历史\n"
        "- 2026-09-25 12:3x T01(a.py, 4 passed)\n"
        "## 附录\n非历史一行\n"
        "## 执行历史\n"
        "- 2026-09-26 12:3x T02(b.py, 5 passed)\n"
    )
    entries = MOD.parse_history(text)
    assert [e["task_id"] for e in entries] == ["T01", "T02"]


def test_old_parser_behaviour_is_guarded_out():
    """反证：被"第一段结尾 break"截断的样例必须拿不全条目（防守卫自己退化）。"""
    text = (
        "## 执行历史\n"
        "- 2026-09-25 12:3x T01(a.py, 4 passed)\n"
        "## 红线（不可破）\nIR-30 诚实\n"
        "## 执行历史\n"
        "- 2026-09-26 12:3x T02(b.py, 5 passed)\n"
    )
    # 段语义：第二个同名单段重新打开，两个都收
    assert len(MOD.parse_history(text)) == 2
    # 而"遇到第一个 `## ` 就停"的旧语义只能拿到 1 条
    broken, in_hist = [], False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(MOD.HISTORY_HEADING):
            in_hist = True
            continue
        if in_hist and s.startswith("## "):
            break
        if in_hist:
            e = MOD.parse_history_line(ln)
            if e:
                broken.append(e)
    assert len(broken) == 1


def test_line_re_accepts_time_spans():
    """时刻字段须容忍区间写法（en dash / em dash / 连字符 / 波浪号）。"""
    for sep in ("-", "–", "—", "~"):
        e = MOD.parse_history_line(f"- 2026-09-28 07:3x{sep}08:0x **T57(x.py, 3 passed)")
        assert e is not None, sep
        assert e["date"] == "2026-09-28"
        assert e["time"].startswith("07:3x")
        assert e["task_id"] == "T57"


def test_task_id_not_extracted_from_range():
    """区间写法 `T01-T29` 不得抽出 T01（否则巡检轮次被记成任务 T01）。"""
    e = MOD.parse_history_line("- 2026-09-26 19:53 巡检(队列T01-T29全清空转): 验证台三态")
    assert e is not None
    assert e["task_id"] is None


def test_first_last_round_are_date_extremes_not_file_order():
    """首/末轮按日期取极值；新条目追加在文件顶部时不得污染 first_round。"""
    text = (
        "## 执行历史\n"
        "- 2026-09-28 09:1x T99(z.py, 2 passed)\n"
        "- 2026-09-25 12:3x T01(a.py, 4 passed)\n"
    )
    t = MOD.summarize(MOD.parse_history(text))["totals"]
    assert t["first_round"] == "2026-09-25 12:3x"
    assert t["last_round"] == "2026-09-28 09:1x"


def test_real_memory_last_entry_is_parsed():
    """最新一轮必须被计入（防"文件末尾那条被静默丢弃"）。"""
    text = _real_memory_text()
    last_bullet = [l for l in text.splitlines() if l.strip().startswith("- ")][-1]
    m = MOD.LINE_RE.match(last_bullet)
    assert m is not None, "文件末尾那条 bullet 连时间字段都解析不出来"
    expected_body = m.group(3).strip()
    bodies = [e["body"] for e in MOD.parse_history(text)]
    assert expected_body in bodies, "文件末尾那条（最新一轮）未被计入"


def test_real_memory_has_no_duplicate_entries():
    entries = MOD.parse_history(_real_memory_text())
    keys = [(e["date"], e["time"], e["body"][:40]) for e in entries]
    assert len(keys) == len(set(keys)), "同一轮被重复计入"
