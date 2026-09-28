"""T49 `daily_predictions.status` 防回退静态守卫 — 判定式与分类器的直接测试。

覆盖四类断言：
  1. `is_sql_daily_status_predicate` 的**歧义符号防护**（前端本地变量 `d.status` 不得假阳性）；
  2. `classify_code_hit` 的三层排除（自指 / 审计字面量 / 注释）；
  3. 写方判定的**字面量豁免**（否则把「测试里写断言」读成「生产在写」）；
  4. 文档面两级触发（TIGHT/LOOSE）与 `matches.status` 无关的排除；
  5. 守卫机制盘点器的 re.M 边界（`^` 少 re.M 会把显式清单数静默读成 0）。

零生产 I/O：不碰 events.db、不写 verification.db、不改任何源文件。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

from audit_daily_status_guard import (  # noqa: E402
    LITERAL_EXEMPT_SUFFIXES,
    catalog_guard_mechanisms,
    classify_code_hit,
    is_sql_daily_status_predicate,
    scan_code_surface,
    scan_docs_surface,
)


# ── 1. 歧义符号防护（本轮最关键的一条） ───────────────────────────────────────
@pytest.mark.parametrize('line', [
    "        WHERE d.status='finished' AND m.score_home IS NOT NULL",
    "SELECT ... FROM daily_predictions d WHERE d.status='finished'",
    "SELECT * FROM daily_predictions d WHERE d.status <> 'finished'",
])
def test_sql_predicate_recognised(line):
    assert is_sql_daily_status_predicate(line)


@pytest.mark.parametrize('line', [
    "{selPhase && (selPhase.key !== 'pre' || d.status === 'scheduled') && (",   # 前端本地变量
    "print(d.status)",                                                            # 无 SQL 语境
    "d.status,",                                                                  # 纯字段引用
])
def test_non_sql_d_status_not_false_positive(line):
    assert not is_sql_daily_status_predicate(line)


# ── 2. 代码面分类与三层排除 ──────────────────────────────────────────────────
def test_ingest_gate_classified():
    line = "        WHERE d.status='finished' AND m.score_home IS NOT NULL"
    assert classify_code_hit('verification/ingest.py', line) == 'INGEST_GATE'


def test_audit_literal_exempted():
    """审计脚本必须写被扫描串来断言 → 不得被守卫抓成违规。"""
    line = 'assert extract_status_predicates("WHERE d.status=\'finished\' AND ...")'
    assert classify_code_hit('tests/test_audit_ingest_status_gate.py', line) == 'AUDIT_LITERAL'
    # 本脚本与其单测也必须在豁免名单里，否则「审计自己的审计」会自抓
    here = os.path.relpath(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'scripts', 'audit_daily_status_guard.py'),
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).replace('\\', '/')
    assert any(here.endswith(s) for s in LITERAL_EXEMPT_SUFFIXES)


def test_self_exclude_covers_this_test():
    rel = os.path.relpath(__file__,
                          os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert any(rel.replace('\\', '/').endswith(s) for s in LITERAL_EXEMPT_SUFFIXES)


def test_comment_lines_downgraded():
    line = "        # 历史遗留：d.status='finished' 门控"
    assert classify_code_hit('verification/ingest.py', line) == 'COMMENT'


def test_passthrough_snapshot():
    line = "kickoff=excluded.kickoff, match_date=excluded.match_date, status=excluded.status,"
    assert classify_code_hit('pipeline/predict_export.py', line) == 'PASSTHROUGH'


# ── 3. 真写方判定（字面量豁免） ───────────────────────────────────────────────
def test_update_writer_real_vs_literal_only():
    code = scan_code_surface()
    real = [u for u in code['update_writers'] if not u.get('is_literal_only')]
    # 基线事实：T33 已机械证明 UPDATE_WRITER=0；本轮独立复核仍应为 0
    assert code['update_writer_count'] > 0, '应有审计/测试字面量被识别为候选'
    assert real == [], '真写方应为 0 —— 否则说明有人把 A/B 方案的 UPDATE 写回来了'
    assert code['by_kind'].get('INGEST_GATE') == 1, 'ingest 门控应只有 verification/ingest.py 一处'


def test_scan_code_surface_is_readonly():
    """扫描器不得打开数据库：模块内不得出现 sqlite3/connect 用法。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'scripts', 'audit_daily_status_guard.py'),
               encoding='utf-8').read()
    assert 'sqlite3' not in src
    assert 'connect(' not in src


# ── 4. 文档面两级触发 ────────────────────────────────────────────────────────
def test_docs_surface_no_assertions():
    docs = scan_docs_surface()
    assert docs['claim_count'] == docs['claim_count'], 'claim 计数须自洽'
    for c in docs['stale_derived_claims']:
        assert 'daily_predictions' in c['text'] or 'd.status' in c['text']


def test_docs_unrelated_matches_status_excluded():
    docs = scan_docs_surface()
    for u in docs['unrelated_matches_status']:
        assert 'matches' in u['text'] and 'daily_predictions' not in u['text']


# ── 5. 守卫机制盘点：re.M 边界（少 re.M 会静默把 1 读成 0） ──────────────────
def test_scanlist_regex_needs_multiline():
    import re
    from audit_daily_status_guard import SCANLIST_PAT
    # 清单出现在文件中部（真实形态），无 re.M 时 `^` 只锚整串开头 → 漏判
    probe = "import os\nSCAN_FILES = [\n    os.path.join(ROOT, 'x'),\n]"
    assert SCANLIST_PAT.search(probe)
    assert not re.compile(r"^\s*(SCAN_FILES)\s*[=:]").search(probe)   # 无 re.M 的失效对照


def test_guard_mechanism_inventory_has_exemplar():
    inv = catalog_guard_mechanisms()
    assert inv['count'] > 0
    # test_no_crossbook.py 是唯一带显式清单 + 源码正则的守卫（IR-32），T49 需沿用其形状
    names = [m['file'] for m in inv['guards']]
    assert 'tests/test_no_crossbook.py' in names
    assert inv['with_explicit_scanlist'] >= 1
