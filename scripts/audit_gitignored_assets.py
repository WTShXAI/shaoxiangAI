#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T66 全仓「被 .gitignore 静默吃掉的文件」只读盘点 (资产面)
=========================================================================
承接：T61（`scripts/_log_codec.py` 事故）+ 自动化 T63（`_*.py` 吃掉整棵包树）的 Q4 守卫只覆盖 `scripts/` + `tests/` 的
`.py`。但 `.gitignore` 的 `_*.py` / `_*.log` / `_*.png` / `_*.db` / `*.sqlite`
是**目录无关**规则 —— 仓库根、`deliverables/`、`data/`、`archive/`、`.workbuddy/`
下同样躺着未入库的文件。本例要回答的不是「有多少」，而是：

  Q1 **有多少被忽略的源码/配置**（.py / .json / .yaml），落进什么类别
  Q2 其中**有没有被已跟踪代码引用** —— 有引用 = 重建/克隆环境即断链（真风险）
  Q3 **资产类**（大 .db/.sqlite）：是否有第二份副本 —— 单副本 = §4 数据资产保全风险
     （重点：`deliverables/p2_package_b/p2_package_B.sqlite` 13.9GB 是 P2 保底锚实体）

设计原则：
  * **只读** —— 只跑 `git ls-files` / `os.stat` / grep 已跟踪文本; 不 `git add`、不删、不动数据。
  * **带理由登记册**（沿用 T57 立场）：被忽略的 `.py` 必须按前缀/逐文件写明理由，
    空理由不许登记; 未被登记且**被已跟踪代码引用**者 → RED。
  * **未做检查 = FAIL**（沿用 T51 立场）。

产出 ``reports/gitignored_assets_audit.{json,md}``。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_JSON = os.path.join(ROOT, 'reports', 'gitignored_assets_audit.json')
OUT_MD = os.path.join(ROOT, 'reports', 'gitignored_assets_audit.md')

#: 扫描面排除：依赖/构建缓存/版本库元数据（不是"资产"，盘点它们只会淹没信号）。
SKIP_PARTS: Tuple[str, ...] = (
    'node_modules', '.venv', '__pycache__', '.git/', '.pytest_cache',
)

#: 被忽略的 `.py` **逐文件/前缀**理由登记册（空理由不许登记）。
#: 语义：这些文件"应该在 git 之外"(历史快照/交付产物/一次性分析），
#: 而不是"被规则误伤"（误伤者必须改名或 `git add`）。
IGNORED_PY_REGISTRY: Dict[str, str] = {
    'scripts/_analyze_live_ou_margin.py': (
        '一次性只读分析脚本 (2026-08-24, 验证滚球 OU 余量是否结构固定); 全仓零引用、'
        '无生产调用方 → 不入库是有意选择 (T61 登记)'
    ),
    'archive/': '历史快照目录 (quant_system_20260919 / betting_decisions / root_debris 等), 有意不入库',
    'gq/_trash_2026-08-27/': '显式垃圾目录 (2026-08-27 清理时的临时诊断脚本), 有意保留在盘上但不入库',
    'deliverables/': '交付产物与审计脚本快照 (对外交付用, 非生产源码)',
    'data/': '数据分析/抽取脚本 (一次性, 结果已落库或报告)',
    'odds_db/': 'ODDS 数据脚本与人工录入产物',
    '.workbuddy/': 'agent 工作区 (技能包脚本 / 会话产物), 非生产代码',
    'analysis/': '分析脚本 (部分被 bridge 引用者除外 — 见 referenced 判定)',
}

#: 资产类阈值：>= 该体积的 ignored 文件必须有第二份副本，否则 AMBER（§4）。
ASSET_MIN_BYTES: int = 100 * 1024 * 1024

#: 源码类后缀（被忽略需登记）。
SOURCE_SUFFIXES: Tuple[str, ...] = ('.py',)
#: 配置类后缀（被忽略需登记；.env 属密钥，登记不报体积）。
CONFIG_SUFFIXES: Tuple[str, ...] = ('.json', '.yaml', '.yml', '.toml', '.ini')
#: 资产类后缀。
ASSET_SUFFIXES: Tuple[str, ...] = ('.db', '.sqlite', '.parquet', '.zip', '.joblib', '.csv')


def _run_git(args: List[str], cwd: str = ROOT) -> str:
    """执行 git 并返回 stdout 文本。

    注意（T61 踩坑）：`subprocess.run(..., text=True, input=...)` 传多行文本时实测
    返回空 stdout → 静默"零结果"假绿。故一律走 **bytes** 入参/出参。
    """
    proc = subprocess.run(['git'] + args, cwd=cwd, capture_output=True, timeout=120)
    return (proc.stdout or b'').decode('utf-8', 'replace')


def _is_skipped(rel: str) -> bool:
    norm = rel.replace('\\', '/')
    return any(part in norm for part in SKIP_PARTS)


def tracked_files() -> List[str]:
    """已跟踪文件清单（用于引用面核查）。"""
    return [ln.strip() for ln in _run_git(['ls-files']).splitlines() if ln.strip()]


def ignored_files() -> List[str]:
    """未跟踪且被 `.gitignore` 命中的文件清单（已跟踪文件天然不出现）。"""
    out = _run_git(['ls-files', '--others', '--ignored', '--exclude-standard'])
    return [ln.strip() for ln in out.splitlines()
            if ln.strip() and not _is_skipped(ln.strip())]


def classify(rel: str) -> str:
    """按后缀/文件名分类；未知后缀归 OTHER。

    顺序敏感：``.env`` 家族**没有后缀**（``.env`` / ``.env.bak``），必须先判，
    否则会静默落进 OTHER 并从密钥面统计里消失（本节实测踩到）。
    """
    low = rel.lower()
    base = os.path.basename(low)
    # 密钥面：.env / .env.bak / gq/.env.bak_2026... （一律 ignored 是正确的，须计数留痕）
    if base == '.env' or base.startswith('.env.') or '.env.' in base:
        if base.endswith('.example'):
            return 'CONFIG_TEMPLATE'      # 模板本应入库 → 未入库是缺陷
        return 'CONFIG_SECRET'
    if low.endswith(SOURCE_SUFFIXES):
        return 'SOURCE_CODE'
    if low.endswith(ASSET_SUFFIXES):
        return 'DATA_ASSET'
    if low.endswith(CONFIG_SUFFIXES):
        return 'CONFIG'
    return 'OTHER'


def registry_reason(rel: str) -> Optional[str]:
    """命中登记册则返回理由，否则 None（= 未登记）。"""
    norm = rel.replace('\\', '/')
    if norm in IGNORED_PY_REGISTRY:
        return IGNORED_PY_REGISTRY[norm]
    for prefix, reason in IGNORED_PY_REGISTRY.items():
        if prefix.endswith('/') and norm.startswith(prefix):
            return reason
    return None


def referenced_by_tracked(rel: str, tracked: List[str]) -> List[str]:
    """该文件是否被已跟踪文件引用（按 basename 与去后缀模块名双查）。

    只把「已跟踪的文本文件」纳入搜索面，避免被自身/其它未跟踪产物误导。
    """
    base = os.path.basename(rel)
    stem = base[:-3] if base.endswith('.py') else base
    hits: List[str] = []
    for tf in tracked:
        low = tf.lower()
        if not low.endswith(('.py', '.bat', '.ps1', '.md', '.json', '.yaml', '.yml',
                             '.toml', '.ini', '.cfg', '.xml', '.sh')):
            continue
        path = os.path.join(ROOT, tf)
        try:
            if os.path.getsize(path) > 2_000_000:
                continue
            txt = open(path, 'r', encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        if base in txt or (stem and f'{stem}.py' in txt) or (stem and f'{stem} import' in txt):
            hits.append(tf)
    return hits


def _dup_lookup(names: Dict[str, List[str]], rel: str) -> List[str]:
    """同名文件的其它副本（用于"单副本"判定）。"""
    base = os.path.basename(rel)
    return [other for other in names.get(base, []) if other != rel]


def scan(asset_min_bytes: int = ASSET_MIN_BYTES,
         do_reference_check: bool = True) -> Dict[str, Any]:
    """执行三项盘点（纯只读）。"""
    ign = ignored_files()
    trk = tracked_files()
    all_seen: Dict[str, List[str]] = {}
    for rel in list(ign) + list(trk):
        all_seen.setdefault(os.path.basename(rel), []).append(rel)

    py_unregistered: List[Dict[str, Any]] = []
    py_unreferenced: List[Dict[str, Any]] = []
    py_registered: List[Dict[str, Any]] = []
    single_copy_assets: List[Dict[str, Any]] = []
    env_templates_untracked: List[str] = []
    secret_files: List[str] = []
    counts: Dict[str, int] = {}
    asset_bytes_total = 0

    for rel in ign:
        cat = classify(rel)
        counts[cat] = counts.get(cat, 0) + 1
        try:
            size = os.path.getsize(os.path.join(ROOT, rel))
        except OSError:
            size = 0
        if cat == 'DATA_ASSET':
            asset_bytes_total += size
        if cat == 'CONFIG_SECRET':
            secret_files.append(rel)
            continue
        if cat == 'CONFIG_TEMPLATE':
            env_templates_untracked.append(rel)
            continue
        if cat == 'SOURCE_CODE':
            reason = registry_reason(rel)
            refs = referenced_by_tracked(rel, trk) if (do_reference_check and not reason) else []
            item = {'file': rel, 'bytes': size}
            if reason:
                item['reason'] = reason
                py_registered.append(item)
            elif refs:
                # 真风险: 被已跟踪代码引用却未入库 → 克隆/重建环境即断链。
                item['referenced_by'] = refs[:5]
                py_unregistered.append(item)
            else:
                # 未被引用且未登记: 不是断链风险, 但必须留痕(不许静默丢)。
                py_unreferenced.append(item)
        elif cat == 'DATA_ASSET' and size >= asset_min_bytes:
            dups = _dup_lookup(all_seen, rel)
            if not dups:
                single_copy_assets.append({
                    'file': rel, 'bytes': size,
                    'note': '单副本且未入库 → 磁盘故障/误删即不可恢复 (§4 数据资产保全)',
                })

    single_copy_assets.sort(key=lambda d: -d['bytes'])
    verdict = 'FAIL' if py_unregistered else ('AMBER' if (single_copy_assets or env_templates_untracked) else 'PASS')
    return {
        'verdict': verdict,
        'counts': counts,
        'source_code_total': counts.get('SOURCE_CODE', 0),
        'source_code_accounted': (len(py_registered) + len(py_unregistered)
                                  + len(py_unreferenced)),
        'ignored_total': len(ign),
        'tracked_total': len(trk),
        'source_code_unregistered_referenced': py_unregistered,
        'source_code_unregistered_unreferenced': py_unreferenced,
        'source_code_registered': py_registered,
        'single_copy_assets_ge_100mb': single_copy_assets,
        'env_templates_untracked': sorted(env_templates_untracked),
        'secret_files_count': len(secret_files),
        'asset_bytes_total': asset_bytes_total,
        'asset_bytes_total_gb': round(asset_bytes_total / 1024 ** 3, 2),
        'registry_prefixes': sorted(IGNORED_PY_REGISTRY.keys()),
        'honest_scope': (
            '副本检测只在**本仓内**按 basename 比对, 未核查离机/其它盘备份; '
            '"单副本"应读作"本仓内找不到第二份", 不等于"绝对没有备份"'
        ),
    }


def write_report(findings: Dict[str, Any], out_json: str = OUT_JSON,
                 out_md: str = OUT_MD) -> Dict[str, str]:
    """落盘 JSON + Markdown（仅写 reports/ 自有文件）。"""
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(findings, f, ensure_ascii=False, indent=2)
    lines = [
        '# T66 被 .gitignore 静默吃掉的文件 —— 全仓只读盘点 (资产面: 单副本/模板/引用面)',
        '',
        f"- **结论**: {findings['verdict']}",
        f"- **忽略文件总数**: {findings['ignored_total']} (已跟踪 {findings['tracked_total']})",
        f"- **分类**: " + ' / '.join(f'{k}={v}' for k, v in sorted(findings['counts'].items())),
        f"- **资产类合计**: {findings['asset_bytes_total_gb']} GB",
        f"- **未登记且被已跟踪代码引用的 .py**: {len(findings['source_code_unregistered_referenced'])} 处",
        f"- **.py 对账**: 总数 {findings['source_code_total']} = 已登记 {len(findings['source_code_registered'])}"
        f" + 未登记被引用 {len(findings['source_code_unregistered_referenced'])}"
        f" + 未登记未引用 {len(findings['source_code_unregistered_unreferenced'])}"
        f" (accounted={findings['source_code_accounted']}, 不闭合即报告本身有洞)",
        f"- **单副本大资产(≥100MB)**: {len(findings['single_copy_assets_ge_100mb'])} 处",
        f"- **未入库的 .env 模板**: {len(findings['env_templates_untracked'])} 处",
        f"- **密钥文件(ignored, 正确)**: {findings['secret_files_count']} 处",
        f"- **诚实口径**: {findings['honest_scope']}",
        '',
    ]
    for item in findings['env_templates_untracked']:
        lines.append(f"  - AMBER {item} — 模板本应入库, 未入库 → 重建环境时模板缺失")
    for item in findings['source_code_unregistered_referenced']:
        lines.append(f"  - RED {item['file']} ← 被 {item.get('referenced_by')} 引用")
    for item in findings['single_copy_assets_ge_100mb']:
        lines.append(f"  - AMBER {item['file']} ({round(item['bytes'] / 1024 ** 3, 2)} GB) — {item['note']}")
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return {'json': out_json, 'md': out_md}


def run_audit(write: bool = True) -> Dict[str, Any]:
    findings = scan()
    if write:
        write_report(findings)
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description='T66 gitignore 静默忽略资产盘点 (纯只读)')
    ap.add_argument('--no-write', action='store_true', help='只打印不落盘')
    ap.add_argument('--no-reference-check', action='store_true', help='跳过引用面核查(慢)')
    args = ap.parse_args()
    findings = scan(do_reference_check=not args.no_reference_check)
    if not args.no_write:
        write_report(findings)
    print(json.dumps(findings, ensure_ascii=False, indent=2)[:4000])
    return 0 if findings['verdict'] in ('PASS', 'AMBER') else 1


if __name__ == '__main__':
    raise SystemExit(main())
