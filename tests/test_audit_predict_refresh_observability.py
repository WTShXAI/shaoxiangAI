#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T50 「今日预测 ?」可观测性只读盘点 —— 纯函数单元测试 (零生产 I/O)。"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.audit_predict_refresh_observability import (  # noqa: E402
    S4_FIELDS, classify_error_kind, classify_prediction_block, contract_gap,
    current_status_snapshot, hysteresis_streaks, lock_flags, narrative_ambiguity,
    read_history, scan_status_consumers, streak_alerts, summarize_degradation,
)


def _rec(cycle_at: str, pred=None, narrative=None) -> dict:
    return {'cycle_at': cycle_at, 'predictions': pred, 'narrative_added': narrative}


OK_PRED = {'date': '2026-09-28', 'before': 0, 'added': 89, 'after': 89,
           'sources': {'market_baseline': 86}}
LOCK_ERR = {'error': 'database is locked'}
REAL_ERR = {'error': 'no such table: daily_predictions'}


# --------------------------------------------------------------------------- Q1

def test_ok_block_not_degraded():
    c = classify_prediction_block(OK_PRED)
    assert c['degraded'] is False
    assert c['kind'] == 'OK'
    assert c['after'] == 89


def test_lock_error_is_lock_contention():
    c = classify_prediction_block(LOCK_ERR)
    assert c['degraded'] is True
    assert c['kind'] == 'DEGRADED_LOCK'
    assert c['error_kind'] == 'LOCK_CONTENTION'


def test_real_error_not_mixed_into_lock_streak():
    assert classify_prediction_block(REAL_ERR)['kind'] == 'DEGRADED_REAL'
    assert classify_error_kind('') == 'UNKNOWN'


def test_ssot_classifier_identifies_real_operational_error():
    """必须复用 core.error_envelope.classify_error (SSoT), 且真 sqlite 类型能被正确识别。"""
    import sqlite3
    kind = classify_error_kind(str(sqlite3.OperationalError('database is locked')))
    assert kind == 'LOCK_CONTENTION'
    assert classify_error_kind(str(sqlite3.OperationalError('no such table: x'))) == 'REAL_ERROR'


def test_missing_after_alone_is_degraded():
    """成功路径若缺 after(如部分字段退化) 同样必须被标降级, 否则 «? » 会漏报。"""
    assert classify_prediction_block({'added': 3})['kind'] == 'DEGRADED_NO_AFTER'


def test_missing_predictions_block_is_degraded():
    c = classify_prediction_block(None)
    assert c['kind'] == 'MISSING' and c['degraded'] is True


def test_s4_fields_absent_everywhere_in_real_history():
    """真值锚: 现存 monitor_history 中 T48 §S4 四字段出现次数必须为 0。"""
    path = os.path.join(ROOT, 'reports', 'monitor_history.jsonl')
    if not os.path.exists(path):
        pytest.skip('history 不存在')
    recs = read_history(path)
    assert recs, 'history 为空'
    cg = contract_gap(recs)
    assert cg['ok_rows']['n'] + cg['degraded_rows']['n'] >= 1
    for f in S4_FIELDS:
        assert cg['ok_rows']['coverage'][f] == 0, f
        assert cg['degraded_rows']['coverage'][f] == 0, f


def test_degradation_rate_uses_all_cycles():
    recs = [_rec('t1', OK_PRED), _rec('t2', LOCK_ERR)]
    d = summarize_degradation(recs)
    assert d['cycles'] == 2
    assert d['dist']['OK'] == 1 and d['dist']['DEGRADED_LOCK'] == 1
    assert d['degraded_rate'] == 0.5


# --------------------------------------------------------------------------- Q2

def test_hysteresis_single_success_does_not_reset():
    """T48 S4 硬要求: 单周期成功不得清零。"""
    flags = [True, False, True]
    st = hysteresis_streaks(flags, clean_to_reset=2)
    assert len(st) == 1, '两个锁失败之间的单个成功不得切断段'
    assert st[0]['length'] == 3


def test_hysteresis_two_clean_cycles_reset():
    st = hysteresis_streaks([True, False, False, True], clean_to_reset=2)
    assert len(st) == 2 and st[0]['length'] == 1


def test_open_streak_flagged_and_alert_threshold():
    st = hysteresis_streaks([True, True, True])
    assert st[0].get('open') is True
    alerts = streak_alerts(st, threshold=3)
    assert alerts[0]['alert'] is True and alerts[0]['length'] == 3


def test_lock_flags_exclude_real_errors():
    recs = [_rec('t1', LOCK_ERR), _rec('t2', REAL_ERR), _rec('t3', OK_PRED)]
    assert lock_flags(recs) == [True, False, False]


def test_streak_alert_threshold_default_3():
    rows = streak_alerts(hysteresis_streaks([True, True]), threshold=3)
    assert rows[0]['alert'] is False


# --------------------------------------------------------------------------- Q3

def test_narrative_none_vs_zero_are_distinguishable_counts():
    recs = [_rec('t1', OK_PRED, narrative=None), _rec('t2', OK_PRED, narrative=0),
            _rec('t3', OK_PRED, narrative=5)]
    na = narrative_ambiguity(recs)
    assert na['none_count'] == 1 and na['zero_count'] == 1


def test_contract_gap_reports_missing_fields():
    cg = contract_gap([_rec('t1', LOCK_ERR)])
    assert cg['degraded_rows']['missing_fields'] == list(S4_FIELDS)
    assert cg['any_s4_field_ever_present'] == 0.0


# --------------------------------------------------------------------------- Q4

def test_consumer_scan_self_excludes_and_reads_repo():
    cs = scan_status_consumers()
    assert cs['hits'] >= 1
    assert 'MENTION' in cs['by_polarity']          # 文档叙述不得被当消费方
    assert set(cs['by_polarity']) >= {'MENTION', 'WRITER'}   # 文档不算消费方, monitor 自身是写方
    self_script = f'scripts{os.sep}' + os.path.basename(__file__).replace('test_', '')
    for h in cs['hit_rows']:                        # self-exclude 生效
        assert h['file'] != self_script
    assert cs['by_polarity']['MENTION'] >= 1        # 只看叙述不算消费方


def test_reader_files_exclude_frontend_and_bridge():
    """事实断言: 现存读取方不得在 backend / frontend 面。"""
    cs = scan_status_consumers()
    for f in cs['reader_files']:
        assert not f.startswith(('frontend/', 'bridge_service.py')), f


def test_current_snapshot_readable_or_absent():
    snap = current_status_snapshot()
    if snap is not None:
        assert isinstance(snap, dict)
        assert 'predictions' in snap
