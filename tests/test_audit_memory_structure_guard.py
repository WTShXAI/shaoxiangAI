#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T65 自动化 memory 结构不变式守卫的 fail-closed 回归测试。

T60 修掉了「重复 header 把执行历史截断成 9 条」，但没有任何守卫防止下一次复制粘贴再犯
（截断是静默的：看板变小、断言要到下次全量回归才红）。本文件把 T60 的四条根因
D1 文件结构 / D2 正则丢行 / D3 首末轮取行序 固化成可执行的六条不变式 S1-S6。

覆盖:
  G1 真实 memory 必须通过全部不变式（结构回归的第一个哨兵）
  G2 重复 `## ` 标题必须被 R1 抓住（D1 的 verbatim 重复形态）
  G3 重复 header 造成的多历史段必须被 R2 抓住（D1 的真实事故形态）
  G4 历史段外的 `- <date>` bullet 必须被 R3 抓住（截断/丢行的指纹）
  G5 带区间时刻的 bullet 不得被判为未解析（D2 反证：修复后覆盖率必须满）
  G6 首末轮必须取 (date, 时刻) 极值而非文件行序（D3 反证，合成文档）
  G7 缺时刻的 bullet 必须被 S5 抓住
  G8 重复条目只报 AMBER 不判 FAIL（刻意的 non-fail 决策，避免噪声变硬失败）
  G9 报告必须渲染出全部六条不变式，且审计只读不改 memory

纯只读: 不碰 events.db / 不跑验证台 / 不写 verification.db / 零进程操作；
临时文件只在 tmp_path 内。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import audit_memory_structure_guard as G  # noqa: E402
from scripts import snapshot_automation_health as H  # noqa: E402


# --- 合成样本 --------------------------------------------------------------- #

def _doc(body: str) -> str:
    return ("# 自动化 dbda4380\n\n"
            "调度: rrule\n\n## 红线（不可破）\n\nIR-30 诚实\n\n"
            "## 执行历史（仅高层，详情见每日 memory）\n\n" + body)


GOOD_BODY = (
    "- 2026-09-28 07:3x–08:0x **T57(样例, 587 passed)**\n"
    "- 2026-09-25 12:3x T01(样例, 4 passed)\n"
    "- 2026-09-26 19:53 巡检(队列 T01-T29 全清空转)\n"
)


def findings_for(text: str) -> dict:
    return G.build_findings(text)


# --- G1 真实文档 ------------------------------------------------------------ #

def test_real_memory_passes_all_invariants():
    """真实 memory.md 必须结构完好；任何一条被破坏都说明本守卫已失去意义。"""
    f = findings_for(G.read_memory())
    assert f["verdict"] == "PASS", f"真实 memory 结构已损坏: {f['red']}"
    assert f["red"] == []
    assert f["invariants"]["S2_history_section_single"]["count"] == 1


def test_audit_does_not_mutate_memory():
    """只读审计：build_findings 不得改写 memory（含 mtime）。"""
    path = G.MEMORY_PATH
    before = (os.stat(path).st_mtime_ns, open(path, "rb").read())
    findings_for(G.read_memory())
    after = (os.stat(path).st_mtime_ns, open(path, "rb").read())
    assert before == after


# --- G2 重复标题（D1 的逐字重复形态） -------------------------------------- #

def test_duplicate_heading_is_caught():
    """T60 D1 的 verbatim 重复形态：同一标题一字不差地出现两次。"""
    text = _doc(GOOD_BODY).replace(
        "## 红线（不可破）\n",
        "## 红线（不可破）\n\nIR-30 诚实\n\n## 红线（不可破）\n", 1)
    f = findings_for(text)
    assert "R1_DUPLICATE_HEADING" in [r["id"] for r in f["red"]]
    assert f["invariants"]["S1_heading_unique"]["ok"] is False
    # 现行 parse_history 跨段累积，所以轮次仍全收 —— 证明「看板变小」来自结构而非解析
    assert f["invariants"]["S3_bullet_coverage"]["parsed_entries"] == 3


def test_near_duplicate_heading_is_not_a_false_positive():
    """`## 执行历史（仅高层…）` 与 `## 执行历史` 逐字不同 → S1 不抓（只由 S2 抓段数）。"""
    text = _doc(GOOD_BODY) + "\n## 执行历史\n\n- 2026-09-25 13:47 T03(样例, 4 passed)\n"
    f = findings_for(text)
    assert "R1_DUPLICATE_HEADING" not in [r["id"] for r in f["red"]]
    assert "R2_MULTIPLE_HISTORY_SECTIONS" in [r["id"] for r in f["red"]]


# --- G3 重复 header 造成多历史段（D1 真实事故形态） ------------------------ #

def test_duplicate_history_section_is_caught():
    text = (
        "# 自动化 dbda4380\n\n调度: rrule\n\n## 红线（不可破）\n\nIR-30 诚实\n\n"
        "## 执行历史（仅高层，详情见每日 memory）\n\n- 2026-09-28 07:3x–08:0x T57(x.py, 587 passed)\n\n"
        "## 执行历史\n\n- 2026-09-25 12:3x T01(x.py, 4 passed)\n"
    )
    f = findings_for(text)
    assert "R2_MULTIPLE_HISTORY_SECTIONS" in [r["id"] for r in f["red"]]
    assert f["invariants"]["S2_history_section_single"]["count"] == 2


# --- G4 bullet 覆盖（截断/丢行指纹） -------------------------------------- #

def test_bullet_outside_history_section_is_caught():
    """落在历史段**之前**的 date bullet 解析不出来 —— 正是重复 header 截断后的形态。"""
    text = _doc(GOOD_BODY).replace(
        "## 执行历史（仅高层，详情见每日 memory）\n\n",
        "- 2026-09-27 01:5x **T00(游离 bullet, 1 passed)**\n\n"
        "## 执行历史（仅高层，详情见每日 memory）\n\n", 1)
    f = findings_for(text)
    cov = f["invariants"]["S3_bullet_coverage"]
    assert "R3_STRANDED_BULLET" in [r["id"] for r in f["red"]]
    assert len(cov["stranded"]) == 1
    assert cov["stranded"][0]["raw"].startswith("- 2026-09-27")
    assert cov["date_bullets"] == 4 and cov["parsed_entries"] == 3
    # 段外游离 bullet 行级本身能解析 —— 只有显式算「段内/段外」才抓得到
    assert G.history_line_numbers(text) != {ln - 1 for ln in range(1, 30)}


def test_section_outside_is_the_only_hard_part_of_coverage():
    """段内漏收只报 AMBER：现行解析器有意忽略「无 T 号且非巡检」的 bullet。"""
    text = _doc(GOOD_BODY + "- 2026-09-28 09:0x 这段只是说明文本，不是运行条目\n")
    f = findings_for(text)
    cov = f["invariants"]["S3_bullet_coverage"]
    assert cov["dropped_in_section"] and not cov["stranded"]
    assert f["verdict"] == "PASS"
    assert "A2_DROPPED_BULLET_IN_SECTION" in [r["id"] for r in f["amber"]]


def test_parsed_entries_never_exceed_date_bullets():
    """条目数不可能超过 date bullet 数；超过即说明解析口径变了（fail-closed）。"""
    cov = findings_for(G.read_memory())["invariants"]["S3_bullet_coverage"]
    assert cov["parsed_entries"] <= cov["date_bullets"]


# --- G5 区间时刻 bullet（D2 反证） ---------------------------------------- #

def test_timespan_bullet_counts_as_covered():
    """T57 那类 `07:3x–08:0x` 整行必须被收下；否则覆盖率会假降。"""
    text = _doc("- 2026-09-28 07:3x–08:0x **T57(样例, 587 passed)**\n")
    cov = findings_for(text)["invariants"]["S3_bullet_coverage"]
    assert cov["date_bullets"] == 1 and cov["parsed_entries"] == 1
    assert cov["ok"] is True


# --- G6 首末轮语义（D3 反证，合成文档：最新条目在文件首行） ---------------- #

DUPLICATE_CHECK_DOC = _doc(
    "- 2026-09-28 10:5x **T61(样例, 641 passed)**\n"
    "- 2026-09-25 12:3x T01(样例, 4 passed)\n"
)


def test_first_round_uses_extremes_not_file_order():
    entries = H.parse_history(DUPLICATE_CHECK_DOC)
    assert entries, "合成文档必须能解析出条目"
    # 文件首行是最新条目 —— 「取行序」会把它当 first_round（T60 D3 的原始形态）
    assert entries[0]["date"] == "2026-09-28"
    ex = G.extreme_rounds(entries)
    assert ex["first"] == "2026-09-25 12:3x"
    assert ex["last"] == "2026-09-28 10:5x"
    assert ex["first"] != f"{entries[0]['date']} {entries[0]['time']}"


def test_check_extremes_semantics_flags_file_order_tie():
    """文件首行恰好等于极值时无法区分两种语义 —— 守卫必须如实标注，不许假装通过。"""
    text = _doc("- 2026-09-25 12:3x T01(样例, 4 passed)\n")
    r = G.check_extremes_semantics(text)
    assert r["distinguishable_from_file_order"] is False
    assert r["ok"] is False


def test_guard_reports_extremes_regression_on_real_document():
    real = G.check_extremes_semantics(G.read_memory())
    assert real["ok"] is True, "真实文档上现行语义必须已经取极值（否则 D3 已回归）"
    assert real["matches_summarize"] is True


# --- G7 缺时刻 bullet ------------------------------------------------------ #

def test_bullet_without_time_is_caught():
    text = _doc("- 2026-09-28 巡检(没有时刻字段)\n")
    s5 = findings_for(text)["invariants"]["S5_field_complete"]
    assert s5["ok"] is False
    assert len(s5["bullets_without_time"]) == 1
    assert "R5_MALFORMED_ENTRY" in [r["id"] for r in findings_for(text)["red"]]


# --- G8 重复条目只报 AMBER ------------------------------------------------ #

def test_duplicate_entry_is_amber_not_failure():
    """同一轮被写两次是噪声不是结构损坏；判 FAIL 会把噪声变成硬失败。"""
    text = _doc(GOOD_BODY + "- 2026-09-25 12:5x T02(样例, 4 passed)\n"
                           "- 2026-09-25 12:5x T02(样例, 4 passed)\n")
    f = findings_for(text)
    s6 = f["invariants"]["S6_duplicate_noise"]
    assert s6["duplicate_keys"] >= 1
    assert f["verdict"] == "PASS"
    assert any(r["id"] == "A1_DUPLICATE_ENTRY_KEYS" for r in f["amber"])


# --- G9 报告渲染与常量 ---------------------------------------------------- #

def test_report_renders_all_six_invariants():
    md = G.render_md(findings_for(G.read_memory()))
    for label in ("S1", "S2", "S3", "S4", "S5", "S6"):
        assert label in md, f"报告漏掉不变式 {label}"


def test_report_paths_are_own_reports_only():
    """审计只读 memory，只写自有 reports。"""
    assert G.MEMORY_PATH.endswith("memory.md")
    assert os.path.join(".workbuddy", "memory") in G.MEMORY_PATH
    assert G.MEMORY_PATH.startswith(G.REPO_ROOT)
    assert G.OUT_JSON.startswith(os.path.join(G.REPO_ROOT, "reports"))
    assert G.OUT_MD.startswith(os.path.join(G.REPO_ROOT, "reports"))


@pytest.mark.parametrize("text", ["", "## only heading\n", "- not a datum bullet\n"])
def test_degenerate_documents_do_not_crash(text):
    f = findings_for(text)
    assert isinstance(f["verdict"], str)
    assert isinstance(f["invariants"], dict)
