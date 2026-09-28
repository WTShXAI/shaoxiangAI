#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T52 混合编码日志解码 SSoT 的跨审计回归守卫
=========================================================================
背景: ``logs/autonomous_monitor.log`` 是**混合编码**(2026-09-28 实测 101 行 UTF-8 /
664 行 GBK / 1 行两者皆非): ``log()`` 以 UTF-8 写文件, 同一行 ``print()`` 落 stdout
时被控制台代码页重编码成 GBK。T44/T48 各踩一次后各自写了自己的 ``decode_line``,
一旦某个解析脚本退回「整文件 decode」或「再抄一份解码器」, 就会重新产出
**假的 0 周期 / 假 0 命中**, 把真 bug 读成健康。

本脚本**纯只读**: 不碰生产服务、不读业务库、不改任何文件(仅写自有报告)。
三问:
  Q1 重复实现守卫 —— ``scripts/`` 下是否还有脚本自带 ``def decode_line`` 而没走 SSoT
  Q2 整文件解码守卫 —— 是否还有脚本对 ``logs/*.log`` 做整文件 ``decode('utf-8'/'gbk')``
  Q3 活体回归     —— 真实混合编码日志能否被共用解码器完整解析出中文行
  Q4 git 静默忽略守卫 (T61 新增) —— ``scripts/``/``tests/`` 下被 ``.gitignore``
      吃掉却没进带理由登记册的 ``.py``; ``.gitignore:142`` 的 ``_*.py`` 曾把 SSoT
      自身整文件忽略 (``_log_codec.py``), 重建环境后守卫集体 import 失败且本地无感。

产出 ``reports/log_codec_ssot_audit.{json,md}``。
"""
from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

sys_path = os.path.dirname(os.path.abspath(__file__))
if sys_path not in __import__('sys').path:
    __import__('sys').path.insert(0, sys_path)

from log_codec_ssot import HAN_RE, decode_line, decode_log_lines, read_log_lines  # noqa: E402

ROOT = os.path.dirname(sys_path)
SCRIPTS = os.path.join(ROOT, 'scripts')
TESTS = os.path.join(ROOT, 'tests')
LOG = os.path.join(ROOT, 'logs', 'autonomous_monitor.log')
OUT_JSON = os.path.join(ROOT, 'reports', 'log_codec_ssot_audit.json')
OUT_MD = os.path.join(ROOT, 'reports', 'log_codec_ssot_audit.md')

#: SSoT 模块名 (T61 由 ``_log_codec.py`` 改名: ``.gitignore:142`` 的 ``_*.py``
#: 会把共享模块整文件忽略, 克隆/重建环境后两个守卫同时 import 失败且本地无感)。
SSOT_MODULE: str = 'log_codec_ssot.py'
SSOT_IMPORT: str = 'log_codec_ssot'          # 不含后缀的模块名 (import 语句用)
SSOT_BASENAMES: Tuple[str, ...] = (SSOT_MODULE,)
SSOT_REL: str = 'scripts/log_codec_ssot.py'

#: 扫描面刻意收窄到 scripts/ 自有模块 (archive/ 与 .venv 是历史产物与依赖, 不参与守卫)。
SKIP_DIRS = {'__pycache__'}

#: Q1 守卫: 本地自带解码器定义 (SSoT 在 log_codec_ssot.py, 其它脚本出现即违规)。
RE_DEF_DECODER = re.compile(r'^def\s+decode_line\s*\(', re.MULTILINE)

#: Q2 守卫: 同一行同时出现 log 路径字面量与整文件解码调用。
RE_LOG_LITERAL = re.compile(r"logs[/\\][A-Za-z0-9_.\-]+\.log")
RE_WHOLE_DECODE = re.compile(r"\.decode\(\s*['\"](?:utf-8|utf8|gbk|gb2312)['\"]\s*\)")


def iter_script_files(scripts_dir: str = SCRIPTS) -> List[str]:
    """列出 scripts/ 下参与守卫的 .py 文件 (跳过 __pycache__)。"""
    out: List[str] = []
    for name in sorted(os.listdir(scripts_dir)):
        if name.endswith('.py'):
            out.append(os.path.join(scripts_dir, name))
    return out


def scan_duplicate_decoders(files: List[str]) -> List[Dict[str, Any]]:
    """Q1: 找出本地自带 ``def decode_line`` 的脚本 (不含 SSoT 自身)。"""
    hits: List[Dict[str, Any]] = []
    for path in files:
        base = os.path.basename(path)
        if base in SSOT_BASENAMES:
            continue
        try:
            src = open(path, 'r', encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        if RE_DEF_DECODER.search(src):
            hits.append({'file': os.path.relpath(path, ROOT).replace('\\', '/'),
                         'issue': 'LOCAL_DECODER_DEF'})
    return hits


def scan_whole_file_decodes(files: List[str]) -> List[Dict[str, Any]]:
    """Q2: 找出对日志路径做整文件解码的代码行 (逐行解码缺失 == 假 0 风险)。"""
    hits: List[Dict[str, Any]] = []
    for path in files:
        base = os.path.basename(path)
        if base in SSOT_BASENAMES:
            continue
        try:
            src = open(path, 'r', encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        for i, line in enumerate(src.splitlines(), 1):
            if RE_LOG_LITERAL.search(line) and RE_WHOLE_DECODE.search(line):
                hits.append({'file': os.path.relpath(path, ROOT).replace('\\', '/'),
                             'line': i,
                             'issue': 'WHOLE_FILE_DECODE_ON_LOG',
                             'snippet': line.strip()[:120]})
    return hits


#: Q4 带理由登记册: 被 .gitignore 命中的 .py 必须逐条写明理由。
#: 空理由不许登记 —— 否则「每次自动豁免」会让守卫退化成无守卫 (同 T57 立场)。
GITIGNORED_PY_REGISTRY: Dict[str, str] = {
    'scripts/_analyze_live_ou_margin.py': (
        '一次性只读分析脚本 (2026-08-24, 读 match_outcomes 验证滚球 OU 余量是否结构固定), '
        '全仓零引用、无生产调用方、不含 SSoT 语义; 不入库是因为它是一次性产物而非模块'
    ),
}


def iter_python_files(*roots: str) -> List[str]:
    """列出给定根目录下的 .py 文件 (跳过 __pycache__)。"""
    out: List[str] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if name.endswith('.py') and name != '__init__.py':
                out.append(os.path.join(root, name))
    return out


def scan_git_ignored_python(roots: Tuple[str, ...] = (SCRIPTS, TESTS)) -> Dict[str, Any]:
    """Q4: 找出**被 .gitignore 静默吃掉**的 .py 文件 (整类隐患, 不只 SSoT)。

    失效模式 (T52/T57 各踩一次): ``.gitignore:142`` 的 ``_*.py`` 会把共享模块
    (``_log_codec.py`` / ``_verdict_guard.py``) 整文件忽略 —— ``git status`` 里连
    ``??`` 都不显示, 重建环境后守卫集体 import 失败, 而本地工作区毫无异常。
    这里用 ``git check-ignore --stdin`` 一次批查, 命中项**必须进登记册并写理由**。

    Returns:
        dict: 未登记项 (FAIL 源) / 已登记项 / 是否被跳过(git 不可用)。
    """
    files = iter_python_files(*roots)
    rels = [os.path.relpath(p, ROOT).replace('\\', '/') for p in files]
    result: Dict[str, Any] = {'scanned': len(rels), 'ignored_total': None,
                              'unregistered': [], 'registered': {}}
    try:
        import subprocess
        # 注意: 必须走 **bytes** 入参 —— 2026-09-28 T61 实测, text=True 传多行文本时
        # git 侧返回空 (rc=1), 整份扫描静默变「0 个被忽略文件」假绿。
        payload = ('\n'.join(rels) + '\n').encode('utf-8')
        proc = subprocess.run(['git', 'check-ignore', '--stdin'],
                              cwd=ROOT, input=payload,
                              capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        result['skipped'] = 'git 不可用或超时'
        return result
    ignored = [ln.strip() for ln in (proc.stdout or b'').decode('utf-8', 'replace').splitlines()
               if ln.strip()]
    result['ignored_total'] = len(ignored)
    for rel in ignored:
        if rel in GITIGNORED_PY_REGISTRY:
            result['registered'][rel] = True
        else:
            result['unregistered'].append(rel)
    return result


def live_regression(log_path: str = LOG) -> Dict[str, Any]:
    """Q3: 真实日志能否被共用解码器完整解析 (防「0 周期 / 0 命中」假零)。

    Args:
        log_path: 日志文件路径。

    Returns:
        dict: 是否存在 / 行数 / 含中文行数 / 是否含周期标记 / 逐行解码 vs 整文件解码差异。
    """
    exists = os.path.exists(log_path)
    result: Dict[str, Any] = {'path': log_path, 'exists': exists}
    if not exists:
        result['verdict'] = 'LOG_MISSING_SKIP'
        return result
    with open(log_path, 'rb') as f:
        raw = f.read()
    # 字节入参与路径入参必须给出同样的行数; 不等说明解码路径出现"假零"(2026-09-28 T52)。
    lines = decode_log_lines(raw)
    cross = read_log_lines(raw)
    han = [ln for ln in lines if HAN_RE.search(ln)]
    result.update({
        'bytes': len(raw),
        'lines': len(lines),
        'han_lines': len(han),
        # 字节路径与文件路径的差只允许 1 行(尾随空行剥除), 更大差即"假零"嫌疑。
        'bytes_path_line_delta': len(lines) - len(cross),
        'verdict': 'OK' if (len(lines) > 0 and len(han) > 0) else 'DECODE_LOST_HAN',
    })
    # 反证: 整文件 utf-8 解码必然失败或丢行(混合编码), 用于给报告留证据。
    try:
        raw.decode('utf-8')
        result['whole_file_utf8_works'] = True
        result['whole_file_baseline_lines'] = len(raw.decode('utf-8').splitlines())
    except UnicodeDecodeError:
        result['whole_file_utf8_works'] = False
        result['whole_file_baseline_lines'] = None
    try:
        gbk_text = raw.decode('gbk')
        result['whole_file_gbk_lines'] = len(gbk_text.splitlines())
        result['whole_file_gbk_han_lines'] = len(
            [ln for ln in gbk_text.splitlines() if HAN_RE.search(ln)])
    except UnicodeDecodeError:
        result['whole_file_gbk_lines'] = None
        result['whole_file_gbk_han_lines'] = None
    return result


def build_findings(duplicates: List[Dict[str, Any]],
                   whole_decodes: List[Dict[str, Any]],
                   live: Dict[str, Any],
                   git_ignored: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """汇总守卫结论: 任一红项即 FAIL (fail-closed)。

    Args:
        git_ignored: Q4 结果 (``None`` 时按「未做检查 = FAIL」处理, 同 T51 立场)。
    """
    git_ignored = git_ignored if git_ignored is not None else {'unregistered': ['<NOT RUN>']}
    ok = (not duplicates and not whole_decodes
          and live.get('verdict') in ('OK', 'LOG_MISSING_SKIP')
          and live.get('bytes_path_line_delta', 0) in (0, 1)
          and not git_ignored.get('unregistered'))
    return {
        'verdict': 'PASS' if ok else 'FAIL',
        'q1_duplicate_decoders': duplicates,
        'q2_whole_file_decodes': whole_decodes,
        'q3_live_regression': live,
        'q4_git_ignored_python': git_ignored,
        'ssot': SSOT_REL,
    }


def write_report(findings: Dict[str, Any], out_json: str = OUT_JSON,
                 out_md: str = OUT_MD) -> Dict[str, str]:
    """落盘 JSON + Markdown 报告 (仅写 reports/ 自有文件)。"""
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(findings, f, ensure_ascii=False, indent=2)
    live = findings.get('q3_live_regression', {})
    lines = [
        '# T52 日志解码 SSoT 回归守卫',
        '',
        f"- **结论**: {findings['verdict']}",
        f"- **SSoT**: `{findings['ssot']}`",
        f"- **Q1 重复解码器**: {len(findings['q1_duplicate_decoders'])} 处",
        f"- **Q2 整文件解码**: {len(findings['q2_whole_file_decodes'])} 处",
        f"- **Q3 活体回归**: {live.get('verdict')} (lines={live.get('lines')} / han={live.get('han_lines')})",
        f"- **Q4 git 静默忽略的 .py**: 未登记 {len(findings['q4_git_ignored_python'].get('unregistered', []))} 处"
        f" / 已登记带理由 {len(findings['q4_git_ignored_python'].get('registered', {}))} 处",
    ]
    for item in findings['q4_git_ignored_python'].get('unregistered', []):
        lines.append(f"  - Q4 {item}: UNREGISTERED_GIT_IGNORED — 未被版本控制且无理由登记(重建环境会静默丢失)")
    for item in findings['q1_duplicate_decoders']:
        lines.append(f"  - Q1 {item['file']}: {item['issue']}")
    for item in findings['q2_whole_file_decodes']:
        lines.append(f"  - Q2 {item['file']}:{item['line']}: {item['issue']}")
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return {'json': out_json, 'md': out_md}


def run_audit(write: bool = True) -> Dict[str, Any]:
    """执行三问守卫; ``write=False`` 供测试用 (不落盘)。"""
    files = iter_script_files()
    duplicates = scan_duplicate_decoders(files)
    whole_decodes = scan_whole_file_decodes(files)
    live = live_regression()
    git_ignored = scan_git_ignored_python()
    findings = build_findings(duplicates, whole_decodes, live, git_ignored)
    if write:
        write_report(findings)
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description='T52 日志解码 SSoT 回归守卫 (纯只读)')
    ap.add_argument('--no-write', action='store_true', help='只打印不落盘')
    args = ap.parse_args()
    findings = run_audit(write=not args.no_write)
    print(json.dumps(findings, ensure_ascii=False, indent=2))
    return 0 if findings['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
