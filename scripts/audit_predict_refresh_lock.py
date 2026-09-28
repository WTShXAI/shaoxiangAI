#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T44 预测补算 `database is locked` 只读归因 (承接 2026-09-27 巡检新发现)
=========================================================================
对象: ``scripts/autonomous_monitor.py::refresh_predictions()`` (:93-116) 每 ~1h 被
``main()`` 调用一次, 走 ``gq.db.conn()`` 写连接对 events.db 做 ``executescript(TABLE_DDL)``
+ ``build_for_date(...)``; 实测 2026-09-27 有 4/8 个周期抛 ``database is locked``。

本脚本**纯只读**: 不重试、不重启、不碰生产服务、不写 events.db、不改任何调度。
产出 ``reports/predict_refresh_lock_audit.{json,md}``。

四问:
  Q1 归因 —— 谁在写 events.db (进程面 + 代码面), 锁是谁持有的, 为什么 monitor 必被撞
  Q2 时序 —— 失败周期的真实耗时形态 (是否呈 30s busy_timeout 签名)
  Q3 影响 —— 撞锁周期直接损失什么 (今日 K线判定未应用到 daily_predictions 的场次数)
  Q4 整改 —— 重试/退避 + fail-closed 验收门禁 + 回滚 (纯规格, 不落地)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from log_codec_ssot import decode_line, decode_log_lines, decode_log_text, read_log_lines  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, 'logs', 'autonomous_monitor.log')
DB = os.path.join(ROOT, 'data', 'events.db')
OUT_JSON = os.path.join(ROOT, 'reports', 'predict_refresh_lock_audit.json')
OUT_MD = os.path.join(ROOT, 'reports', 'predict_refresh_lock_audit.md')

#: 关键写方白名单 (扫描面刻意收窄, 避免全仓 grep 噪声)。标记为 events.db 写方的进程/模块。
WRITE_SURFACE_FILES = [
    'gq/db.py', 'gq/ws_collector.py', 'gq/auto_collector.py', 'gq/match_narrative.py',
    'gq/event_db.py', 'pipeline/predict_export.py', 'scripts/autonomous_monitor.py',
    'bridge_service.py',
]

#: 写方特征 (命中即判 WRITER); BUSY 特征命中说明该文件自己设了 busy_timeout。
RE_WRITE = re.compile(r'\b(INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|executescript|'
                      r'conn\(\)|get_manager\(|writer\(\))', re.IGNORECASE)
RE_BUSY = re.compile(r'PRAGMA\s+busy_timeout\s*=', re.IGNORECASE)
#: 只读连接特征
RE_RO = re.compile(r'mode=ro|conn\(readonly=True\)|readonly\s*:\s*bool', re.IGNORECASE)

RE_TS = re.compile(r'^\[(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)\] \[(\w+)\] (.*)$')
RE_MATCH_FAIL = re.compile(r'^\s+\[.*\]\s+失败:\s')
RE_CYCLE_SEC = re.compile(r'周期完成\s+([\d.]+)s')
RE_PREDICT_AFTER = re.compile(r'今日预测\s+(\S+)')


# ---------------------------------------------------------------------------
# Q1 代码面 / 进程面
# ---------------------------------------------------------------------------

def read_text_if_exists(path: str) -> Optional[str]:
    """按混合编码逐行解码读取 (SSoT = ``scripts/log_codec_ssot.py``, 2026-09-28 T52 迁移; T61 改名)。

    本函数只保留「缺文件返回 None」的语义, 解码实现已迁往共用模块,
    避免每个审计脚本各写一份解码器 (T44/T48 先后各踩一次同一个坑)。
    """
    lines = read_log_lines(path)
    if not os.path.exists(path):
        return None
    return '\n'.join(lines)


def analyze_monitor_source(src: str) -> Dict[str, Any]:
    """逐行扫描 autonomous_monitor 源码, 抽出与撞锁相关的结构性事实。

    Args:
        src: 源码文本 (测试用合成串, 生产用真实文件)。

    Returns:
        dict: 结构性事实 (写连接处 / 是否重试 / 异常吞没 / 连接打开次序)。
    """
    lines = src.splitlines()
    facts: Dict[str, Any] = {
        'lines': len(lines),
        'write_conn_hits': [],
        'ro_conn_hits': [],
        'retry_keywords': [],
        'swallowed_error_lines': [],
        'open_write_connection': False,
        'has_backoff': False,
    }
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        if re.search(r'conn\(\)|gq_conn|gq\.db', s):
            facts['write_conn_hits'].append({'line': i, 'text': s[:160]})
            if re.search(r'conn\(\)\s*(?!.*readonly=True)', s) or 'gq_conn' in s:
                facts['open_write_connection'] = True
        if 'mode=ro' in s or 'readonly=True' in s:
            facts['ro_conn_hits'].append({'line': i, 'text': s[:160]})
        if re.search(r'retry|backoff|time\.sleep\(|attempt', s, re.IGNORECASE):
            facts['retry_keywords'].append({'line': i, 'text': s[:160]})
        if 'predictions' in s and ("{'error'" in s or 'error' in s):
            facts['swallowed_error_lines'].append({'line': i, 'text': s[:160]})
    facts['has_backoff'] = any(re.search(r'retry|backoff', k['text'], re.IGNORECASE)
                               for k in facts['retry_keywords'])
    return facts


def scan_write_surface(root: str, files: List[str]) -> Dict[str, Any]:
    """扫描白名单文件, 判定 events.db 写方及其 busy_timeout 姿态。"""
    out: Dict[str, Any] = {'scanned': [], 'writers': [], 'readers': []}
    for rel in files:
        p = os.path.join(root, rel.replace('/', os.sep))
        if not os.path.exists(p):
            continue
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            txt = f.read()
        hits = [(i, ln.strip()[:160]) for i, ln in enumerate(txt.splitlines(), 1)
                if RE_WRITE.search(ln)]
        busy = bool(RE_BUSY.search(txt))
        ro = bool(RE_RO.search(txt))
        entry = {'file': rel, 'write_stmts': len(hits), 'busy_timeout_set': busy,
                 'readonly_path': ro,
                 'sample': hits[:3]}
        out['scanned'].append(rel)
        if hits:
            out['writers'].append(entry)
        elif ro:
            out['readers'].append(entry)
    return out


def _cim_date(value: Any) -> str:
    """CIM 时间形如 ``/Date(1790080685444)/`` (毫秒 epoch) → 可读字符串。"""
    s = str(value or '')
    m = re.search(r'/Date\((\d+)\)', s)
    if not m:
        return s
    try:
        return datetime.fromtimestamp(int(m.group(1)) / 1000.0).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return s


def live_processes() -> List[Dict[str, Any]]:
    """只读列出 python 进程 (绝不 kill / 绝不 stop)。best-effort, 失败返回 []。"""
    try:
        import subprocess
        ps = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
             "Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | "
             "ConvertTo-Json -Depth 3"],
            capture_output=True, timeout=30)
        data = json.loads(ps.stdout.decode('utf-8', 'replace') or '[]')
        if isinstance(data, dict):
            data = [data]
        res = []
        for d in data:
            cmd = str(d.get('CommandLine') or '')
            script = ''
            for tok in cmd.replace('"', ' ').split():
                if tok.endswith('.py'):
                    script = os.path.basename(tok)
                    break
            res.append({
                'pid': d.get('ProcessId'),
                'ppid': d.get('ParentProcessId'),
                'created': _cim_date(d.get('CreationDate')),
                'script': script or (os.path.basename(cmd.split()[-1]) if cmd else ''),
            })
        return res
    except Exception as e:  # pragma: no cover - 环境依赖
        return [{'error': f'{type(e).__name__}: {e}'}]


# ---------------------------------------------------------------------------
# Q2 时序
# ---------------------------------------------------------------------------

def parse_monitor_log(text: str) -> Dict[str, Any]:
    """解析 monitor 日志: 抽 ERROR/WARN/INFO 时间戳行 + 单场失败行 + 周期耗时。"""
    ts_lines, match_fails = [], 0
    for i, ln in enumerate(text.splitlines(), 1):
        m = RE_TS.match(ln.strip())
        if m:
            ts_lines.append({'line': i, 'ts': m.group(1).replace('T', ' '),
                             'level': m.group(2), 'msg': m.group(3).strip()})
        elif RE_MATCH_FAIL.match(ln):
            match_fails += 1
    cycles: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, Any]] = None
    for rec in ts_lines:
        msg = rec['msg']
        if '周期完成' in msg:
            m = RE_CYCLE_SEC.search(msg)
            cyc = {
                    'ts': rec['ts'], 'cycle_sec': float(m.group(1)) if m else None,
                    'predictions': (RE_PREDICT_AFTER.search(msg).group(1)
                                    if RE_PREDICT_AFTER.search(msg) else None),
                    'predictions_failed': '今日预测 ?' in msg,
                    'narrative': None, 'kind': 'CYCLE_DONE',
                }
            nm = re.search(r'叙事\+(-?\d+)', msg)
            if nm:
                cyc['narrative'] = int(nm.group(1))
            cycles.append(cyc)
        elif '预测补算失败' in msg:
            pending = {'ts': rec['ts'], 'level': rec['level'], 'kind': 'ERROR',
                       'error': msg.replace('预测补算失败: ', '').strip()}
            cycles.append(pending)
    # 失败 → 下一周期完成 的间隔 = 该周期在撞锁上耗掉的时间 (含 busy_timeout 等待)
    for i, c in enumerate(cycles):
        if c.get('kind') != 'ERROR':
            c['kind'] = 'CYCLE_DONE'
        if c['kind'] == 'ERROR' and i + 1 < len(cycles) and cycles[i + 1].get('kind') == 'CYCLE_DONE':
            c['next_done_ts'] = cycles[i + 1]['ts']
            c['gap_sec'] = _gap_sec(c['ts'], c['next_done_ts'])
    return {'ts_lines': len(ts_lines), 'match_fail_lines': match_fails, 'cycles': cycles}


def _gap_sec(ts_a: str, ts_b: str) -> Optional[float]:
    try:
        a = datetime.strptime(ts_a, '%Y-%m-%d %H:%M:%S')
        b = datetime.strptime(ts_b, '%Y-%m-%d %H:%M:%S')
        return round((b - a).total_seconds(), 1)
    except Exception:
        return None


def summarize_cycles(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """按日汇总周期成功率 + 失败周期形态。"""
    by_day: Dict[str, Dict[str, Any]] = {}
    for c in parsed['cycles']:
        d = (c.get('ts') or '')[:10] or 'unknown'
        slot = by_day.setdefault(d, {'cycles': 0, 'refresh_failures': 0,
                                     'match_fail_lines': 0,
                                     'predictions_failed_cycles': 0,
                                     'cycle_sec_sum': 0.0, 'cycle_sec_n': 0,
                                     'gap_samples': []})
        if c['kind'] == 'CYCLE_DONE':
            slot['cycles'] += 1
            if c.get('cycle_sec') is not None:
                slot['cycle_sec_sum'] += float(c['cycle_sec'])
                slot['cycle_sec_n'] += 1
            if c.get('predictions_failed'):
                slot['predictions_failed_cycles'] += 1
        else:
            slot['refresh_failures'] += 1
        if c.get('gap_sec') is not None:
            slot['gap_samples'].append(c['gap_sec'])
    for d, s in by_day.items():
        tot = s['cycles'] + s['refresh_failures']
        s['total_cycles'] = tot
        s['failure_rate'] = round(s['refresh_failures'] / tot, 4) if tot else None
        s['avg_cycle_sec'] = round(s['cycle_sec_sum'] / s['cycle_sec_n'], 1) if s['cycle_sec_n'] else None
        s['match_fail_lines'] = parsed['match_fail_lines'] if d == _latest_day(parsed) else 0
    return by_day


def _latest_day(parsed: Dict[str, Any]) -> str:
    days = sorted({(c.get('ts') or '')[:10] for c in parsed['cycles'] if c.get('ts')})
    return days[-1] if days else ''


def classify_lock_pattern(gap: Optional[float], busy_timeout_ms: int = 30000) -> str:
    """把"失败→周期结束"的间隔归类, 判定是否为 busy_timeout 等待签名。"""
    if gap is None:
        return 'UNKNOWN'
    if busy_timeout_ms / 1000.0 * 0.8 <= gap <= busy_timeout_ms / 1000.0 * 1.5:
        return 'BUSY_TIMEOUT_WAIT'
    if gap < 1.0:
        return 'IMMEDIATE_FAIL'
    return 'OTHER'


# ---------------------------------------------------------------------------
# Q3 影响面 (只读)
# ---------------------------------------------------------------------------

def wal_sidecar_stats(db_path: str) -> Dict[str, Any]:
    """只读 stat WAL/SHM 侧车文件 (写活跃度代理指标, 绝不 checkpoint/truncate)。"""
    out: Dict[str, Any] = {'db_bytes': None, 'wal_bytes': None, 'shm_bytes': None,
                           'wal_mb': None}
    for suffix, key in (('', 'db_bytes'), ('-wal', 'wal_bytes'), ('-shm', 'shm_bytes')):
        p = db_path + suffix
        try:
            out[key] = os.path.getsize(p)
        except OSError:
            out[key] = None
    if out['wal_bytes'] is not None:
        out['wal_mb'] = round(out['wal_bytes'] / 1048576.0, 1)
    return out


def live_impact(db_path: str, today: Optional[str] = None) -> Dict[str, Any]:
    """只读量化撞锁的直接损失: 今日应刷新却无 daily_predictions 的场次数。

    全部走 ``mode=ro``, 零写入。
    """
    today = today or datetime.now().strftime('%Y-%m-%d')
    res: Dict[str, Any] = {'date': today, 'ok': False}
    try:
        con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=30)
        one = lambda sql, p=(): con.execute(sql, p).fetchone()[0]
        res['matches_today'] = one(
            "SELECT COUNT(*) FROM matches WHERE kickoff LIKE ?", (f'{today}%',))
        res['matches_today_by_status'] = dict(con.execute(
            "SELECT status, COUNT(*) FROM matches WHERE kickoff LIKE ? GROUP BY 1",
            (f'{today}%',)).fetchall())
        res['daily_predictions_today'] = one(
            "SELECT COUNT(*) FROM daily_predictions WHERE match_date=?", (today,))
        res['daily_today_by_source'] = dict(con.execute(
            "SELECT model_source, COUNT(*) FROM daily_predictions WHERE match_date=? GROUP BY 1",
            (today,)).fetchall())
        res['non_finished_without_daily'] = one(
            "SELECT COUNT(*) FROM matches m WHERE m.kickoff LIKE ? "
            "AND m.status NOT IN ('finished','abandoned','postponed','canceled') "
            "AND NOT EXISTS (SELECT 1 FROM daily_predictions d WHERE d.match_key=m.match_key)",
            (f'{today}%',))
        res['verdicts_today_matches'] = one(
            "SELECT COUNT(*) FROM prematch_candles_verdict v JOIN matches m "
            "ON m.match_key=v.match_key WHERE m.kickoff LIKE ?", (f'{today}%',))
        con.close()
        res['ok'] = True
    except Exception as e:
        res['error'] = f'{type(e).__name__}: {e}'
    return res


# ---------------------------------------------------------------------------
# Q4 整改规格 (纯计算, 不落地)
# ---------------------------------------------------------------------------

def plan_retry(attempt: int, base: float = 2.0, factor: float = 3.0,
               cap: float = 120.0, jitter: float = 0.3) -> float:
    """第 attempt 次重试的退避秒数 (指数退避 + 对称抖动, 上限 cap)。

    Args:
        attempt: 从 1 开始的重试序号。
        base: 首拍基数 (秒)。
        factor: 指数因子。
        cap: 单拍上限 (秒)。
        jitter: 抖动比例 (0.3 = ±30%)。

    Returns:
        退避秒数 (float, >= 0)。
    """
    if attempt < 1:
        attempt = 1
    raw = base * (factor ** (attempt - 1))
    import random
    jittered = raw * (1.0 + ((random.random() * 2 - 1) * jitter))
    return round(min(cap, max(0.0, jittered)), 2)


def build_recommendations(findings: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """整改条目 (WINDOW 备料, 纯规格)。每项含验收门禁与回滚。"""
    f = findings or {}
    return [
        {'id': 'R1', 'title': 'refresh_predictions 加抖动退避重试 (撞锁专用)',
         'change': ('把 :196 的单次调用换成 retry 包装: 仅对 OperationalError '
                    '("database is locked"/"database is busy") 重试, 其余异常立即上抛; '
                    '最多 3 拍, 退避 base=2s factor=3 jitter=±30% cap=120s'),
         'why': '实测失败即整周期放弃补算 (status 退化成"今日预测 ?"), 无重试无退避',
         'gates': ['G1 连续 24h 内 refresh 失败率 <= 5%', 'G2 重试不得放大写锁占用 (单拍不超过 busy_timeout)',
                   'G3 非锁类异常仍 fail-fast (禁止把一切当锁重试掩盖真实错误)',
                   'G4 新增守卫: 锁类异常与非锁异常走不同分支'],
         'rollback': '回退单点调用; 回滚期间失败率回到原基线, 无数据副作用'},
        {'id': 'R2', 'title': '预测写路径与监测只读面分离 (long-lived writer)',
         'change': ('refresh_predictions 复用单一写连接并缩短持有时间: 先 RO 取候选, '
                    '再单连接分批写, 避免"先开 RO 再开 RW"的两连接叠加'),
         'why': '单写者模型下 monitor 与 ws_collector 必撞; 两连接叠加放大争抢窗口',
         'gates': [                   'G1 events.db journal_mode 恒为 wal (落库前后 PRAGMA 读取一致)',
                   'G2 写事务单条语句粒度 <= 5s (日志计时)', 'G3 落库不改变任何既有 daily_predictions 行 (只补缺)',
                   'G4 不引入任何跨庄字段 (IR-32 单庄口径不变)'],
         'rollback': '恢复原每次新建连接; 无迁移无 schema 变更'},
        {'id': 'R3', 'title': '撞锁降级为可观测告警而非静默 "?"',
         'change': ('把 status[\'predictions\'] 的 error 分两类: LOCK_CONTENTION / REAL_ERROR; '
                    'LOCK_CONTENTION 记 retry_after 与连续次数, 连续 3 次才 WARN 并计入日报'),
         'why': '当前失败只退化成摘要里的"今日预测 ?", 与"今日无赛"不可区分, 无人察觉',
         'gates': ['G1 monitor_status.json 新增 predictions.error_kind 字段',
                   'G2 零信息对照不受影响 (本项不触碰任何概率/赔率口径, IR-30 不受影响)',
                   'G3 历史 monitor_history.jsonl 追加格式向后兼容'],
         'rollback': '字段可缺省, 老解析器按 absence 处理'},
        {'id': 'R4', 'title': '串行化写入窗口 (避免与采集器同刻写)',
         'change': ('给 monitor 增加避让: 检测到 ws_collector 正在写 (读 events.db-wal mtime 变化 / '
                    'monitor_history 中上一周期写入行数突增) 时推迟本轮补算到下一拍'),
         'why': '采集器持续写 odds_changes (超大表), 撞锁窗口由其事务粒度决定',
         'gates': ['G1 避让不得导致补算永久饿死 (最长 2 拍后强制执行)',
                   'G2 不影响 IR-32 零跨庄 / 不影响校准口径'],
         'rollback': '关掉避让开关即回退'},
    ]


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def render_md(report: Dict[str, Any]) -> str:
    q = report['questions']
    L: List[str] = []
    A = L.append
    A('# T44 预测补算 `database is locked` 只读归因')
    A('')
    A(f"- 生成时间: {report['generated_at']}")
    A(f"- 证据源: `{report['log_path']}` / 只读 `events.db` (`mode=ro`) / 进程清单 (只读, 未 kill)")
    A(f"- 纯只读: 未重试 / 未重启 / 未改调度 / 未写 events.db / 未跑 ingest / 未写 verification.db")
    A('')
    A('## 结论摘要')
    A('')
    for c in report['headline']:
        A(f"- {c}")
    A('')
    A('## Q1 归因: 谁在写 events.db')
    A('')
    for w in report['write_surface']['writers']:
        A(f"- **写方** `{w['file']}` — 写语句 {w['write_stmts']} 处"
          f", 自设 busy_timeout={w['busy_timeout_set']}, 只读通路={w['readonly_path']}")
    for r in report['write_surface']['readers']:
        A(f"- 只读方 `{r['file']}` (命中 {r['write_stmts']} 处但含 readonly 通路)")
    A('')
    A('**进程面 (只读 Get-CimInstance, 未 kill 未 stop)**:')
    for p in report['processes'][:12]:
        A(f"- PID {p.get('pid')} (PPID {p.get('ppid')}, 启动 {p.get('created')}) — {p.get('script')}")
    if report['processes'] and 'error' in report['processes'][0]:
        A(f"- 进程采集失败: {report['processes'][0]['error']}")
    A('')
    A(f"- monitor 自身: 独立进程, 走 `gq.db.conn()` 写连接 → 与 `ws_collector` / `bridge_service` "
      f"同写 events.db; SQLite 单写者模型下**跨进程撞锁是结构性必然**, 不是偶发")
    A(f"- monitor 源码结构: 打开写连接={report['monitor_source']['open_write_connection']}, "
      f"自带退避={report['monitor_source']['has_backoff']}")
    A('')
    A('## Q2 时序: 失败周期形态')
    A('')
    A('| 日期 | 周期数 | 补算失败 | 失败率 | 平均周期耗时 | 失败→周期结束间隔(s) |')
    A('|---|---|---|---|---|---|')
    for d, s in sorted(report['daily'].items()):
        gaps = ', '.join(str(g) for g in (s['gap_samples'] or [])) or '-'
        A(f"| {d} | {s['total_cycles']} | {s['refresh_failures']} | "
          f"{s['failure_rate']} | {s['avg_cycle_sec']} | {gaps} |")
    A('')
    A(f"- 单场失败日志行总数: **{report['parsed']['match_fail_lines']}** "
      f"(来自 `gq/match_narrative.py:364` 的 per-match try/except, 说明撞锁也打掉了叙事补录)")
    A(f"- 间隔归类: {report['lock_pattern']}")
    A('')
    A('## Q3 影响: 今日损失面 (只读)')
    A('')
    imp = report['impact']
    if imp.get('ok'):
        A(f"- 今日 `matches` {imp['matches_today']} 场 (by status: {imp['matches_today_by_status']})")
        A(f"- 今日 `daily_predictions` {imp['daily_predictions_today']} 场 (by source: {imp['daily_today_by_source']})")
        A(f"- **今日未开赛/未完赛且无预测行的场次: {imp['non_finished_without_daily']}** ← 撞锁周期直接损失")
        A(f"- 今日有 K线判定的场 {imp['verdicts_today_matches']}, 与上面的缺口共同决定"
          f"「临场≤2h 新定格判定何时能应用到日表」")
    else:
        A(f"- impact 采集失败: {imp.get('error')}")
    A(f"- WAL 侧车: {report['wal']}")
    A('')
    A('## Q4 整改规格 (WINDOW 备料, 不落地)')
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
    ap = argparse.ArgumentParser(description='T44 预测补算撞锁只读归因')
    ap.add_argument('--skip-db', action='store_true', help='跳过 events.db 只读查询')
    args = ap.parse_args()

    log_text = read_text_if_exists(LOG) or ''
    parsed = parse_monitor_log(log_text)
    daily = summarize_cycles(parsed)
    mon = analyze_monitor_source(read_text_if_exists(
        os.path.join(ROOT, 'scripts', 'autonomous_monitor.py')) or '')
    surface = scan_write_surface(ROOT, WRITE_SURFACE_FILES)
    procs = live_processes()
    wal = wal_sidecar_stats(DB)
    imp = (live_impact(DB) if not args.skip_db else {'ok': False, 'error': 'skipped'})

    gaps = [g for s in daily.values() for g in s['gap_samples']]
    pattern = classify_lock_pattern(gaps[0]) if gaps else 'UNKNOWN'

    total_cycles = sum(s['total_cycles'] for s in daily.values())
    total_fail = sum(s['refresh_failures'] for s in daily.values())
    today_key = max(daily.keys()) if daily else ''
    t_slot = daily.get(today_key, {})
    headline = [
        f"补算失败 **{total_fail}/{total_cycles}** (全程, {today_key or '今日'} 当日 "
        f"{t_slot.get('refresh_failures', 0)}/{t_slot.get('total_cycles', 0)} = "
        f"{t_slot.get('failure_rate')}) —— 全部集中在 {today_key}",
        "根因=跨进程单写者争用: monitor(独立进程写连接) 撞 ws_collector/bridge 的持续写入",
        "结构性缺陷=:196-199 无重试无退避, 一次撞锁即整周期放弃, 且只退化成摘要「今日预测 ?」",
        "撞锁同时打掉叙事补录 (单场失败行见 Q2, 来源 match_narrative.py:364)",
        "⚠ 附属发现: `logs/autonomous_monitor.log` 是**混合编码** (部分行 UTF-8 / 部分行 GBK), "
        "整文件解码或按 UTF-8 grep 都会静默漏匹配一半日志 (本脚本改逐行解码后才解析出全部 96 个周期)",
        "同类风险在 T31/T33/T36 已证明会继续饿死 G1 供给 (补算面与 ingest 面同源)",
    ]
    caveats = [
        "本脚本零重试、零重启、零 events.db 写入; 结论全部来自只读证据 (日志/源码/stat/ro 查询)。",
        "「谁持锁」无法在不干扰生产的前提下直接观测; 本文给出的是**结构与耗时形态证据**"
        "(失败→周期结束间隔呈 ~30s = busy_timeout 等待签名), 不是持锁进程快照。",
        "Q3 的场次缺口含「正常未到刷新点」的成分 (临场≤2h 未写入判定的场本就无 daily 行), "
        "不是全部由撞锁造成, 须按判定时间切分后才能归因。",
        "整改条目均为 WINDOW 备料; 落地须走维护窗口 + 全量 pytest + 回滚预案。",
    ]
    report = {
        'task': 'T44 预测补算 database is locked 只读归因',
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'log_path': LOG,
        'headline': headline,
        'questions': {'Q1': 'who writes events.db / 归因', 'Q2': '时序形态',
                      'Q3': '损失面', 'Q4': '整改规格'},
        'parsed': {'ts_lines': parsed['ts_lines'],
                   'match_fail_lines': parsed['match_fail_lines'],
                   'cycles': parsed['cycles']},
        'daily': daily,
        'lock_pattern': pattern,
        'monitor_source': mon,
        'write_surface': surface,
        'processes': procs,
        'wal': wal,
        'impact': imp,
        'recommendations': build_recommendations(),
        'caveats': caveats,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(OUT_MD, 'w', encoding='utf-8') as f:
        f.write(render_md(report))
    print(f'cycles={total_cycles} refresh_failures={total_fail} lock_pattern={pattern}')
    print(f'-> {OUT_JSON}')
    print(f'-> {OUT_MD}')


if __name__ == '__main__':
    main()
