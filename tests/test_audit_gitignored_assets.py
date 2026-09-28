#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T66 gitignore 静默忽略资产盘点的回归守卫测试。

覆盖四条:
  U1 分类器: `.env` 家族必须先于后缀判定 (否则密钥面静默消失) / 模板单独归类
  U2 登记册: 未登记且被引用 = RED; 未登记未引用 = 留痕但不 RED; 理由不许为空
  U3 资产面: 单副本大资产判定 (同名副本存在即不算单副本)
  U4 活体: 真仓库扫描必须闭合 (39 = 已登记 + 未登记被引用 + 未登记未引用) 且未登记被引用为 0

纯只读: 只读 git 索引与文件元信息, 不 `git add`、不删、不改数据。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from scripts.audit_gitignored_assets import (  # noqa: E402
    ASSET_MIN_BYTES, IGNORED_PY_REGISTRY, classify, ignored_files, registry_reason,
    scan, write_report,
)

# --- U1 分类器 ---------------------------------------------------------------

def test_env_family_classified_before_suffix_rules():
    """`.env` 无后缀, 若先走后缀判定会静默落进 OTHER 并从密钥面统计消失。"""
    assert classify('.env') == 'CONFIG_SECRET'
    assert classify('gq/.env') == 'CONFIG_SECRET'
    assert classify('gq/.env.bak_20260916_195717') == 'CONFIG_SECRET'


def test_env_templates_are_not_classified_as_secrets():
    """模板不是密钥: 它本应入库, 归到 CONFIG_TEMPLATE 才能被单独点名。"""
    assert classify('.env.example') == 'CONFIG_TEMPLATE'
    assert classify('deploy/.env.example') == 'CONFIG_TEMPLATE'


def test_other_categories_unchanged():
    assert classify('scripts/foo.py') == 'SOURCE_CODE'
    assert classify('data/events.db') == 'DATA_ASSET'
    assert classify('config/settings.yaml') == 'CONFIG'
    assert classify('data/logo.png') == 'OTHER'


# --- U2 登记册 ---------------------------------------------------------------

def test_registry_reasons_cannot_be_empty():
    bad = [k for k, v in IGNORED_PY_REGISTRY.items() if not str(v).strip()]
    assert bad == [], f'空理由登记: {bad}'


def test_registry_lookup_supports_file_and_prefix():
    assert registry_reason('scripts/_analyze_live_ou_margin.py')
    assert registry_reason('archive/root_debris_20260918/scratch_x.py')
    assert registry_reason('gq/_trash_2026-08-27/diag_dom.py')
    assert registry_reason('scripts/never_registered.py') is None


def test_registry_prefix_must_end_with_slash():
    """前缀必须带 `/` —— 否则 `data` 会误吞 `database_notes.py` 一类同前缀文件。

    2026-09-28 本轮实测: 写成 `gq/_trash_` 时不生效 (匹配逻辑要求以 `/` 结尾),
    单测当场打红 —— 这条守卫就是为了把这种"看起来登记了其实没登记"钉死。
    """
    for key in IGNORED_PY_REGISTRY:
        assert key.endswith('.py') or key.endswith('/'), f'可疑登记键: {key}'


# --- U3 资产面 ---------------------------------------------------------------

def test_single_copy_detection_marks_duplicates_as_not_single(tmp_path):
    """同名副本存在时不得判为"单副本"(防把有备份的资产误报成风险)。"""
    from scripts.audit_gitignored_assets import _dup_lookup
    names = {'a.db': ['data/a.db', 'backups/a.db']}
    assert _dup_lookup(names, 'data/a.db') == ['backups/a.db']
    assert _dup_lookup(names, 'other/missing.db') == []


def test_asset_threshold_is_100mb():
    assert ASSET_MIN_BYTES == 100 * 1024 * 1024


# --- U4 活体 -----------------------------------------------------------------

@pytest.fixture(scope='module')
def live():
    return scan(do_reference_check=True)


def test_live_scan_is_closed_and_has_no_referenced_unregistered(live):
    """不闭合 = 报告本身有洞; 未登记被引用 = 克隆即断链 (RED)。"""
    assert live['source_code_accounted'] == live['source_code_total'], (
        f"对账不闭合: {live['source_code_accounted']} != {live['source_code_total']}"
    )
    assert live['source_code_unregistered_referenced'] == [], (
        f"未登记且被引用: {live['source_code_unregistered_referenced']}"
    )


def test_live_scan_finds_ignored_files_at_all(live):
    """扫描面不能是空的 —— 空结果=扫描失效(假绿)。"""
    assert live['ignored_total'] > 100
    assert live['tracked_total'] > 500


def test_live_single_copy_assets_include_p2_package_b(live):
    """P2 保底锚实体必须在单副本清单里被点名 (§4), 否则守卫漏掉了最重要的资产。"""
    files = [a['file'] for a in live['single_copy_assets_ge_100mb']]
    assert any('p2_package_B.sqlite' in f for f in files), (
        f'P2 Package B 未被点名, 实际清单: {files}'
    )


def test_live_env_templates_are_tracked_now(live):
    """本轮修复已落地: 模板不再出现在"未入库"清单里。

    修复前实测 3 处 (`.env.example` / `deploy/.env.example` / `frontend/.env.example`)
    被 `.gitignore:36` 的 `.env.*` 吃掉; 修复 = `!**/.env.example` + `git add`。
    检测能力本身由 U1 分类器用例覆盖 (不依赖真实仓库状态)。
    """
    assert live['env_templates_untracked'] == [], (
        f"又有模板掉出 git: {live['env_templates_untracked']}"
    )


def test_live_honest_scope_present(live):
    """必须带上"副本检测只在本仓内"的诚实口径, 防止被读成"绝对无备份"。"""
    assert '本仓' in live['honest_scope']


def test_ignored_files_excludes_dependency_noise():
    files = ignored_files()
    bad = [f for f in files if 'node_modules' in f or '.venv/' in f or '__pycache__' in f]
    assert bad == [], f'扫描面混入依赖噪声: {bad[:5]}'


def test_env_templates_tracked_and_real_env_still_ignored():
    """本轮的真修复: 模板入库(重建环境才知道要填哪些键) + 真 `.env` 仍被忽略(§8)。

    踩过的坑: `.gitignore:36` 的 `.env.*` 把 `.env.example` 一并吃掉 → 模板从未入库,
    克隆后不知道需要哪些环境变量。修复 = 加 `!**/.env.example` 负向规则 + `git add`。
    """
    import subprocess
    def _git(*args):
        return subprocess.run(['git'] + list(args), cwd=ROOT,
                              capture_output=True, timeout=60)

    for rel in ('.env.example', 'deploy/.env.example', 'frontend/.env.example'):
        p = _git('ls-files', '--error-unmatch', rel)
        assert p.returncode == 0, f'{rel} 未被 git 跟踪 → 重建环境缺模板(本守卫防它退回)'
    for rel in ('.env', 'gq/.env'):
        if not os.path.exists(os.path.join(ROOT, rel)):
            continue
        p = _git('check-ignore', '-q', rel)
        assert p.returncode == 0, f'{rel} 不再被 .gitignore 命中 → 真密钥可能被提交(§8)'


def test_write_report_emits_both_verdicts(tmp_path):
    findings = scan(do_reference_check=False)
    paths = write_report(findings, out_json=str(tmp_path / 'a.json'),
                         out_md=str(tmp_path / 'a.md'))
    md = open(paths['md'], encoding='utf-8').read()
    assert findings['verdict'] in md
    assert '诚实口径' in md
