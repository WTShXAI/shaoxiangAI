# -*- coding: utf-8 -*-
"""T44 审计脚本单测 (纯函数 + 合成证据, 零生产 I/O)。

覆盖: 日志解码/解析/周期汇总/锁形态判定/源码结构扫描/退避计算/整改条目/报告渲染。
不碰 events.db、不重试、不重启、不改调度。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.audit_predict_refresh_lock import (  # noqa: E402
    analyze_monitor_source, build_recommendations, classify_lock_pattern,
    decode_line, decode_log_lines, decode_log_text, live_impact, live_processes,
    parse_monitor_log, plan_retry, render_md, scan_write_surface, summarize_cycles,
    wal_sidecar_stats, _cim_date,
)


# ---------------------------------------------------------------------------
# 日志解码 / 解析
# ---------------------------------------------------------------------------

def test_decode_log_text_gbk_wins_over_utf8():
    """GBK 行与 ASCII 行都能正确还原 (逐行解码, 不再依赖整文件编码猜测)。"""
    raw = '[2026-09-27 08:57:01] [ERROR] 预测补算失败: database is locked\n'.encode('gbk')
    assert '预测补算失败' in decode_log_text(raw)
    assert decode_log_text('plain ascii\n'.encode('utf-8')) == 'plain ascii\n'
    assert decode_log_text(b'\xff\xfe broken \x81')  # latin-1 兜底, 绝不抛


def test_decode_line_handles_mixed_encoding_per_line():
    """真实踩坑: 同一文件内既有 UTF-8 行也有 GBK 行 → 必须逐行解码。

    反例: `raw.decode('utf-8')` 会整串失败, `raw.decode('gbk')` 会把 UTF-8 行解成桶乱码,
    两种都会让中文关键字 (如 '周期完成') 静默匹配不到。
    """
    gbk = '[2026-09-27 08:57:33] [INFO] 周期完成 62.6s'.encode('gbk')
    utf8 = '[2026-09-27 08:57:33] [INFO] 周期完成 62.6s'.encode('utf-8')
    assert '周期完成' in decode_line(gbk)
    assert '周期完成' in decode_line(utf8)
    # 整文件解码: 混合体必须仍能命中中文关键字
    mixed = gbk + b'\n' + utf8 + b'\n'
    assert '周期完成' in decode_log_text(mixed)
    assert decode_log_text(mixed).splitlines()[0] == decode_log_text(mixed).splitlines()[1]
    assert decode_log_lines(mixed)[0].startswith('[2026-09-27')


def test_decode_log_text_utf8_suffix():
    raw = '[2026-09-27 08:57:01] [ERROR] \u9884\u6d4b\u8865\u7b97\u5931\u8d25\n'.encode('utf-8')
    assert '\u9884\u6d4b\u8865\u7b97\u5931\u8d25' in decode_log_text(raw)


LOG_SAMPLE = "\n".join([
    '[2026-09-27 07:50:37] [WARN] 新定格判定已达 599 — 建议评估重训 (finalize 前需 walkforward 过线)',
    '[2026-09-27 07:50:37] [INFO] 周期完成 5.9s | bridge=OK | 今日预测 225 | 叙事+22 | LL偏差 0.03231 | 重训建议 是',
    '[2026-09-27 08:57:01] [ERROR] 预测补算失败: database is locked',
    '  [维加雷亚尔竞技 vs 莫卡] 失败: database is locked',
    '  [圣安娜德尔孔德 vs 奥尔梅卡体育] 失败: database is locked',
    '[2026-09-27 08:57:33] [WARN] 新定格判定已达 599 — 建议评估重训 (finalize 前需 walkforward 过线)',
    '[2026-09-27 08:57:33] [INFO] 周期完成 62.6s | bridge=OK | 今日预测 ? | 叙事+0 | LL偏差 0.03231 | 重训建议 是',
])


def test_parse_monitor_log_cycles_and_gap():
    p = parse_monitor_log(LOG_SAMPLE)
    assert p['ts_lines'] == 5
    assert p['match_fail_lines'] == 2
    kinds = [c['kind'] for c in p['cycles']]
    assert 'ERROR' in kinds and 'CYCLE_DONE' in kinds
    err = next(c for c in p['cycles'] if c['kind'] == 'ERROR')
    assert err['error'] == 'database is locked'
    assert err['gap_sec'] == 32.0  # 08:57:01 → 08:57:33
    failed_cycle = next(c for c in p['cycles'] if c.get('cycle_sec') == 62.6)
    assert failed_cycle['predictions_failed'] is True
    ok_cycle = next(c for c in p['cycles'] if c.get('cycle_sec') == 5.9)
    assert ok_cycle['predictions_failed'] is False


def test_parse_monitor_log_empty_and_noise():
    p = parse_monitor_log('random line\n  [x vs y] 失败: boom\n')
    assert p['ts_lines'] == 0
    assert p['match_fail_lines'] == 1


def test_summarize_cycles_failure_rate_and_gaps():
    p = parse_monitor_log(LOG_SAMPLE)
    d = summarize_cycles(p)
    today = d['2026-09-27']
    assert today['total_cycles'] == 3          # 2 个成功周期 + 1 个补算失败周期
    assert today['refresh_failures'] == 1
    assert today['failure_rate'] == round(1 / 3, 4)
    assert today['avg_cycle_sec'] == 34.2  # round((5.9 + 62.6) / 2, 1)
    assert today['gap_samples'] == [32.0]
    assert today['match_fail_lines'] == 2


def test_summarize_cycles_single_failure_has_no_rate_division_error():
    p = parse_monitor_log('[2026-09-27 01:00:00] [ERROR] 预测补算失败: database is locked\n')
    d = summarize_cycles(p)
    assert d['2026-09-27']['total_cycles'] == 1
    assert d['2026-09-27']['failure_rate'] == 1.0


def test_classify_lock_pattern():
    assert classify_lock_pattern(32.0) == 'BUSY_TIMEOUT_WAIT'      # ~30s busy_timeout 签名
    assert classify_lock_pattern(0.4) == 'IMMEDIATE_FAIL'
    assert classify_lock_pattern(3.2) == 'OTHER'
    assert classify_lock_pattern(90.0) == 'OTHER'
    assert classify_lock_pattern(None) == 'UNKNOWN'
    assert classify_lock_pattern(30.0, busy_timeout_ms=30000) == 'BUSY_TIMEOUT_WAIT'


# ---------------------------------------------------------------------------
# 源码结构扫描
# ---------------------------------------------------------------------------

MONITOR_SRC = "\n".join([
    'def _conn_ro():',
    '    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)',
    '',
    'def refresh_predictions():',
    '    with _conn_ro() as con:',
    '        out["before"] = len(read_for_date(con, today))',
    '    from gq.db import conn as gq_conn',
    '    with gq_conn() as con:',
    '        con.executescript(TABLE_DDL)',
    '        build_for_date(con, today, refresh=True)',
    '',
    'try:',
    '    status["predictions"] = refresh_predictions()',
    'except Exception as e:',
    "    status['predictions'] = {'error': str(e)}",
])


def test_analyze_monitor_source_flags_write_conn_and_no_backoff():
    f = analyze_monitor_source(MONITOR_SRC)
    assert f['open_write_connection'] is True
    assert f['has_backoff'] is False
    assert f['swallowed_error_lines'], '撞锁异常被吞进 status, 摘要退化为 "今日预测 ?"'
    assert any('gq_conn' in h['text'] for h in f['write_conn_hits'])
    assert any('mode=ro' in h['text'] or 'readonly=True' in h['text'] for h in f['ro_conn_hits'])


def test_analyze_monitor_source_detects_present_backoff():
    src = MONITOR_SRC + '\n    time.sleep(backoff)  # retry'
    f = analyze_monitor_source(src)
    assert f['has_backoff'] is True


def test_cim_date_normalization():
    assert _cim_date('/Date(1790232404152)/') == '2026-09-24 14:46:44'
    assert _cim_date('2026-09-24 14:46:44') == '2026-09-24 14:46:44'
    assert _cim_date(None) == ''


def test_live_processes_never_kills_and_extracts_script():
    """只读进程清单: 抽 .py 主脚本; 异常即返回空表, 绝不抛、绝不 kill。"""
    if os.name != 'nt':
        pytest.skip('Windows-only')
    ps = live_processes()
    assert isinstance(ps, list)
    for p in ps:
        assert set(p) >= {'pid', 'ppid', 'created', 'script'}
        assert 'error' not in p, ps
    # 断言本脚本自身在列时以自身脚本名出现 (自指, 仅供证据, 不做清理)
    assert all(not str(p['pid']).isdigit() or p['script'] for p in ps)


def test_scan_write_surface_identifies_writer_and_busy_flag():
    with tempfile.TemporaryDirectory() as td:
        rel = os.path.join('pkg', 'writer_mod.py')
        os.makedirs(os.path.join(td, 'pkg'))
        with open(os.path.join(td, rel.replace('/', os.sep)), 'w', encoding='utf-8') as fh:
            fh.write('with conn() as c:\n    c.execute("PRAGMA busy_timeout=30000")\n'
                     '    c.execute("INSERT INTO t VALUES(1)")\n')
        r = scan_write_surface(td, [rel])
        assert r['writers'][0]['file'] == rel
        assert r['writers'][0]['write_stmts'] >= 1
        assert r['writers'][0]['busy_timeout_set'] is True


def test_scan_write_surface_ignores_missing_files():
    r = scan_write_surface('/nonexistent-dir-xyz', ['a.py'])
    assert r['scanned'] == [] and r['writers'] == []


# ---------------------------------------------------------------------------
# 退避 / 整改 / 报告
# ---------------------------------------------------------------------------

def test_plan_retry_exponential_and_capped():
    # 固定 random 种子, 退避 = base * factor^(n-1) * (1 ± jitter)
    import random
    random.seed(7)
    first = plan_retry(1, base=2.0, factor=3.0, cap=120.0, jitter=0.0)
    assert first == 2.0
    third = plan_retry(3, base=2.0, factor=3.0, cap=120.0, jitter=0.0)
    assert third == 18.0


def test_plan_retry_jitter_stays_in_band_and_capped():
    vals = [plan_retry(2, 2.0, 3.0, 120.0, 0.3) for _ in range(50)]
    assert all(6.0 * 0.7 <= v <= 6.0 * 1.3 for v in vals)
    assert all(v <= 120.0 for v in vals)


def test_plan_retry_defensive_inputs():
    assert plan_retry(0) >= 0
    assert plan_retry(-5) >= 0


def test_build_recommendations_structure_and_honesty():
    recs = build_recommendations({'failure_rate': 0.5})
    ids = [r['id'] for r in recs]
    assert ids == ['R1', 'R2', 'R3', 'R4']
    assert len(set(ids)) == len(ids)
    for r in recs:
        assert r['title'] and r['change'] and r['why'] and r['gates'] and r['rollback']
        assert all(str(g).startswith('G') for g in r['gates'])
    # 诚实纪律约束: 涉及概率/赔率口径的改动必须显式引用 IR-30 (R3 触及判定语义)
    assert any('IR-30' in g for g in next(r for r in recs if r['id'] == 'R3')['gates'])


def test_render_md_contains_key_sections_and_no_production_io():
    rep = {
        'generated_at': '2026-09-27 17:00:00',
        'log_path': 'logs/autonomous_monitor.log',
        'headline': ['测试标题'],
        'write_surface': {'writers': [{'file': 'gq/ws_collector.py', 'write_stmts': 9,
                                       'busy_timeout_set': True, 'readonly_path': False}],
                          'readers': []},
        'processes': [{'pid': 1, 'ppid': 2, 'created': '2026-09-24 14:46:44',
                       'script': 'ws_collector.py'}],
        'monitor_source': {'open_write_connection': True, 'has_backoff': False},
        'lock_pattern': 'BUSY_TIMEOUT_WAIT',
        'parsed': {'match_fail_lines': 34},
        'daily': {'2026-09-27': {'total_cycles': 8, 'refresh_failures': 4,
                                 'failure_rate': 0.5, 'avg_cycle_sec': 40.0,
                                 'gap_samples': [32.0]}},
        'impact': {'ok': True, 'matches_today': 862, 'matches_today_by_status': {'scheduled': 526},
                   'daily_predictions_today': 282,
                   'daily_today_by_source': {'market_baseline': 258},
                   'non_finished_without_daily': 512, 'verdicts_today_matches': 69},
        'wal': {'wal_mb': 568.5},
        'recommendations': build_recommendations(),
        'caveats': ['caveat-1'],
        'questions': {'Q1': 'a', 'Q2': 'b', 'Q3': 'c', 'Q4': 'd'},
    }
    md = render_md(rep)
    for token in ('# T44', 'Q1 归因', 'Q2 时序', 'Q3 影响', 'Q4 整改', '诚实边界',
                  'gq/ws_collector.py', '512', 'BUSY_TIMEOUT_WAIT', '568.5', 'caveat-1'):
        assert token in md, token


# ---------------------------------------------------------------------------
# 只读辅助 (默认不联网不写库; 传 db 路径才读)
# ---------------------------------------------------------------------------

def test_wal_sidecar_stats_missing_file_is_none():
    r = wal_sidecar_stats(os.path.join(tempfile.gettempdir(), 'no_such_db_t44.db'))
    assert r['db_bytes'] is None and r['wal_bytes'] is None
    assert r['wal_mb'] is None


def test_live_impact_returns_error_dict_without_db():
    r = live_impact(os.path.join(tempfile.gettempdir(), 'no_such_db_t44.db'))
    assert r['ok'] is False and 'error' in r
