# -*- coding: utf-8 -*-
"""T62 守卫: 信号词(`NO_EDGE` 一族)持久化面 / 词表一致 / 冻结语义 的不变量。

纯只读: 不跑迁移、不写快照、不写 events.db、不碰生产服务、零进程操作。
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import audit_signal_vocab_migration as A  # noqa: E402


# --------------------------------------------------------------------------- 基础设施
def _res():
    return A.collect()


def test_collect_is_read_only():
    res = _res()
    assert res["read_only"] is True
    assert A.MODULE_BASENAME == "audit_signal_vocab_migration.py"


def test_self_excluded_from_literal_scan():
    hits = A.literal_sites()["hits"]
    assert A.MODULE_BASENAME not in hits, "审计脚本自己被字面量扫描命中, 自避失效"


def test_producer_vocab_has_seven_values():
    pv = A.producer_vocab()
    assert pv["n"] == 7, pv["values"]
    for v in ("NO_EDGE", "STRONG_BREAK", "STRONG_HOLD", "WEAK_TREND",
              "ALREADY_BROKEN", "SCORE_LAGGING", "SETTLED_UNDER"):
        assert v in pv["values"], v


def test_frontend_map_keys_are_the_badge_table():
    fm = A.frontend_map_keys()
    assert set(fm["map"]) == {"STRONG_BREAK", "STRONG_HOLD", "WEAK_TREND",
                              "ALREADY_BROKEN", "NO_EDGE"}, fm["map"]


def test_frontend_map_has_no_fallback_branch():
    """裸取键 + && 短路 = 未知键整段消失(比 T54 的灰色兜底更糟)。"""
    assert A.frontend_rename_consequence()["has_fallback_branch"] is False


def test_snapshot_values_subset_of_producer_vocab():
    """数据面只允许出现生产者词表内的值(否则是孤儿词, 前端也没徽章)。"""
    pv = set(A.producer_vocab()["values"])
    snap = set(A.snapshot_values()["values"])
    assert snap <= pv, sorted(snap - pv)


def test_ledger_rows_subset_of_producer_vocab():
    led = set((A.ledger_signal_counts()["dist"] or {}).keys())
    pv = set(A.producer_vocab()["values"])
    assert led <= pv, sorted(led - pv)


# --------------------------------------------------------------------------- 词表一致
def test_vocab_consistency_is_currently_fail():
    """三处词表互不一致 —— 守卫要能如实报红, 而不是恒绿。"""
    res = _res()
    d = res["q2_vocab_consistency"]["differences"]
    assert res["q2_vocab_consistency"]["verdict"] == "FAIL"
    assert "SCORE_LAGGING" in d["in_producer_not_frontend"]
    assert "SETTLED_UNDER" in d["in_data_not_frontend"]


def test_frontend_is_missing_three_producer_values():
    d = _res()["q2_vocab_consistency"]["differences"]
    assert set(d["in_producer_not_frontend"]) == {"SCORE_LAGGING", "SETTLED_UNDER"}
    assert d["in_frontend_not_producer"] == []


# --------------------------------------------------------------------------- 持久化面
def test_ledger_is_the_surface_t58_missed():
    led = A.ledger_signal_counts()
    assert led["n_rows"] > 40000, led
    assert "NO_EDGE" in led["dist"]
    br = _res()["q4_blast_radius"]
    assert br["counts"]["ledger_rows"] == led["n_rows"]


def test_events_db_column_is_written_side_not_read_side():
    """存量改写 = events.db 写入 → 必须被标记为 §4/WINDOW, 不得被当成普通数据迁移。"""
    br = _res()["q4_blast_radius"]
    assert br["events_db_write_flag"] is True
    assert "停机窗口" in br["events_db_write_note"]
    assert "prediction_ledger" in br["events_db_write_note"]


def test_blast_radius_counts_are_non_zero():
    c = _res()["q4_blast_radius"]["counts"]
    assert c["snapshot_records"] > 0
    assert c["python_literal_files"] > 0
    assert c["frontend_map_keys"] > 0


def test_five_persistence_surfaces():
    s = _res()["q1_surfaces"]
    assert set(s) == {"producer", "bridge_passthrough", "business_snapshot",
                      "events_db_column", "frontend_map"}


# --------------------------------------------------------------------------- 冻结语义
def test_freeze_applies_only_to_first_prefixed_fields():
    fr = _res()["q3_freeze_semantics"]
    assert fr["frozen_field_prefixes"] == ["first_score", "first_minute", "first_seen", "first_verdict"]
    assert "ou" in fr["live_keys_overwritten_by_v.update(now_v)"]
    assert fr["records_refreshed_after_freeze"] > 0, "没有刷新过的记录就无法证明 live 字段会被覆盖"


def test_first_verdict_ou_settlement_is_structurally_dead():
    """`first_verdict` 存扁平 `ou_direction`, `ou_win()` 读 `v['ou']['direction']` → first_OU 恒 None。"""
    fv = _res()["q3_freeze_semantics"]["first_verdict_settlement"]
    assert fv["first_verdict_ou_settlement_dead"] is True
    assert "ou" not in fv["first_verdict_keys"]
    assert fv["first_verdict_keys"] == ["x2", "cs_top1", "cs_top3", "ou_direction"]


def test_guard_fails_if_first_verdict_gains_an_ou_subdict():
    """反向用例: 若首见结构补上了 `ou` 子字典, 空转判定必须翻转(守卫不是恒真)。"""
    import ast
    src = A.read_text(A.WATCHER)
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Subscript) and getattr(t.slice, "value", None) == "first_verdict"
                        for t in n.targets))
    # 把扁平的 ou_direction 替换成 `ou` 子字典(consumers 期望的形状)
    node.value = ast.Dict(
        keys=[ast.Constant(value="x2"), ast.Constant(value="cs_top1"),
              ast.Constant(value="cs_top3"), ast.Constant(value="ou")],
        values=[ast.Constant(value="home"), ast.Constant(value="2-0"),
                ast.List(elts=[ast.Constant(value="2-0")], ctx=ast.Load()), node.value.values[-1]],
    )
    ast.fix_missing_locations(tree)
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(ast.unparse(tree))
        tmp = f.name
    try:
        real = A.WATCHER
        A.WATCHER = tmp
        try:
            assert A.first_verdict_settlement_is_dead()["first_verdict_ou_settlement_dead"] is False
        finally:
            A.WATCHER = real
    finally:
        os.unlink(tmp)


def test_first_sight_invariant_holds_today():
    inv = _res()["q3_freeze_semantics"]["invariant_first_sight_only"]
    assert inv["ok"] is True
    assert inv["issues"] == []
    assert {w["target"] for w in inv["first_sight_writes"]} == {
        "v['first_score']", "v['first_minute']", "v['first_seen']", "v['first_verdict']"}


def test_guard_fails_if_someone_moves_a_frozen_field_into_the_refresh_set():
    """fail-closed: 把 first_verdict 挪进 v.update 的刷新集 → 不变量必须 FAIL。"""
    import ast
    src = A.read_text(A.WATCHER)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "snapshot":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and "first_" in ast.unparse(stmt.targets[0]):
                    stmt.value = ast.Call(
                        func=ast.Attribute(value=ast.Name(id="v", ctx=ast.Load()), attr="update", ctx=ast.Load()),
                        args=[ast.Dict(keys=[ast.Constant(value="first_verdict")],
                                       values=[ast.Constant(value="x")])],
                        keywords=[])
        if isinstance(node, ast.FunctionDef) and node.name == "snapshot":
            node.body.append(ast.Assign(
                targets=[ast.Subscript(value=ast.Name(id="v", ctx=ast.Store()),
                                       slice=ast.Constant(value="first_score"), ctx=ast.Store())],
                value=ast.Constant(value="0-0")))
    ast.fix_missing_locations(tree)
    import tempfile
    code = ast.unparse(tree)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp = f.name
    try:
        real = A.WATCHER
        A.WATCHER = tmp
        try:
            inv = A.frozen_fields_are_first_sight_only()
            assert inv["ok"] is False, "守卫失效: 冻结字段被挪进刷新集却仍报绿"
            assert inv["issues"], "必须给出具体行号级理由"
        finally:
            A.WATCHER = real
    finally:
        os.unlink(tmp)


# --------------------------------------------------------------------------- 迁移面
def test_rename_consequence_is_silent_disappearance():
    fx = _res()["q5_rename_consequence"]
    assert fx["map_lookup_guarded"] is True
    assert fx["has_fallback_branch"] is False
    assert "消失" in fx["consequence"]
    assert fx["snapshot_carrying_no_edge"] > 0


def test_probe_cache_ttl_is_capped_and_named():
    pc = _res()["q5_rename_consequence"]["probe_cache"]
    assert isinstance(pc["ttl_sec"], (int, float)) and pc["ttl_sec"] <= 60


def test_watcher_opens_events_db_readwrite_violation_is_reported():
    hy = _res()["q6_hygiene"]
    assert hy["opens_readwrite"] is True
    assert "mode=ro" not in (hy["connect_call"] or "")


def test_report_files_written_and_parseable():
    assert os.path.exists(A.OUT_JSON) and os.path.exists(A.OUT_MD)
    with open(A.OUT_JSON, encoding="utf-8") as f:
        json.load(f)
    with open(A.OUT_MD, encoding="utf-8") as f:
        assert "T62" in f.read()


@pytest.mark.parametrize("key", ["q1_surfaces", "q2_vocab_consistency", "q3_freeze_semantics",
                                 "q4_blast_radius", "q5_rename_consequence", "q6_hygiene"])
def test_all_sections_present(key):
    assert key in _res()


def test_module_is_not_git_ignored():
    import subprocess
    r = subprocess.run(["git", "check-ignore", "-q", A.__file__],
                       cwd=REPO, capture_output=True)
    assert r.returncode != 0, "审计脚本被 .gitignore 吃掉(与 T61 同族的静默失效)"
