#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T52 混合编码日志解码 SSoT 的回归守卫测试。

覆盖四条:
  T1 共用解码器本身正确 (逐行挑编码, 中文不丢, 绝不抛)
  T2 跨审计守卫: 无重复解码器 / 无对日志的整文件解码 (fail-closed)
  T3 活体回归: 真实混合编码日志必须能解出中文行 (防"假 0 周期 / 假 0 命中")
  T4 (T61) git 跟踪: SSoT 必须被 git 索引, 且 scripts/ + tests/ 下被 .gitignore
     静默吃掉的 .py 必须逐条进带理由登记册 (空理由不许登记)

纯只读: 不碰生产服务、不读业务库、不写生产数据; 临时文件只在 tmp_path 内。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from scripts.audit_log_codec_ssot import (  # noqa: E402
    GITIGNORED_PY_REGISTRY, ROOT, SSOT_IMPORT, SSOT_MODULE, SSOT_REL, build_findings,
    iter_script_files, iter_python_files, live_regression,
    scan_duplicate_decoders, scan_git_ignored_python,
    scan_whole_file_decodes, write_report,
)

import log_codec_ssot  # noqa: E402,F401
from log_codec_ssot import decode_line, read_log_lines  # noqa: E402

# --- 测试数据 ---------------------------------------------------------------

UTF8_LINE = '[2026-09-27 08:56:12] [INFO] 周期完成 67.4s\n'.encode('utf-8')
GBK_LINE = '[2026-09-27 10:02:33] [ERROR] 预测补算失败\n'.encode('gbk')


# --- T1 共用解码器 -----------------------------------------------------------

def test_decode_line_picks_encoding_per_line():
    """同一份混合内容里, UTF-8 行与 GBK 行都必须解出中文。"""
    mixed = UTF8_LINE + GBK_LINE
    out = [decode_line(b) for b in mixed.split(b'\n')]
    assert '周期完成' in out[0]
    assert '预测补算失败' in out[1]


def test_decode_line_never_raises_on_binary_garbage():
    """任何字节都不许抛异常 —— 日志解析失败必须表现为"读不到"而不是"崩了"。"""
    assert isinstance(decode_line(b'\x8b\x11\xff\xfe\x00\x81'), str)
    assert isinstance(decode_line(b''), str)


def test_read_log_lines_preserves_line_boundaries(tmp_path):
    path = tmp_path / 'mixed.log'
    path.write_bytes(UTF8_LINE + GBK_LINE)
    lines = read_log_lines(str(path))
    assert len(lines) == 2
    assert any('周期完成' in ln for ln in lines)
    assert any('预测补算失败' in ln for ln in lines)


def test_read_log_lines_missing_file_returns_empty_list(tmp_path):
    assert read_log_lines(str(tmp_path / 'nope.log')) == []


# --- T2 跨审计守卫 -----------------------------------------------------------

def test_ssot_module_is_the_only_decoder_definition():
    """守卫语义: 除 SSoT 外, scripts/ 不得再出现本地 ``def decode_line``。"""
    hits = scan_duplicate_decoders(iter_script_files())
    offenders = [h['file'] for h in hits if not h['file'].endswith(SSOT_MODULE)]
    assert offenders == [], f'重复解码器: {offenders}'


def test_migrated_lock_audit_imports_ssot_instead_of_defining():
    """迁移证据: 原 T44 脚本已改为 import, 且其解码行为与 SSoT 一致。"""
    import scripts.audit_predict_refresh_lock as lock_audit
    src = open(lock_audit.__file__, 'r', encoding='utf-8').read()
    assert f'from {SSOT_IMPORT} import' in src
    assert 'def decode_line(' not in src
    assert decode_line(GBK_LINE) == lock_audit.decode_line(GBK_LINE)


def test_no_whole_file_decode_applied_to_log_paths():
    """对 logs/*.log 做整文件 decode 就是"假 0 周期"的根源, 必须为零。"""
    hits = scan_whole_file_decodes(iter_script_files())
    assert hits == [], f'整文件解码日志: {hits}'


def test_build_findings_is_fail_closed():
    """任一红项即 FAIL (不因其它项绿而放行); 缺 live 结果按 FAIL 处理。"""
    q4_ok = {'unregistered': []}
    bad = build_findings([{'file': 'x.py', 'issue': 'LOCAL_DECODER_DEF'}], [], {})
    assert bad['verdict'] == 'FAIL'
    assert build_findings([], [{'file': 'y.py', 'line': 1, 'issue': 'X'}],
                          {'verdict': 'OK'})['verdict'] == 'FAIL'
    assert build_findings([], [], {})['verdict'] == 'FAIL'
    assert bad['verdict'] == 'FAIL'
    good = build_findings([], [], {'verdict': 'OK'}, q4_ok)
    assert good['verdict'] == 'PASS'


# --- T3 活体回归 ---------------------------------------------------------------

def test_live_log_decodes_chinese_lines():
    """真实混合编码日志必须解析出中文行, 否则守卫本身就是假的。"""
    live = live_regression()
    if not live['exists']:
        pytest.skip('monitor 日志不存在, 跳过活体回归')
    assert live['lines'] > 0
    assert live['han_lines'] > 0, '整份日志一个中文行都解不出来 -> 解码器失效'
    assert live['verdict'] == 'OK'


def test_whole_file_utf8_decode_is_proven_broken_on_live_log():
    """反证: 整文件 UTF-8 解码在混合编码日志上确实失败 —— 这是守卫存在的理由。"""
    live = live_regression()
    if not live['exists']:
        pytest.skip('monitor 日志不存在, 跳过活体反证')
    if live.get('whole_file_utf8_works'):
        pytest.skip('当前日志恰为纯 UTF-8, 混合编码未复现(守卫仍保留)')
    assert live['whole_file_baseline_lines'] is None


def test_write_report_emits_markdown(tmp_path):
    findings = build_findings([], [], {'verdict': 'OK', 'lines': 3, 'han_lines': 2},
                              {'unregistered': [], 'registered': {}, 'scanned': 2,
                               'ignored_total': 0})
    paths = write_report(findings, out_json=str(tmp_path / 'a.json'),
                         out_md=str(tmp_path / 'a.md'))
    md = open(paths['md'], encoding='utf-8').read()
    assert 'PASS' in md and SSOT_REL in md
    assert os.path.exists(paths['json'])


def test_repo_root_is_architecture():
    """路径锚定: 守卫必须跑在 D:\\Architecture 根, 否则扫描面整体失效。"""
    assert os.path.basename(ROOT) == 'Architecture' or ROOT.endswith('Architecture')


# --- T4 (T61) git 跟踪守卫 ------------------------------------------------------

def test_ssot_module_is_tracked_by_git():
    """踩过的坑: ``.gitignore`` 的 ``_*.py`` 会把共享 SSoT 模块整文件忽略。

    T57 撞上 ``scripts/_verdict_guard.py``, T52 撞上 ``scripts/_log_codec.py`` ——
    两者都会被 ``.gitignore:142`` 吃掉, ``git status`` 里连 ``??`` 都不显示,
    重建/克隆环境后守卫集体 import 失败而 **本地毫无异常**。SSoT 改名后本条锁死它
    必须能被 git 索引。
    """
    path = os.path.join(ROOT, SSOT_REL)
    assert os.path.exists(path), f'{SSOT_REL} 缺失 → 两个审计守卫同时失效'
    try:
        proc = subprocess.run(['git', 'check-ignore', '-q', SSOT_REL],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip('git 不可用')
    assert proc.returncode != 0, f'{SSOT_REL} 被 .gitignore 命中 → 重建环境后守卫静默失效'
    # 真正的根因守卫: check-ignore 对【未跟踪】文件也返回「未忽略」(rc=1) → 假绿。
    # 必须再断言文件已进 git 索引, 否则克隆/重建后该 SSoT 直接丢失、守卫 import 集体失败。
    tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', SSOT_REL],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert tracked.returncode == 0, f'{SSOT_REL} 未纳入 git 索引(未跟踪) → 克隆/重建后守卫静默失效'


def test_ssot_module_name_avoids_leading_underscore():
    """改名后的模块名不得再以 ``_`` 开头 (``_*.py`` 是 .gitignore 的整类地雷)。"""
    assert not SSOT_MODULE.startswith('_'), (
        'SSoT 模块名以 _ 开头 → 会被 .gitignore:142 的 ``_*.py`` 忽略, '
        '重建环境后本守卫与 T44 锁审计同时 import 失败且本地无感'
    )


def test_no_silent_git_ignored_python_modules():
    """整类守卫: scripts/ + tests/ 下被 .gitignore 命中的 .py 必须都有理由登记。"""
    res = scan_git_ignored_python()
    if 'skipped' in res:
        pytest.skip(res['skipped'])
    assert res['unregistered'] == [], (
        f'以下 .py 被 .gitignore 静默吃掉且未登记理由: {res["unregistered"]} → '
        '重建环境后文件会连同守卫一起丢失'
    )
    for rel in res['registered']:
        assert rel in GITIGNORED_PY_REGISTRY and GITIGNORED_PY_REGISTRY[rel].strip()


def test_scan_git_ignored_python_fails_closed_in_temp_repo(tmp_path):
    """守卫自证: 在**临时仓库**里必须检出被 `_*.py` 吃掉的模块 (防恒绿)。

    原用例依赖「真仓库里存在一个被忽略的 .py」这一事实 —— 2026-09-28 该事实被修复
    (自动化把 `_*.py` 真实源 `git add` 入库), 用例随之失效。改成临时仓库自证后,
    **不再依赖真实仓库状态**: 无论线上是否还剩被忽略文件, 守卫的检出能力都被钉死。
    """
    import subprocess
    repo = tmp_path / 'repo'
    (repo).mkdir()
    proc = subprocess.run(['git', 'init', '-q'], cwd=str(repo), capture_output=True)
    if proc.returncode != 0:
        pytest.skip('git init 不可用')
    (repo / '.gitignore').write_text('_*.py\n', encoding='utf-8')
    (repo / '_hidden.py').write_text('x = 1\n', encoding='utf-8')
    (repo / 'visible.py').write_text('y = 1\n', encoding='utf-8')
    res = scan_git_ignored_python(roots=(str(repo),), root=str(repo))
    if 'skipped' in res:
        pytest.skip(res['skipped'])
    assert res['ignored_total'] == 1, res
    assert res['unregistered'] == ['_hidden.py'], res
    assert res['registered'] == {}


def test_live_git_scan_is_closed():
    """真仓库: 被忽略项必须全部归入登记册或未登记项 (三者对账闭合, 不静默丢)。"""
    res = scan_git_ignored_python()
    if 'skipped' in res:
        pytest.skip(res['skipped'])
    assert res['ignored_total'] == len(res['registered']) + len(res['unregistered'])
    for rel in res['registered']:
        assert rel in GITIGNORED_PY_REGISTRY and GITIGNORED_PY_REGISTRY[rel].strip()


def test_guard_registry_reasons_cannot_be_empty():
    """空理由不许登记 (同 T57 立场): 防止「自动豁免」把守卫掏空。"""
    bad = [k for k, v in GITIGNORED_PY_REGISTRY.items() if not str(v).strip()]
    assert bad == [], f'空理由登记: {bad}'


def test_build_findings_fails_when_git_scan_not_run():
    """未做 Q4 检查必须算 FAIL (沿用 T51「未检查 = FAIL」立场)。"""
    good = build_findings([], [], {'verdict': 'OK'}, {'unregistered': []})
    assert good['verdict'] == 'PASS'
    assert build_findings([], [], {'verdict': 'OK'})['verdict'] == 'FAIL'
    assert build_findings(
        [], [], {'verdict': 'OK'}, {'unregistered': ['scripts/_x.py']}
    )['verdict'] == 'FAIL'


def test_iter_python_files_covers_both_roots():
    scripts_n = len(iter_python_files(os.path.join(ROOT, 'scripts')))
    tests_n = len(iter_python_files(os.path.join(ROOT, 'tests')))
    both = len(iter_python_files(os.path.join(ROOT, 'scripts'), os.path.join(ROOT, 'tests')))
    assert scripts_n > 0 and tests_n > 0 and both == scripts_n + tests_n
