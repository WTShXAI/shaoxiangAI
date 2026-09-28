#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T51 验证台报告「时效 + 三态不变式」守卫测试。

覆盖四族:
  T1 三态枚举   verdict 只允许 {EDGE, NO EDGE, INCONCLUSIVE}; 越界必须报错
  T2 IR-30 字段 EDGE 必须可独立复核 (数据面 + 静态渲染面双检)
  T3 时效分级   FRESH / STALE_WARN / STALE_HARD / UNKNOWN 边界与阈值语义
  T4 噪声面     全仓三态字面量扫描须排除本脚本, 且验证台包外噪声被计量不误判

纯只读: 不跑验证台 / 不写 verification.db / 碰事件库为零 / 不碰生产服务。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import verdict_guard_ssot as VG  # noqa: E402  (T57 判定词表/Tier SSoT)
from scripts.audit_verification_report_freshness import (  # noqa: E402
    ALLOWED_VERDICTS, FRESH_HARD_HOURS, FRESH_WARN_HOURS, IR30_EDGE_REQUIRED,
    RE_VERDICT_LITERAL, RE_VERDICT_QUOTED, ROOT, build_findings, check_edge_fields,
    check_renderer_edge_fields, check_verdict_enum, classify_freshness,
    classify_literal_file, iter_python_files, live_regression, load_report, parse_ts,
    read_text, report_age_hours, run_audit, scan_consumers, scan_render_fallback,
    scan_verdict_literal_sources, self_exclude, write_report,
)

RE_TOKEN = VG.RE_VERDICT_TOKEN
RE_QUOTED = VG.RE_VERDICT_QUOTED

#: 干净基线用的最小字面量扫描结果 (无未分层产出体)。
_LIT_OK = {"unexpected_count": 0, "unexpected": []}


# --- T1 三态枚举 ------------------------------------------------------------

def test_verdict_enum_accepts_only_three_states():
    assert set(ALLOWED_VERDICTS) == {"EDGE", "NO EDGE", "INCONCLUSIVE"}


def test_check_verdict_enum_accepts_valid_report():
    data = {"models": {"A": {"verdict": "NO EDGE"}, "B": {"verdict": "INCONCLUSIVE"},
                       "C": {"verdict": "EDGE"}}}
    assert check_verdict_enum(data) == []


def test_check_verdict_enum_flags_no_edge_style_drift():
    """把 NO_EDGE 误写进报告 = 三态语义被污染, 必须被发现。"""
    data = {"models": {"A": {"verdict": "NO_EDGE"}}}
    out = check_verdict_enum(data)
    assert len(out) == 1 and out[0]["issue"] == "VERDICT_NOT_IN_ENUM"
    assert out[0]["model"] == "A"


def test_check_verdict_enum_flags_missing_and_non_dict_models():
    data = {"models": {"A": {}, "B": "oops"}}
    issues = sorted(v["issue"] for v in check_verdict_enum(data))
    assert issues == ["NOT_A_DICT", "VERDICT_NOT_IN_ENUM"]


# --- T2 IR-30 可复核字段 ----------------------------------------------------

def test_edge_rows_without_proof_fields_are_gaps():
    data = {"models": {"A": {"verdict": "EDGE", "roi": 0.05}}}
    gaps = check_edge_fields(data)
    assert gaps and gaps[0]["model"] == "A"
    assert set(gaps[0]["missing"]) == set(IR30_EDGE_REQUIRED)


def test_edge_row_with_all_proof_fields_passes():
    row = {"verdict": "EDGE"}
    row.update({f: 0.01 for f in IR30_EDGE_REQUIRED})
    assert check_edge_fields({"models": {"A": row}}) == []


def test_non_edge_rows_are_not_checked_for_proof_fields():
    data = {"models": {"A": {"verdict": "NO EDGE"},
                       "B": {"verdict": "INCONCLUSIVE", "matches": 181}}}
    assert check_edge_fields(data) == []


def test_renderer_static_gap_is_measurable():
    """静态面才是今天真正可强制的一层: 返回缺失字段名 (可为空=达标)。"""
    missing = check_renderer_edge_fields()
    assert isinstance(missing, list)
    assert all(isinstance(x, str) for x in missing)
    # 本轮实测基线: render_json 不产出 G6 三元组, 该缺项是被审计出来的事实。
    assert "paired_excess_ci_low" in missing


def test_renderer_gap_shows_up_as_red_finding():
    missing = check_renderer_edge_fields()
    f = build_findings([], [], missing, "FRESH",
                       {"file": "x.py", "allowed_mapped": 3, "expected_mapped": 3,
                        "fail_open": False}, [], None)
    assert f["verdict"] == "FAIL" and "R2_RENDERER_CANNOT_PROVE_EDGE" in f["red"]


# --- T3 时效分级 ------------------------------------------------------------

def test_freshness_thresholds_are_ordered():
    assert 0 < FRESH_WARN_HOURS < FRESH_HARD_HOURS


def test_freshness_boundaries():
    assert classify_freshness(None) == "UNKNOWN"
    assert classify_freshness(FRESH_WARN_HOURS) == "FRESH"          # 边界内
    assert classify_freshness(FRESH_WARN_HOURS + 1) == "STALE_WARN"
    assert classify_freshness(FRESH_HARD_HOURS + 1) == "STALE_HARD"


def test_report_age_is_measured_from_generated_at():
    now = parse_ts("2026-09-28T04:26:34Z")
    age = report_age_hours({"generated_at": "2026-09-25T04:26:34Z"}, now)
    assert age == 72.0
    assert classify_freshness(age) == "STALE_WARN"


def test_parse_ts_handles_both_utc_forms_and_rejects_garbage():
    assert parse_ts("2026-09-25T04:26:34Z") is not None
    assert parse_ts("2026-09-25T04:26:34.990116+00:00") is not None
    assert parse_ts("not-a-time") is None
    assert parse_ts(None) is None


def test_stale_report_is_amber_not_red():
    f = build_findings([], [], [], "STALE_WARN",
                       {"file": "x.py", "allowed_mapped": 3, "expected_mapped": 3,
                        "fail_open": False}, [], 72.0, _LIT_OK)
    assert f["verdict"] == "PASS"
    assert f["amber"] == ["R3_REPORT_EXPIRED"]


def test_undatable_report_is_red():
    """没有时间戳的报告 = 读者无法判断新旧, 必须 RED 而非跳过。"""
    f = build_findings([], [], [], "UNKNOWN",
                       {"file": "x.py", "allowed_mapped": 3, "expected_mapped": 3,
                        "fail_open": False}, [], None, _LIT_OK)
    assert f["verdict"] == "FAIL"
    assert "R3_REPORT_UNDATABLE" in f["red"]


# --- T4 噪声面与 fail-open 渲染 --------------------------------------------

def test_verdict_literal_scan_skips_self_and_its_tests():
    """自避必须含测试文件 —— 本轮实测只排脚本会让本测试自己变成 10 条假命中。"""
    hits = scan_verdict_literal_sources()
    all_hits = (hits["emitters_inside_package"] + hits["render_consumers"]
                + hits["noise_known"] + hits["doc_meta"] + hits["unexpected"])
    assert all(h["file"] != "scripts/audit_verification_report_freshness.py" for h in all_hits)
    assert all(h["file"] != "tests/test_audit_verification_report_freshness.py" for h in all_hits)


def test_layered_taxonomy_puts_known_noise_in_noise_bucket():
    """analysis/ 里的 NO_EDGE 是盘口信号语义, 须进 NOISE_KNOWN 而不是 UNEXPECTED。"""
    hits = scan_verdict_literal_sources()
    assert hits["noise_known"], "analysis/ 已知噪声面量化为 0 -> 扫描面失效"
    assert hits["emitters_inside_package"], "verification/ 包内未扫到任何三态字面量"


def test_unexpected_emitter_baseline_is_zero_with_reason_gated_registry():
    """放宽词表 + 共享带理由登记册后的基线 (T57): **零**未登记产出体。

    算术: 三个真绕过体落 ``EXPECTED_TIER``, ``pipeline/fusion_wdl_proto.py``(阈值字典键)
    落 ``KNOWN_FALSE_POSITIVE``, 其余审计/叙述面落 ``DOC_META``; 于是 UNEXPECTED 为空。
    **零基线只在登记册仍然「每条都有理由」时成立** —— 空理由一旦被塞进登记册,
    ``test_registry_reasons_cannot_be_empty`` 会先红, 防止把零基线当免死金牌。
    """
    hits = scan_verdict_literal_sources()
    files = sorted({h["file"] for h in hits["unexpected"]})
    assert files == []
    # 三个真绕过体必须被放宽词表捞到并显式登记 —— 旧词表(两侧带引号)捞不到后两个。
    exp = {(h["file"], h["line"]) for h in hits_expected(hits)}
    assert ("scripts/mh_train_div.py", 67) in exp
    assert ("scripts/mh_train_walkforward.py", 118) in exp
    assert ("scripts/mh_train_walkforward_x.py", 82) in exp
    # 已知假阳性必须显式落在 FALSE_POSITIVE 桶里, 而不是靠「没命中」蒙混过关。
    fp = {h["file"] for h in hits.get(VG.TIER_FALSE_POSITIVE, [])
          + hits.get("known_false_positive", [])}
    assert "pipeline/fusion_wdl_proto.py" in fp


def hits_expected(hits: dict) -> list:
    """Tier 为 EXPECTED_TIER 的全部命中（分层扫描结果里按 bucket 取）。"""
    out = list(hits.get(VG.TIER_EXPECTED, []))
    out += list(hits.get("expected", []))
    return out


def test_relaxed_token_table_is_a_superset_of_the_quoted_table():
    """放宽词表只准多捞, 不准漏捞 (旧词表的命中必须仍被新词表命中)。"""
    samples = [
        "print('VERDICT ... -> NO EDGE')",          # 带引号
        'print("VERDICT ... -> NO EDGE")',          # 双引号
        "print('VERDICT: ' + d + ' -> NO EDGE')",   # 行尾无引号 (旧词表漏检)
        "verdict = 'BEATS MARKET'",
        "if d < 0.001: return 'TIE'",
    ]
    for s in samples:
        assert RE_QUOTED.search(s) is not None or RE_TOKEN.search(s) is not None, s
    assert RE_TOKEN.search(samples[2]), "行尾无引号写法必须被捞到"
    assert RE_TOKEN.search(samples[3]) and RE_TOKEN.search(samples[4])
    # 词内下划线不算命中: NO_OVEREDGE 既非三态也非盘口信号, 不该被判定词表咬到。
    assert RE_TOKEN.search("x = 'NO_OVEREDGE'") is None
    assert RE_QUOTED.search("x = 'NO_OVEREDGE'") is None


def test_registry_reasons_cannot_be_empty():
    """豁免/登记必须写理由 —— 空理由不许登记(防「每次自动豁免」退化成无守卫)。"""
    assert VG.registry_reasons_complete() == {}


def test_ssot_module_is_tracked_by_git():
    """踩过的坑: ``.gitignore`` 的 ``_*.py`` 会把共享 SSoT 模块整文件忽略。

    本模块最初叫 ``scripts/_verdict_guard.py`` —— 被 ``.gitignore:142`` 的 ``_*.py``
    吃掉, `git status` 里连 `??` 都不显示。SSoT 一旦不在版本控制里, 克隆/重建环境后
    **两个守卫同时 import 失败**, 而本地看不出任何异常。故断言「SSoT 必须能被 git 索引」。
    """
    rel = "scripts/verdict_guard_ssot.py"
    path = os.path.join(ROOT, rel)
    assert os.path.exists(path), "SSoT 模块缺失 → 两个守卫的判定词表同时失效"
    try:
        # 退出码 0 = 该文件被 .gitignore 命中; 1 = 未被忽略。
        proc = subprocess.run(["git", "check-ignore", "-q", rel],
                              cwd=ROOT, capture_output=True, text=True)
    except OSError:
        pytest.skip("git 不可用")
    assert proc.returncode != 0, (
        f"{rel} 被 .gitignore 命中 → 重建环境(克隆)后两个守卫同时 import 失败, "
        f"而本地工作区看不出异常"
    )
    # 真正的根因守卫: check-ignore 对【未跟踪】文件也返回「未忽略」(rc=1) → 假绿。
    # 必须再断言文件已进 git 索引, 否则克隆/重建后该 SSoT 直接丢失。
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert tracked.returncode == 0, (
        f"{rel} 未纳入 git 索引(未跟踪) → 克隆/重建后两个守卫同时 import 失败, 而本地无异常"
    )


def test_both_guards_share_one_wordlist_and_one_tier_table():
    """T57 的合流点: 两个守卫不得各自自带判定词正则/Tier 判定。"""
    for path in ("scripts/audit_verification_report_freshness.py",
                 "scripts/audit_mh_train_div_bypass.py"):
        src = read_text(os.path.join(ROOT, path)) or ""
        for i, line in enumerate(src.splitlines(), 1):
            if "re.compile" in line and any(w in line for w in
                                            ("NO_EDGE", "NO EDGE", "INCONCLUSIVE",
                                             "BEATS MARKET", "TIE")):
                pytest.fail(f"{path}:{i} 仍自带判定词正则, 词表已有 SSoT")
    assert VG.classify_verdict_file("scripts/mh_train_div.py") == VG.TIER_EXPECTED
    assert VG.classify_verdict_file("scripts/backtest_ou_signal.py") == VG.TIER_NOISE


def test_consumers_are_listed_but_almost_none_check_freshness():
    """引用报告的 10+ 处里, 除一处自述外没有任何做时效判定 —— 旧结论被当最新。"""
    cons = scan_consumers()
    assert cons
    checked = [c for c in cons if c["checks_freshness"]]
    assert len(checked) <= 1, f"消费点已开始判时效者过多, 基线需更新: {checked}"


def test_classify_literal_file_tiers():
    assert classify_literal_file("verification/report.py") == "EMITTER_OK"
    assert classify_literal_file("scripts/build_dashboard.py") == "RENDER_CONSUMER"
    assert classify_literal_file("analysis/live_goal_probe.py") == "NOISE_KNOWN"
    assert classify_literal_file("tests/test_x.py") == "DOC_META"
    assert classify_literal_file("scripts/audit_some_thing.py") == "DOC_META"
    assert classify_literal_file("pipeline/brand_new.py") == "UNEXPECTED"


def test_unexpected_emitter_is_red():
    f = build_findings([], [], [], "FRESH",
                       {"file": "x.py", "allowed_mapped": 3, "expected_mapped": 3,
                        "fail_open": False}, [], None,
                       {"unexpected_count": 2, "unexpected": []})
    assert f["verdict"] == "FAIL"
    assert "R6_UNKNOWN_VERDICT_EMITTER" in f["red"]


def test_render_fallback_is_detected_as_fail_open_in_dashboard():
    r = scan_render_fallback()
    assert r["found"] is True
    assert r["allowed_mapped"] == r["expected_mapped"]
    assert r["fallback_is_neutral"] is True


def test_consumer_scan_ignores_verification_package_and_self():
    cons = scan_consumers()
    assert all(not c["file"].startswith("verification/") for c in cons)
    assert all(c["file"] not in self_exclude() for c in cons)


def test_build_findings_is_fail_closed_on_any_red_or_missing():
    ok = {"file": "x.py", "allowed_mapped": 3, "expected_mapped": 3, "fail_open": False}
    assert build_findings([], [], [], "FRESH", ok, [], None, _LIT_OK)["verdict"] == "PASS"
    assert build_findings([{"model": "A", "verdict": "NO_EDGE"}], [], [], "FRESH", ok,
                          [], None, _LIT_OK)["verdict"] == "FAIL"
    assert build_findings([], [{"model": "A", "missing": ["win_rate"]}], [], "FRESH", ok,
                          [], None, _LIT_OK)["verdict"] == "FAIL"
    assert build_findings([], [], [], "FRESH", ok, [], None, None)["verdict"] == "FAIL"
    assert build_findings([], [], [], "FRESH", ok)["verdict"] == "FAIL"  # 未做字面量扫描


# --- 活体回归 ---------------------------------------------------------------

def test_live_report_is_parseable_and_enum_clean():
    live = live_regression()
    if not live["exists"]:
        pytest.skip("reports/verification_report.json 不存在, 跳过活体回归")
    assert live["verdict"] == "OK", f"三态越界: {live['enum_violations']}"
    assert live["models"]


def test_live_report_expiry_is_reported_not_hidden():
    live = live_regression()
    if not live["exists"]:
        pytest.skip("报告不存在, 跳过时效活体回归")
    assert live["freshness"] in ("FRESH", "STALE_WARN", "STALE_HARD")
    f = run_audit()
    assert f["freshness"] == live["freshness"]


def test_write_report_emits_markdown_and_json(tmp_path):
    ok = {"file": "x.py", "allowed_mapped": 3, "expected_mapped": 3, "fail_open": False}
    f = build_findings([], [], [], "FRESH", ok, [], None, _LIT_OK)
    paths = write_report(f, str(tmp_path / "a.json"), str(tmp_path / "a.md"))
    md = open(paths["md"], encoding="utf-8").read()
    assert "PASS" in md and "时效" in md
    assert os.path.exists(paths["json"])


def test_repo_root_is_architecture():
    assert os.path.basename(ROOT) == "Architecture" or ROOT.endswith("Architecture")


def test_iter_python_files_excludes_vendor_dirs():
    rels = [_rel(p) for p in iter_python_files()]
    assert all(not r.startswith((".venv", "node_modules", "archive")) for r in rels)


def _rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")
