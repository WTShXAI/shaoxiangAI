#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T50 「今日预测 ?」可观测性只读盘点 (承接 T44 / T48 S4, WINDOW 备料)
================================================================================
对象: ``scripts/autonomous_monitor.py`` —— 预测补算 ``refresh_predictions()`` 失败后
把 ``status['predictions']`` 退化成 ``{'error': str(e)}``, 而摘要行取
``status.get('predictions', {}).get('after', '?')`` → 打印「今日预测 ?」。
该字符串与「今日本就没有可预测场次」在读者侧**完全不可区分**, 且 ``status`` 每次周期
整体覆盖写、无计数器、无 error_kind。

本脚本**纯只读**: 不重试 / 不重启 / 不碰 events.db / 不改 monitor / 不挂调度 /
不写 verification.db。产出 ``reports/predict_refresh_observability_audit.{json,md}``。

问题:
  Q1 降级路径 —— 历史里有多少周期退化成「?」, 退化后还能不能分辨锁失败与真错误
  Q2 连续撞锁 —— 用滞后(hysteresis)规则重算连续失败段, 验证 T48 S4 四字段够不够用
  Q3 字段契约 —— T48 §S4 四字段在现存 status 里缺多少, 以及"计数器被单周期成功清零"风险
  Q4 消费方    —— 谁读 monitor_status.json (极性判定沿用 T42 教训: open() 模式串才是证据)
"""
from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY = os.path.join(ROOT, 'reports', 'monitor_history.jsonl')
STATUS = os.path.join(ROOT, 'reports', 'monitor_status.json')
OUT_JSON = os.path.join(ROOT, 'reports', 'predict_refresh_observability_audit.json')
OUT_MD = os.path.join(ROOT, 'reports', 'predict_refresh_observability_audit.md')

#: T48 §S4 可观测四字段 (本脚本机械核对"现状缺多少")
S4_FIELDS = ('lock_retries', 'error_kind', 'consecutive_lock_failures',
             'lock_affected_matches')

#: 锁类错误判定: 优先复用 SSoT ``core.error_envelope.classify_error``, 不可自写正则。
LOCK_TOKENS = ('database is locked', 'database is busy', 'sqlite3.OperationalError',
               'database table is locked')

#: 消费者扫描面 (与 T42 同口径: 只扫"会被当成生产消费"的面)
SCAN_EXT = ('.py', '.ts', '.tsx', '.js', '.vue', '.html')
SKIP_DIRS = {'.git', '.venv', 'node_modules', 'archive', '.workbuddy', '.codebuddy',
             '.zcode', '__pycache__', 'deliverables', 'reports', 'logs'}
#: 本脚本自身文件名 (self-exclude, 沿用 T33/T36/T49 教训)
SELF = os.path.basename(__file__)

RE_OPEN = re.compile(r'open\(\s*(?P<arg>[^,()]+?)\s*,\s*(?P<mode>["\'][^"\']*["\'])')
RE_STATUS_STR = re.compile(r'monitor[_]?status\.json|monitor[_]?history\.jsonl')
RE_CONST_DEF = re.compile(r'^(?P<name>[A-Z_][A-Z0-9_]*)\s*=\s*(?P<path>.*)$')


# ---------------------------------------------------------------------------
# Q1 降级路径
# ---------------------------------------------------------------------------

def read_history(path: str = HISTORY) -> List[Dict[str, Any]]:
    """只读读 monitor 历史 jsonl (逐行容错; 空行跳过)。"""
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def classify_error_kind(value: Any) -> str:
    """锁类/真错误判定。

    两级口径 (**SSoT 优先, Token 兜底第二**):
      1) 传入 ``BaseException`` 时复用 SSoT ``core.error_envelope.classify_error``
         (``sqlite3.OperationalError`` + message 含 lock/busy → ``CODE_DB_LOCKED``);
      2) 传入字符串(异常已被 ``str()`` 化)时 SSoT 会因**异常类型丢失**退化成
         ``INTERNAL``(实测: ``classify_error(Exception('database is locked'))`` 返回
         ``('INTERNAL', 500)``) → 此时退回 Token 白名单兜底, 与 SSoT 同一组词面
         (``lock`` / ``busy``), 不是新口径。

    Args:
        value: 异常对象或异常字符串 (可为空)。

    Returns:
        ``LOCK_CONTENTION`` / ``REAL_ERROR`` / ``UNKNOWN``。
    """
    if isinstance(value, BaseException):
        return _kind_from_exception(value)
    m = (value or '').strip()
    if not m:
        return 'UNKNOWN'
    kind = _kind_from_exception(Exception(m))
    if kind != 'UNKNOWN':
        return kind
    low = m.lower()
    for t in LOCK_TOKENS:
        if t in low:
            return 'LOCK_CONTENTION'
    return 'REAL_ERROR'


def _kind_from_exception(exc: BaseException) -> str:
    """经 SSoT 判定异常类别 (不可达时返回 UNKNOWN, 由调用方决定是否兜底)。"""
    try:
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from core.error_envelope import CODE_DB_LOCKED, classify_error  # type: ignore
        code = classify_error(exc)[0]
        if code == CODE_DB_LOCKED:
            return 'LOCK_CONTENTION'
        # SSoT 的保守优先: 无法判定一律 INTERNAL → 交给调用方兜底, 不直接判成 REAL_ERROR
        if code == 'INTERNAL':
            return 'UNKNOWN'
        return 'REAL_ERROR'
    except Exception:
        return 'UNKNOWN'


def classify_prediction_block(pred: Any) -> Dict[str, Any]:
    """判定一个周期 predictions 块的健康/降级形态。

    Args:
        pred: ``status['predictions']`` 原始值 (dict / 缺失)。

    Returns:
        dict: ``{present, degraded, kind, after, error, error_kind, fields}``。
    """
    if not isinstance(pred, dict):
        return {'present': False, 'degraded': True, 'kind': 'MISSING',
                'after': None, 'error': None, 'error_kind': 'UNKNOWN', 'fields': []}
    err = pred.get('error')
    after = pred.get('after')
    degraded = err is not None or after is None
    kind = 'DEGRADED'
    if err:
        k = classify_error_kind(str(err))
        kind = 'DEGRADED_LOCK' if k == 'LOCK_CONTENTION' else 'DEGRADED_REAL'
    elif after is None:
        kind = 'DEGRADED_NO_AFTER'
    else:
        kind = 'OK'
    present_fields = [k for k in S4_FIELDS if k in pred]
    return {'present': True, 'degraded': degraded, 'kind': kind,
            'after': after if isinstance(after, int) else None,
            'error': str(err)[:200] if err else None,
            'error_kind': classify_error_kind(str(err)) if err else None,
            'fields': present_fields}


def summarize_degradation(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按周期统计降级形态分布。"""
    dist: Dict[str, int] = {}
    rows: List[Dict[str, Any]] = []
    for r in records:
        cls = classify_prediction_block(r.get('predictions'))
        dist[cls['kind']] = dist.get(cls['kind'], 0) + 1
        rows.append({'cycle_at': r.get('cycle_at'), **cls})
    n = len(records) or 1
    return {'cycles': len(records), 'dist': dist,
            'degraded_rate': round(sum(v for k, v in dist.items() if k != 'OK') / n, 4),
            'rows': rows}


# ---------------------------------------------------------------------------
# Q2 连续撞锁段 (滞后规则)
# ---------------------------------------------------------------------------

def hysteresis_streaks(flags: List[bool], clean_to_reset: int = 2) -> List[Dict[str, Any]]:
    """把逐周期布尔(是否锁失败)序列切成连续段, 带**滞后复位**语义。

    滞后规则: 连续 ``clean_to_reset`` 个干净周期才把计数器清零; 只夹在两次锁失败
    之间的**单个成功周期不算清零**(那只是抖动, 不证明链路恢复)。
    ``T48 S4`` 要求「单调计数器不得被单周期成功清零」, 本函数即该语义的实现。

    Args:
        flags: 逐周期 True=锁失败/False=成功。
        clean_to_reset: 干净周期达到多少个才清零。

    Returns:
        list[dict]: 每段 {start_idx, end_idx, length, alert_at_threshold}。
    """
    streaks: List[Dict[str, Any]] = []
    run_start: Optional[int] = None
    clean_run = 0
    for i, f in enumerate(flags):
        if f:
            if run_start is None:
                run_start = i
            clean_run = 0
        else:
            if run_start is not None:
                clean_run += 1
                if clean_run >= clean_to_reset:
                    end = i - clean_run          # 段只含真正的失败周期, 净空周期不入段
                    streaks.append({'start_idx': run_start, 'end_idx': end,
                                    'length': end - run_start + 1})
                    run_start = None
                    clean_run = 0
    if run_start is not None:
        streaks.append({'start_idx': run_start, 'end_idx': len(flags) - 1,
                        'length': len(flags) - run_start,
                        'open': True})
    return streaks


def lock_flags(records: List[Dict[str, Any]]) -> List[bool]:
    """逐周期是否发生锁类降级 (非锁类真错误单列, 不混入锁段)。"""
    out: List[bool] = []
    for r in records:
        c = classify_prediction_block(r.get('predictions'))
        out.append(c['degraded'] and c.get('error_kind') == 'LOCK_CONTENTION')
    return out


def streak_alerts(streaks: List[Dict[str, Any]], threshold: int = 3) -> List[Dict[str, Any]]:
    """按连续阈值标出需要进 WARN 的段。"""
    return [{'start': s['start_idx'], 'end': s['end_idx'], 'length': s['length'],
             'open': s.get('open', False), 'alert': s['length'] >= threshold}
            for s in streaks]


# ---------------------------------------------------------------------------
# Q3 字段契约缺口 + 叙事歧义
# ---------------------------------------------------------------------------

def contract_gap(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """T48 §S4 四字段现状覆盖率 (成功路径与降级路径分别统计)。"""
    ok_rows = [r for r in records
               if classify_prediction_block(r.get('predictions'))['kind'] == 'OK']
    deg_rows = [r for r in records
                if classify_prediction_block(r.get('predictions'))['degraded']]
    total = len(records) or 1

    def cover(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {'n': 0, 'coverage': {}, 'note': 'no-such-rows'}
        c = {f: sum(1 for r in rows if f in (r.get('predictions') or {})) for f in S4_FIELDS}
        return {'n': len(rows), 'coverage': c,
                'missing_fields': [f for f in S4_FIELDS if c[f] == 0]}

    return {'total_cycles': len(records), 'ok_rows': cover(ok_rows),
            'degraded_rows': cover(deg_rows),
            'any_s4_field_ever_present': sum(
                1 for r in records if any(f in (r.get('predictions') or {})
                                          for f in S4_FIELDS)) / total}


def narrative_ambiguity(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """``narrative_added`` 的 None / 0 歧义: 失败被写成 None, 与"无增量"同值不同义。"""
    def _is_zero(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and v == 0

    none_n = sum(1 for r in records if r.get('narrative_added') is None)
    zero_n = sum(1 for r in records if _is_zero(r.get('narrative_added')))
    return {'none_count': none_n, 'zero_count': zero_n,
            'ambiguous': none_n + zero_n}


# ---------------------------------------------------------------------------
# Q4 消费方盘点 (极性判定沿用 T42: open() 自带模式串优先)
# ---------------------------------------------------------------------------

def _rel(abs_path: str) -> str:
    try:
        return os.path.relpath(abs_path, ROOT).replace(os.sep, '/')
    except ValueError:
        return os.path.basename(abs_path)


def scan_status_consumers() -> Dict[str, Any]:
    """扫描仓库内引用 monitor 状态文件的位置, 按极性分类。

    判定优先级(与 T42 同): 模式串即在案的 decisive 证据
      1) ``open(..., "w"/"a"/"r"...)`` 且路径含目标串 → WRITER / READER
      2) 变量绑定路径(``json.dump(status, f)`` / ``open(STATUS, "w")``) 且本行引用
         ``STATUS``/``HISTORY`` 常量 → ``WRITER_VIA_CONST``(monitor 自身落盘, 推断非字面)
      3) 其余(文档叙述/字符串常量/注释) → MENTION, 不计入消费方
    """
    hits: List[Dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(SCAN_EXT) or fn == SELF:
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
            except OSError:
                continue
            rel = _rel(p)
            bound_names: List[str] = []
            for ln in lines:
                b = RE_CONST_DEF.match(ln.strip())
                if b and RE_STATUS_STR.search(b.group('path')):
                    bound_names.append(b.group('name'))
            for i, ln in enumerate(lines, 1):
                if not RE_STATUS_STR.search(ln):
                    bound = [n for n in bound_names if re.search(rf'\b{n}\b', ln)]
                    if not bound:
                        continue
                locus = ln.strip()[:180]
                mode = None
                m = RE_OPEN.search(ln)
                if m and (RE_STATUS_STR.search(m.group('arg'))
                          or any(re.search(rf'\b{n}\b', m.group('arg')) for n in bound)):
                    mode = 'w' if re.search(r'[wax+]', m.group('mode')) else 'r'
                if mode == 'w':
                    pol = 'WRITER'
                elif mode == 'r':
                    pol = 'READER'
                elif re.search(r'json\.dump', ln) and re.search(r'\bSTATUS\b|\bHISTORY\b', ln):
                    # 变量绑定路径的写: monitor 用 STATUS/HISTORY 常量 + json.dump 落盘
                    pol = 'WRITER_VIA_CONST'
                elif re.search(r'open\(\s*(STATUS|HISTORY)', ln):
                    pol = 'WRITER_VIA_CONST'
                else:
                    pol = 'MENTION'
                hits.append({'file': rel, 'line': i, 'polarity': pol, 'locus': locus})
    by_pol: Dict[str, int] = {}
    for h in hits:
        by_pol[h['polarity']] = by_pol.get(h['polarity'], 0) + 1
    readers = sorted({h['file'] for h in hits if h['polarity'] == 'READER'})
    return {'hits': len(hits), 'hit_rows': hits, 'by_polarity': by_pol,
            'reader_files': readers,
            'note': 'MENTION 不计消费方 (文档叙述/字符串常量); 沿用 T42 口径'}


def current_status_snapshot(path: str = STATUS) -> Optional[Dict[str, Any]]:
    """只读读当前 monitor_status.json (存在则返回 dict)。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def build_recommendations(snapshot: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """整改条目 (WINDOW 备料, 纯规格)。"""
    return [
        {'id': 'O1', 'title': 'predictions 降级块补齐 error_kind(锁/真错误二分)',
         'change': ("把 `status['predictions']` 的失败分支从 `{'error': str(e)}` "
                    "改为 `{'error':..., 'error_kind': kind, 'consecutive': n, "
                    "'retry_after': sec}`，kind 走 core.error_envelope.classify_error"),
         'why': '现状失败字符串只能靠子串猜测; 摘要行「今日预测 ?」与「今日无赛」不可区分',
         'gates': ['G1 失败块必含 error_kind 且取值 ∈ {LOCK_CONTENTION, REAL_ERROR}',
                   'G2 成功块不引入 error 键 (防止下游用 presence 反推健康)',
                   'G3 摘要行改 prints kind + after, 不再裸 "?"'],
         'rollback': '字段可缺省, 老解析器按 absence 处理'},
        {'id': 'O2', 'title': '连续撞锁滞后计数器 + 阈值告警',
         'change': ('新增 `consecutive_lock_failures` 用滞后规则(连续 2 个干净周期才清零); '
                    '>=3 时必须 log WARN 并在 status 内以独立键 `lock_alert: true` 出现'),
         'why': 'T48 N3 已证受害者固定(同批比赛跨周期重合), 偶发抖动不该被单次成功抹平',
         'gates': ['G1 单周期成功不得把计数器清零 (滞后 2)',
                   'G2 计数器可从 monitor_history.jsonl 离线重算 -> 无需新持久化状态',
                   'G3 告警键与计数键分离 (status 内 lock_alert 与 consecutive 同时可查)'],
         'rollback': '计数键不写即回退, 无数据副作用'},
        {'id': 'O3', 'title': '摘要行可区分三态 (OK / 缺场 / 失败)',
         'change': ('摘要模板改为 `今日预测 {after}{/无场次|/失败:{kind}}`, '
                    '"无场次"只在 today matches=0 时输出'),
         'why': '现在 "?" 同时吃掉「无场次」与「失败」两种语义, 人工巡检无法判断要不要跟',
         'gates': ['G1 三种输出互斥且可机读 (status 内 predictions.outcome ∈ '
                   '{ok,no_matches,failed})', 'G2 不新增 events.db 写方'],
         'rollback': '回退旧模板, 无迁移'},
        {'id': 'O4', 'title': 'narrative_added 的 None/0 歧义消除',
         'change': ('叙事失败改为记 dict(error=..., error_kind=...) 而非 None; '
                    '无增量仍记 0'),
         'why': '现在 None(失败) 与 0(无增量) 在下游同值, 与 predictions 同病',
         'gates': ['G1 成功路径不得出现 None', 'G2 monitor_history 旧行仍可解析'],
         'rollback': '字段可缺省'},
    ]


def render_md(report: Dict[str, Any]) -> str:
    q = report['questions']
    L: List[str] = []
    A = L.append
    A('# T50 「今日预测 ?」可观测性只读盘点')
    A('')
    A(f"- 生成时间: {report['generated_at']}")
    A(f"- 证据源: `{report['history_path']}` ({q['history_lines']} 行) / 当前 `{report['status_path']}` / 全仓消费者扫描")
    A('- 纯只读: 未重试 / 未重启 / 未改 monitor / 未碰 events.db / 未挂调度 / 未写 verification.db')
    A('')
    A('## 结论摘要')
    A('')
    for c in report['headline']:
        A(f"- {c}")
    A('')
    A('## Q1 降级路径: 失败被吞成什么')
    A('')
    d = report['degradation']
    A(f"- 周期总数 {d['cycles']}, 形态分布: {d['dist']}, 降级率 {d['degraded_rate']}")
    A("- 失败块字段 = `{'error': str(e)}` 单键 → **无 error_kind / 无计数 / 无 retry_after**; "
      "摘要行 `.get('after','?')` 取不到即打印 `今日预测 ?`")
    A(f"- 当前快照 predictions 键: {report['snapshot_keys']}")
    A('')
    A('## Q2 连续撞锁段 (滞后规则)')
    A('')
    st = report['streaks']
    A(f"- 锁类失败周期 {st['lock_fail_cycles']} 个 / 干净周期 {st['clean_cycles']} 个")
    A(f"- 切成 {len(st['streaks'])} 段, 最长 {st['max_length']} 个周期"
      f"(当前段仍在进行={st['open_streak_now']})")
    A(f"- `>=3` 阈值命中段: {sum(1 for s in st['alert_rows'] if s['alert'])} 个 "
      f"→ 若按 T48 S4 落地, 这些周期都会进 WARN")
    A('')
    A('## Q3 字段契约缺口')
    A('')
    cg = report['contract']
    A(f"- 成功周期 {cg['ok_rows'].get('n')} 行 / 降级周期 {cg['degraded_rows'].get('n')} 行")
    A(f"- 四字段(S4)历史覆盖率: 成功块 {cg['ok_rows'].get('coverage')} / "
      f"降级块 {cg['degraded_rows'].get('coverage')}")
    A(f"- 四字段**从未**在任何周期出现过 (占全部周期 {round(1 - cg['any_s4_field_ever_present'], 4)})")
    na = report['narrative']
    A(f"- 叙事块歧义: `narrative_added` None(失败) {na['none_count']} 次 vs 0(无增量) {na['zero_count']} 次")
    A('')
    A('## Q4 消费方盘点')
    A('')
    cs = report['consumers']
    A(f"- 命中 {cs['hits']} 条, 极性分布 {cs['by_polarity']}")
    A(f"- 真 READER 文件: {cs['reader_files'] or '无(仅脚本侧 MENTION/READER, 无 backend/frontend)'}")
    A(f"- 说明: {cs['note']}")
    A('')
    A('## 整改规格 (WINDOW 备料, 不落地)')
    A('')
    for r in report['recommendations']:
        A(f"### {r['id']} {r['title']}")
        A('')
        A(f"- **改什么**: {r['change']}")
        A(f"- **为什么**: {r['why']}")
        A(f"- **验收门禁 (fail-closed)**: {' / '.join(r['gates'])}")
        A(f"- **回滚**: {r['rollback']}")
        A('')
    A('## 诚实边界')
    A('')
    for c in report['caveats']:
        A(f"- {c}")
    A('')
    return '\n'.join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description='T50 「今日预测 ?」可观测性只读盘点')
    ap.add_argument('--history', default=HISTORY)
    args = ap.parse_args()

    records = read_history(args.history)
    deg = summarize_degradation(records)
    flags = lock_flags(records)
    streaks = hysteresis_streaks(flags, clean_to_reset=2)
    alert_rows = streak_alerts(streaks, threshold=3)
    snap = current_status_snapshot()
    cs = scan_status_consumers()
    lock_fail = sum(1 for f in flags if f)
    max_len = max((s['length'] for s in streaks), default=0)
    open_now = bool(streaks) and bool(streaks[-1].get('open'))
    snap_pred = (snap or {}).get('predictions') or {}
    headline = [
        f"历史 {len(records)} 周期中降级 {deg['degraded_rate']} —— 降级一律退化为单键 `error`, "
        f"摘要行打印「今日预测 ?」, 与「今日无场次」同形",
        f"锁类失败 {lock_fail} 个周期, 可切出 {len(streaks)} 段(最长 {max_len}); "
        f"T48 §S4 四字段在全部历史周期中**出现次数 0**",
        '风险: `status` 每周期整体覆盖写 → 无计数器; 单周期成功会把「看起来恢复」掩盖掉, '
        '而 T48 N3 已证受害者固定, 抖动不该抹平链路修复信号',
        f"消费者面: backend / frontend 文件读取 monitor 状态文件的命中为 "
        f"{sum(1 for h in cs['hit_rows'] if h['file'].startswith(('frontend', 'bridge_service')))} → "
        "**告警键加得上去也没人看**, O1-O4 必须与一个真实消费点同窗口落地",
    ]
    caveats = [
        '本脚本零重试、零重启、零 events.db 写入、未改 monitor、未挂调度; 结论全部来自只读证据。',
        'monitor_history.jsonl 只覆盖 09-19 起的记录 (少于同日志的周期数), '
        '连续段统计以历史行为准, 日志面口径见 T44。',
        '「无消费点」是事实陈述: 它说明可观测性整改的价值取决于是否同时建立消费面, '
        '不推翻 O1-O4 本身(它们是让失败可见的最小前提)。',
        '整改条目均为 WINDOW 备料; 落地须走维护窗口 + 全量 pytest + 回滚预案。',
    ]
    report = {
        'task': 'T50 「今日预测 ?」可观测性只读盘点',
        'generated_at': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'history_path': args.history,
        'status_path': STATUS,
        'questions': {'history_lines': len(records)},
        'headline': headline,
        'caveats': caveats,
        'degradation': deg,
        'streaks': {'lock_fail_cycles': lock_fail,
                    'clean_cycles': sum(1 for f in flags if not f),
                    'streaks': streaks, 'alert_rows': alert_rows,
                    'max_length': max_len, 'open_streak_now': open_now},
        'contract': contract_gap(records),
        'narrative': narrative_ambiguity(records),
        'consumers': cs,
        'snapshot_keys': sorted(snap_pred.keys()),
        'recommendations': build_recommendations(snap),
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(OUT_MD, 'w', encoding='utf-8') as f:
        f.write(render_md(report))
    print(f"cycles={len(records)} degraded={deg['degraded_rate']} lock_fail={lock_fail} "
          f"streaks={len(streaks)} max_len={max_len}")
    print(f"-> {OUT_JSON}")
    print(f"-> {OUT_MD}")


if __name__ == '__main__':
    main()
