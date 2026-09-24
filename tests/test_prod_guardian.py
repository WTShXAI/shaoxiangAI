# -*- coding: utf-8 -*-
"""prod_guardian 回归测试 (2026-09-24)。

锁定两个**曾经真实踩过**的 bug, 防止被改回去:
  1. token 失效判定必须"时间感知" —— 早期只看日志尾部是否含标记, 换新 token 后旧标记
     仍在尾部 → 已恢复的系统被永久误判失效、采集器永不拉起。
  2. 换号检测必须对 gq/.env 整体做指纹 —— 早期只监听 GQ_REQUEST_ID, 而"只有 sessionId
     变、token 不变"的换号场景只改 GQ_H5_URL → 检测不到变化、不会拉起。
另含存活识别的基本性质: 只拉起、不杀进程; 不存在的目标必须判为不存活。
"""
import os
import sys
import time
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import prod_guardian as g  # noqa: E402


def _write(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def test_token_dead_fresh_marker_is_dead(tmp_path, monkeypatch):
    """刚出现的 0401013 标记 → 判定为当前失效。"""
    log = tmp_path / 'ws_daemon.log'
    ts = (datetime.now() - timedelta(seconds=60)).strftime('%Y-%m-%d %H:%M:%S')
    _write(log, f'[{ts}] [NAV] H5 已加载\n[WARN] 比赛列表 code={g.TOKEN_DEAD_MARK}\n')
    monkeypatch.setattr(g, 'WS_LOG', str(log))
    assert g.token_dead_from_log() is True


def test_token_dead_stale_marker_is_not_dead(tmp_path, monkeypatch):
    """陈旧标记 (>MARK_FRESH_SEC) 视为历史残留 → 不再判定失效。

    这是 bug 1 的回归点: 换新 token 后日志尾部仍留着旧标记, 不能因此永久停机。
    """
    log = tmp_path / 'ws_daemon.log'
    ts = (datetime.now() - timedelta(seconds=g.MARK_FRESH_SEC + 600)).strftime('%Y-%m-%d %H:%M:%S')
    _write(log, f'[{ts}] [NAV] H5 已加载\n[WARN] 比赛列表 code={g.TOKEN_DEAD_MARK}\n')
    monkeypatch.setattr(g, 'WS_LOG', str(log))
    assert g.token_dead_from_log() is False


def test_token_dead_no_marker(tmp_path, monkeypatch):
    log = tmp_path / 'ws_daemon.log'
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _write(log, f'[{ts}] [REG] 刷新完成: 列表 60 场\n')
    monkeypatch.setattr(g, 'WS_LOG', str(log))
    assert g.token_dead_from_log() is False


def test_token_dead_recovery_after_success(tmp_path, monkeypatch):
    """bug 3 回归点 (2026-09-24): 换号后采集器修好连上新 token, 写出成功刷新行晚于
    最后一次 0401013 标记 → 必须判为已恢复 (False), 即便失效标记仍在 MARK_FRESH_SEC 内。
    否则守护会卡在失效态、永不拉起, 形成自愈死结。"""
    log = tmp_path / 'ws_daemon.log'
    fail_ts = (datetime.now() - timedelta(seconds=120)).strftime('%Y-%m-%d %H:%M:%S')
    ok_ts = (datetime.now() - timedelta(seconds=30)).strftime('%Y-%m-%d %H:%M:%S')
    _write(log,
           f'[{fail_ts}] [NAV] H5 已加载\n[WARN] 比赛列表 code={g.TOKEN_DEAD_MARK}\n'
           f'[{ok_ts}] [REG] 刷新完成: 列表 46 场, 成功登记 316 个 mid\n')
    monkeypatch.setattr(g, 'WS_LOG', str(log))
    assert g.token_dead_from_log() is False


def test_token_dead_failure_after_success_is_dead(tmp_path, monkeypatch):
    """成功刷新早于最后一次失效标记 → 仍判为当前失效 (避免用陈旧成功遮盖最新失败)。"""
    log = tmp_path / 'ws_daemon.log'
    ok_ts = (datetime.now() - timedelta(seconds=600)).strftime('%Y-%m-%d %H:%M:%S')
    fail_ts = (datetime.now() - timedelta(seconds=120)).strftime('%Y-%m-%d %H:%M:%S')
    _write(log,
           f'[{ok_ts}] [REG] 刷新完成: 列表 46 场, 成功登记 316 个 mid\n'
           f'[{fail_ts}] [NAV] H5 已加载\n[WARN] 比赛列表 code={g.TOKEN_DEAD_MARK}\n')
    monkeypatch.setattr(g, 'WS_LOG', str(log))
    assert g.token_dead_from_log() is True


def test_env_fingerprint_changes_on_any_line(tmp_path, monkeypatch):
    """bug 2 回归点: 只改 GQ_H5_URL(sessionId) 而 GQ_REQUEST_ID 不变时, 指纹也必须变。"""
    env = tmp_path / '.env'
    _write(env, 'GQ_REQUEST_ID=abc123\nGQ_H5_URL=https://x/?token=abc123&sessionId=111\n')
    monkeypatch.setattr(g, 'ENV_FILE', str(env))
    fp1 = g.env_fingerprint()
    assert fp1

    # 仅 sessionId 变
    _write(env, 'GQ_REQUEST_ID=abc123\nGQ_H5_URL=https://x/?token=abc123&sessionId=222\n')
    fp2 = g.env_fingerprint()
    assert fp2 != fp1, '仅 sessionId 变更必须被指纹捕获'

    # 内容还原 → 指纹还原 (证明不是随机数)
    _write(env, 'GQ_REQUEST_ID=abc123\nGQ_H5_URL=https://x/?token=abc123&sessionId=111\n')
    assert g.env_fingerprint() == fp1


def test_env_fingerprint_contains_no_secret(tmp_path, monkeypatch):
    """指纹不得泄露 token 真实值。"""
    env = tmp_path / '.env'
    _write(env, 'GQ_REQUEST_ID=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n')
    monkeypatch.setattr(g, 'ENV_FILE', str(env))
    fp = g.env_fingerprint()
    assert 'deadbeefdeadbeef' not in fp


def test_alive_false_for_nonexistent_target():
    """不存在的目标必须判为不存活 (否则永远不会拉起)。"""
    assert g._alive('__no_such_target_xyz__.py') is False


def test_guardian_never_kills():
    """守护必须"只拉起、不杀进程" —— 09-19 杀拉循环教训。源码不得出现 kill/terminate。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'scripts', 'prod_guardian.py'), encoding='utf-8').read()
    for banned in ('.kill(', '.terminate(', 'taskkill'):
        assert banned not in src, f'prod_guardian 不得含有 {banned}'


def test_knn_writer_in_jobs_and_safe():
    """2026-09-24 新增: KNN 结论弹性写入器必须接入 JOBS 且只读/只写 prematch_conclusion。

    根因: ws_collector 崩溃期间 KNN tick 随死 → scheduled 场完场后永无结论 (query_match 对
    finished 硬性拒绝, 不可回填) → 9 月 9444 场缺口。解耦独立写入器由守护代拉, 采集器死时仍保覆盖。
    """
    names = [j[0] for j in g.JOBS]
    assert 'knn_conclusion_writer' in names, 'JOBS 必须含 knn_conclusion_writer'
    job = next(j for j in g.JOBS if j[0] == 'knn_conclusion_writer')
    name, cmd, interval, out_f = job
    assert interval <= 600, 'KNN 写入器间隔应 ≤600s (与采集器 tick 同频兜底)'
    assert os.path.exists(cmd[1]), '写入器脚本必须存在'
    # 脚本可编译
    import py_compile
    py_compile.compile(cmd[1], doraise=True)
    # 不得在生产 import 图出现 IR-32 禁区字样 (文档注释里列举禁区词是合规说明, 放行)
    src = open(cmd[1], encoding='utf-8').read()
    for banned in ('cross_book', 'multibook', 'leyu_value_signal', 'bet_split_source',
                   'compute_value_layer', 'bet_core'):
        assert f'import {banned}' not in src, f'knn_conclusion_writer 不得 import IR-32 禁区 {banned}'
        assert f'from {banned}' not in src, f'knn_conclusion_writer 不得 import IR-32 禁区 {banned}'
        assert f'{banned}.' not in src, f'knn_conclusion_writer 不得在生产逻辑调用 IR-32 禁区 {banned}'
    # 只走 store_prematch_conclusion, 不直接写 matches/odds
    assert 'store_prematch_conclusion' in src
    assert 'INSERT INTO matches' not in src and 'INSERT INTO odds' not in src

