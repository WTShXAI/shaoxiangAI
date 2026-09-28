"""T60 — audit_memory_parse_drift 守卫（零生产 I/O，只读 memory.md 文本）。"""
import importlib.util
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "audit_memory_parse_drift.py")

_spec = importlib.util.spec_from_file_location("audit_memory_parse_drift_t60", SCRIPT)
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)

MEMORY = os.path.join(REPO, ".workbuddy", "memory", "automations",
                      "dbda4380-1bc4-41b5-9e04-1117d1ba56b9", "memory.md")


@pytest.fixture(scope="module")
def real_text() -> str:
    if not os.path.exists(MEMORY):
        pytest.skip("memory.md 不存在（离线环境）")
    with open(MEMORY, encoding="utf-8") as f:
        return f.read()


def test_truncation_mechanism_still_reproducible():
    """重复 header 切段 → 旧语义在这些条目前截断，现行语义全收。"""
    t = A.check_truncation_mechanism()
    assert t["legacy_rounds"] == 2
    assert t["current_rounds"] == 5
    assert t["truncated_by_legacy"] == 3


def test_legacy_loses_time_span_entries_on_real_document(real_text):
    """现行文档上旧实现仍会丢 en-dash 时刻条目 → 这条差值就是本次修复的收益。"""
    got = A.check_legacy_on_current_document(real_text)
    assert got["recovered_rounds"] >= 2
    assert got["current_rounds"] == A.HEALTH.summarize(
        A.HEALTH.parse_history(real_text))["totals"]["rounds"]


def test_real_document_has_single_history_section(real_text):
    assert A.HEALTH.count_history_sections(real_text) == 1


def test_no_time_span_bullet_is_dropped(real_text):
    assert A.check_defects(real_text)["D2_span_dropped"] == []


def test_range_task_id_no_longer_extracted():
    """`队列T01-T29全清空转` 不得被抽成任务 T01。"""
    e = A.HEALTH.parse_history_line("- 2026-09-26 19:53 巡检(队列T01-T29全清空转): 验证台三态")
    assert e is not None
    assert e["task_id"] is None


def test_first_round_is_date_extreme_not_file_head(real_text):
    after = A.build_findings(real_text)["after"]
    assert after["first_round"].startswith("2026-09-25")
    assert after["last_round"].startswith("2026-09-28")


def test_audit_verdict_passes_on_repaired_document(real_text):
    assert A.build_findings(real_text)["verdict"] == "PASS"
    assert A.build_findings(real_text)["red"] == []


def test_pre_fix_baseline_record_is_not_edited():
    """留档的修复前读数不得被顺手改掉（它是"9 轮是错的"的唯一物证）。"""
    import inspect
    src = inspect.getsource(A)
    assert "rounds\": 9" in src.replace("'", '"')
    assert A.LEGACY_BASELINE["rounds"] == 9


def test_render_md_states_the_assertion_was_not_weakened(real_text):
    md = A.render_md(A.build_findings(real_text))
    assert ">= 20" in md
    assert "一字不改断言" in md
