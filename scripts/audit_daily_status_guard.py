#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T49 — `daily_predictions.status` 陈旧派生字段的防回退静态守卫：基线只读盘点。

背景（T33 / T46）：
    `verification/ingest.py::ingest_daily_predictions` 用 `WHERE d.status='finished'`
    做入口门控，而 T33 机械证明全仓 **UPDATE_WRITER = 0**（无人把 `daily_predictions.status`
    翻成 finished）。故 T46 推荐 C 方案 = 门控改判 `m.status`（零写方、零撞锁、可自愈），
    代价是 `d.status` 退化为「陈旧派生字段」→ 未来读者若再拿它当"已完赛"判据会重踩。
    T46 验收项 A2 要求「门控改后 ingest 入口 WHERE 不再含 `d.status=`（静态扫描 + 单测）」，
    本脚本就是把 A2 落地前的**基线**量化：现在有几处、分别属于哪类、哪些是真货哪些噪声。

本脚本**只做只读盘点**，明确不做的四件事：
    不实现守卫 / 不修改 ingest.py / 不碰 events.db / 不写 verification.db。
产出 `reports/daily_status_guard_audit.{json,md}`。

口径要点（本轮最重要的两条，决定守卫怎么写才不假阳性）：
    1. `d.status` 是**歧义符号**：前端 `frontend/src/pages/Rollball/index.tsx:616`
       的 `d.status === 'scheduled'` 中 `d` 是本地比赛对象（同作用域 `d.home`/`d.away`），
       与 `daily_predictions d` **毫无关系**。守卫若按裸字符串扫会立刻假阳性 →
       判定式必须要求「SQL 上下文」（同行出现 `WHERE` / `daily_predictions` / `JOIN`）。
    2. `docs/` 命中不能当违规：整改规格本身要写 `d.status='finished'`（描述将要改的行），
       故文档命中一律按「是否把 `d.status` 当已完赛判据」分级，不进 FAIL。
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, 'reports')

# ── 扫描面 ──────────────────────────────────────────────────────────────────
EXCLUDE_DIRS = {
    '.git', '.venv', 'node_modules', 'archive', '__pycache__', '.pytest_cache',
    '.workbuddy', '.codebuddy', '.zcode', 'reports', 'deliverables', 'history',
    'browser_extension', 'logs',
}
# 本脚本自身与它的单测会提到全部命中字面量，必须自指排除（同 T33/T36 的 self-exclude 坑）
SELF_EXCLUDE = {
    'scripts/audit_daily_status_guard.py',
    'tests/test_audit_daily_status_guard.py',
}
# 生产代码面（守卫真正要守的）：允许扩展，但新增必须显式过人工复核
PROD_PY_SCAN = ['verification', 'pipeline', 'gq', 'core', 'data_collector', 'backend']

# 与 `daily_predictions.status` 直接相关的字面量族
D_STATUS_PAT = re.compile(r"d\.status")
SQL_CONTEXT_RE = re.compile(r"(?i)(where|daily_predictions|join\s+matches|from\s+daily_predictions)")
UPDATER_RE = re.compile(r"(?i)update\s+[\w\"]*\s*daily_predictions")
COMMENT_RE = re.compile(r"^\s*(#|\"\"\"|'''|\s*\*|\s*//)")
# 共享字面量豁免后缀：这些文件为了断言而必然包含被扫描串（同族于 T30-C 的注释噪声教训）
LITERAL_EXEMPT_SUFFIXES = (
    'scripts/audit_ingest_status_gate.py',
    'tests/test_audit_ingest_status_gate.py',
    'scripts/audit_candles_verdict_pipeline.py',
    'scripts/diag_candles_sample_hiatus.py',
    'scripts/audit_daily_status_guard.py',
    'tests/test_audit_daily_status_guard.py',
)


def iter_repo_files(exclude_dirs=None, exts=('.py', '.md')) -> Iterator[Tuple[str, str]]:
    """遍历仓库，返回 (relpath_with_forward_slashes, abspath)。跳过排除目录与自身。"""
    exclude_dirs = set(exclude_dirs or EXCLUDE_DIRS)
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for fn in filenames:
            if not fn.endswith(exts):
                continue
            ap = os.path.join(dirpath, fn)
            rp = os.path.relpath(ap, ROOT).replace(os.sep, '/')
            if rp in SELF_EXCLUDE:
                continue
            yield rp, ap


def read_lines(abspath: str) -> List[str]:
    """逐行读取并逐行解码（同 T44：日志/源码混合编码时整文件 decode 会静默漏匹配一半）。"""
    try:
        with open(abspath, 'r', encoding='utf-8', errors='replace') as fh:
            return fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def is_sql_daily_status_predicate(line: str) -> bool:
    """判定某行是否「把 daily_predictions.status 当条件」的 SQL 判定式。

    必须同时满足两条，方可避免 `d.status` 歧义符号（前端本地变量）的假阳性：
      (a) 出现 `d.status` 字面量；
      (b) 同行存在 SQL 上下文（WHERE / daily_predictions / JOIN matches）。
    """
    if not D_STATUS_PAT.search(line):
        return False
    return bool(SQL_CONTEXT_RE.search(line))


def classify_code_hit(relpath: str, line: str) -> str:
    """代码面命中分类。"""
    # 第二层排除（本轮最重要的防假阳性发现）：审计脚本与其单测**必须**写被扫描的
    # 字面量才能断言。T33 脚本已对自己 self-exclude，但它的字面量仍会出现在别的
    # 扫描器（含本脚本）的视野里 → 守卫必须带共享字面量豁免名单，否则一开闸就自抓。
    if any(relpath.endswith(sfx) for sfx in LITERAL_EXEMPT_SUFFIXES):
        return 'AUDIT_LITERAL'
    if COMMENT_RE.match(line):
        return 'COMMENT'
    if UPDATER_RE.search(line):
        return 'UPDATE_WRITER'
    if is_sql_daily_status_predicate(line):
        return 'INGEST_GATE'
    if 'status=excluded.status' in line or 'excluded.status' in line:
        return 'PASSTHROUGH'
    return 'OTHER'


def scan_code_surface() -> Dict[str, object]:
    """扫描代码面：`d.status` 命中分类 + `UPDATE`+`daily_predictions` 写方回归面。

    ⚠ 交叉踩坑(2026-09-27)：本函数注释/输出里**故意**不写连续的
    「UPDATE 动词 + daily_predictions 表名」的连续串（即在文中拆开写），
    因为 `scripts/audit_ingest_status_gate.py::find_status_writers()` 会全仓扫
    "含 daily_predictions 且含 UPDATE" 的行 —— 本脚本一旦出现 ``UPDATE`` 与
    `daily_predictions` 的连续串，就会把
    `tests/test_audit_ingest_status_gate.py::..._real_repo_has_no_update_flipper`
    从 pass 打成 fail。**自排除清单是每份审计各写一份的，新增审计会污染既有断言。**
    """
    hits: List[Dict[str, object]] = []
    updaters: List[Dict[str, object]] = []
    by_layer: Dict[str, int] = defaultdict(int)
    for rp, ap in iter_repo_files(exts=('.py',)):
        top = rp.split('/')[0]
        for idx, line in enumerate(read_lines(ap), start=1):
            if D_STATUS_PAT.search(line):
                kind = classify_code_hit(rp, line)
                by_layer[kind] += 1
                hits.append({'file': rp, 'line': idx, 'kind': kind,
                             'text': line.strip()[:160]})
            if UPDATER_RE.search(line):
                # 豁免判定：审计脚本/单测为了断言必须写 UPDATE 字面量 → 不是真写方。
                # 不加这条会把「测试里写断言」读成「生产在写」，从而把结论从 0 翻成 4。
                real = not any(rp.endswith(sfx) for sfx in LITERAL_EXEMPT_SUFFIXES)
                updaters.append({'file': rp, 'line': idx, 'text': line.strip()[:160],
                                 'is_literal_only': not real})
    hits.sort(key=lambda h: (h['file'], h['line']))
    return {'hits': hits, 'hit_count': len(hits), 'by_kind': dict(by_layer),
            'update_writers': updaters, 'update_writer_count': len(updaters)}


def scan_docs_surface() -> Dict[str, object]:
    """文档面盘点：把 `d.status` 当「已完赛」判据的表述（须配 stale 派生字段标记）。

    分类：
      SPEC_FIX   —— 整改规格/队列条目在描述**将要改的那一行**（整改方案本身必须引用现状串），
                    天然会命中，属豁免面，但必须能被人一眼看出是"描述"而非"断言"；
      DOC_CLAIM  —— 把 `d.status`/`daily_predictions.status` 当作事实判据陈述；
      UNRELATED  —— 命中串实际指 `matches.status`（如 知识库 里两处），不属本守卫范围。
    """
    claims: List[Dict[str, object]] = []
    unrelated: List[Dict[str, object]] = []
    exempt = 0
    for rp, ap in iter_repo_files(exts=('.md',)):
        if not rp.startswith('docs/'):
            continue
        for idx, line in enumerate(read_lines(ap), start=1):
            # 两级触发，避免把「只是提到这张表」的普通行拉进 stale 清单：
            #   TIGHT = 直接出现 d.status / daily_predictions.status 字面量
            #   LOOSE = 提到 daily_predictions 且同现 status/finished（需人工确认）
            tight = bool(D_STATUS_PAT.search(line)) or 'daily_predictions.status' in line
            if not tight and not ('daily_predictions' in line and
                                  re.search(r"(?i)status|finished", line)):
                continue
            # 无关判定：同一行出现 matches 而不出现 daily_predictions → 实指 matches.status
            if re.search(r"matches", line) and 'daily_predictions' not in line:
                unrelated.append({'file': rp, 'line': idx, 'text': line.strip()[:160]})
                continue
            # 豁免判定：整改规格（路径含 P-ENG|P-TEST|P-SEC 或队列条目）——
            # 注意只豁免「提到整改动作」的行，不整文件豁免，否则会把真声明一起豁免掉
            if re.search(r"P-ENG|P-TEST|P-SEC|AUTONOMOUS_BACKLOG", rp) and \
                    re.search(r"(?i)(改成|改判|→|改回|不再|翻|门控|守卫|UPDATE)", line):
                exempt += 1
                continue
            claims.append({'file': rp, 'line': idx, 'text': line.strip()[:160],
                           'mentions_finished': ("finished" in line.lower())})
    return {'stale_derived_claims': claims, 'claim_count': len(claims),
            'exempt_spec_mentions': exempt, 'unrelated_matches_status': unrelated,
            'unrelated_count': len(unrelated)}


GUARD_MECH_PAT = re.compile(r"(?i)(守卫|fail-closed|禁止|不得|违规)")
# ⚠ 勘误(2026-09-27)：初版漏 re.M，导致 `^` 只匹配整串开头 → 显式清单护栏数被读成 0
# （与 T30-D「边界协议」同族坑：`\b`/`^` 少一个标志就静默漏判，且这次是**反过来少报**）
SCANLIST_PAT = re.compile(r"^\s*(SCAN_FILES|BANNED|SCAN_PATHS|SCAN_TARGETS)\s*[=:]", re.M)


def catalog_guard_mechanisms() -> Dict[str, object]:
    """盘点现有静态守卫的**机制**，为 T49 复用它们提供先例依据。"""
    mechs: List[Dict[str, object]] = []
    for rp, ap in iter_repo_files(exts=('.py',)):
        if not rp.startswith('tests/'):
            continue
        src = '\n'.join(read_lines(ap))
        if not GUARD_MECH_PAT.search(src):
            continue
        uses_ast = 'import ast' in src or 'ast.parse' in src
        uses_regex_src = 'open(' in src and 're.' in src
        has_scanlist = bool(SCANLIST_PAT.search(src))
        mechs.append({'file': rp, 'ast': uses_ast, 'regex_source_scan': uses_regex_src,
                      'explicit_scanlist': has_scanlist,
                      'bytes': len(src.encode('utf-8', errors='replace'))})
    with_scanlist = [m for m in mechs if m['explicit_scanlist']]
    return {'guards': mechs, 'count': len(mechs),
            'with_explicit_scanlist': len(with_scanlist),
            'ast_based': sum(1 for m in mechs if m['ast']),
            'regex_source_based': sum(1 for m in mechs if m['regex_source_scan'])}


def run_audit() -> Dict[str, object]:
    """主流程：汇总三大面 + 一条诚实结论，落盘 JSON/MD。 """
    code = scan_code_surface()
    docs = scan_docs_surface()
    guards = catalog_guard_mechanisms()
    ingest_gate = [h for h in code['hits'] if h['kind'] == 'INGEST_GATE']

    summary = {
        'as_of_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'qa': 'T49 daily_predictions.status 防回退静态守卫基线（只读，不实现守卫）',
        'code_hit_count': code['hit_count'],
        'code_by_kind': code['by_kind'],
        'ingest_gate_lines': ingest_gate,
        'update_writer_count': code['update_writer_count'],
        'update_writers': code['update_writers'],
        'real_update_writer_count': sum(1 for u in code['update_writers']
                                        if not u.get('is_literal_only')),
        'docs_stale_derived_claims': docs['claim_count'],
        'docs_exempt_spec_mentions': docs['exempt_spec_mentions'],
        'docs_unrelated_matches_status': docs['unrelated_count'],
        'guard_mechanisms': guards,
        'verdict': None,
    }
    # 诚实结论：基线是「守卫尚未落地且 ingest 门控仍在用 d.status」
    summary['verdict'] = {
        'guard_implemented': False,
        'ingest_gate_uses_d_status': len(ingest_gate) > 0,
        # 只有「非审计/测试字面量」的写方才算真写方：全部是字面量 → 仍是 0（复核 T33）
        'update_writer_exists': sum(1 for u in code['update_writers']
                                    if not u.get('is_literal_only')) > 0,
        'guard_would_fail_today': len(ingest_gate) > 0,
        'note': ('A2 守卫落地前，ingest 门控仍判 d.status；守卫应为「防回退」语义'
                 '（R-A 改门控后转绿，将来有人改回即 FAIL），而非今天的 FAIL 门禁。'),
    }
    os.makedirs(REPORTS, exist_ok=True)
    jpath = os.path.join(REPORTS, 'daily_status_guard_audit.json')
    with open(jpath, 'w', encoding='utf-8') as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    lines = [
        '# T49 `daily_predictions.status` 防回退静态守卫 — 基线只读盘点',
        '',
        f'- as_of(UTC): {summary["as_of_utc"]}',
        '- 只读：未实现守卫 / 未改 ingest.py / 未碰 events.db / 未写 verification.db',
        '',
        '## 1. 代码面（守卫真正要守的）',
        f'- `d.status` 命中 {code["hit_count"]} 处，分类 {code["by_kind"]}',
        f'- **INGEST_GATE（真货）{len(ingest_gate)} 处**:',
    ]
    for h in ingest_gate:
        lines.append(f'  - `{h["file"]}:{h["line"]}` — {h["text"]}')
    # 注：此处同样回避连续字面量，理由见 scan_code_surface 的 docstring（交叉污染）
    lines.append(f'- `UPDATE` + `daily_predictions` 写方 **{code["update_writer_count"]} 处**'
                 '（= T33 UPDATE_WRITER=0 的复核；>0 即 A/B 方案回退）')
    for u in code['update_writers']:
        lines.append(f'  - `{u["file"]}:{u["line"]}` — {u["text"]}')
    lines.append('')
    lines.append('## 2. 文档面（须配 stale 派生字段标记的表述）')
    lines.append(f'- 豁免面（整改规格/队列条目，描述将要改的那一行）: {docs["exempt_spec_mentions"]}')
    lines.append(f'- 无关面（实指 `matches.status`/回填语境）: {docs["unrelated_count"]}')
    lines.append(f'- **需人工标注的 stale_derived 声明 {docs["claim_count"]} 处**：')
    for c in docs['stale_derived_claims']:
        lines.append(f'  - `{c["file"]}:{c["line"]}` — {c["text"]}')
    lines.append('')
    lines.append('## 3. 现有静态守卫机制（复用先例）')
    lines.append(f'- 含守卫语义的测试 {guards["count"]} 个；显式 SCAN_FILES/SCAN_PATHS 清单 '
                 f'{guards["with_explicit_scanlist"]} 个；AST 型 {guards["ast_based"]}；'
                 f'源码正则型 {guards["regex_source_based"]}')
    lines.append('')
    lines.append('## 4. 结论（诚实）')
    v = summary['verdict']
    lines.append(f'- 守卫尚未实现：`{v["guard_implemented"]}`')
    lines.append(f'- 当前 ingest 门控仍判 `d.status`: `{v["ingest_gate_uses_d_status"]}`'
                 f'（A2 落地后应转 False）')
    lines.append(f'- 现有 UPDATE 写方存在: `{v["update_writer_exists"]}`')
    lines.append(f'- 若今天就把守卫开成 FAIL: `{v["guard_would_fail_today"]}` → '
                 '故守卫语义定为**防回退**而非当前门禁（详见规格文档）。')
    mpath = os.path.join(REPORTS, 'daily_status_guard_audit.md')
    with open(mpath, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    summary['outputs'] = {'json': jpath, 'md': mpath}
    return summary


if __name__ == '__main__':
    res = run_audit()
    print('[T49] code_hits=%d ingest_gate=%d updaters=%d docs_claims=%d guards=%d' % (
        res['code_hit_count'], len(res['ingest_gate_lines']),
        res['update_writer_count'], res['docs_stale_derived_claims'],
        res['guard_mechanisms']['count']))
    print('[T49] ->', res['outputs']['json'])
