#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自主监测优化循环 — 单周期执行 (2026-09-19, IR-32 合规: 零跨庄)
================================================================================
每周期依次执行 (任何一步失败不影响后续):
  1. bridge 健康: /health 失败 → start_backend.py 自愈拉起 → 复查
  2. 采集活性: odds_changes 最新 captured_at 距今时长 (有 live 场时 >2h 才告警)
  3. 预测新鲜度: 今日 daily_predictions 缺失/过薄 → 轻路径补算 (不现算 torch)
  4. 叙事增量: 近 3 天新完赛场 match_narrative 补录
  5. 校准评估: 报告超 12h → 重跑 eval_prediction_calibration
  6. 漂移检测: K线 LL 相对市场同场集劣化 > +0.01 → 记"建议重训评估"
  7. 重训门控: 新定格 K线判定 ≥100 → 报告"建议 finalize 重训" (不自动改模型, IR 铁律)

状态落盘: reports/monitor_status.json (快照) + reports/monitor_history.jsonl (追加)
日志:     logs/autonomous_monitor.log
用法:     python scripts/autonomous_monitor.py --cycle
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, r'D:\Architecture')
ROOT = r'D:\Architecture'
LOG = os.path.join(ROOT, 'logs', 'autonomous_monitor.log')
STATUS = os.path.join(ROOT, 'reports', 'monitor_status.json')
HISTORY = os.path.join(ROOT, 'reports', 'monitor_history.jsonl')
TRAIN_MARKER = os.path.join(ROOT, 'reports', 'last_candles_train_marker.json')


def log(msg, level='INFO'):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def check_bridge():
    import urllib.request
    try:
        r = urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=5)
        return {'ok': r.status == 200, 'restarted': False}
    except Exception:
        pass
    log('bridge 失联 → 自愈拉起', 'WARN')
    try:
        subprocess.run([os.path.join(ROOT, '.venv', 'Scripts', 'python.exe'),
                        os.path.join(ROOT, 'start_backend.py')],
                       cwd=ROOT, capture_output=True, timeout=60)
    except Exception as e:
        log(f'自愈拉起异常: {e}', 'ERROR')
        return {'ok': False, 'restarted': True}
    time.sleep(18)
    try:
        r = urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=5)
        ok = r.status == 200
        log(f'自愈后健康: {ok}', 'INFO' if ok else 'CRITICAL')
        return {'ok': ok, 'restarted': True}
    except Exception:
        log('自愈后仍失联', 'CRITICAL')
        return {'ok': False, 'restarted': True}


def check_collector():
    """采集活性以 matches.last_seen 为准 (每轮刷新); odds_changes 只记变动, 不作活性指标。
    另读 gq/.feed_state.json: WS 持续瞬断 = 会话/token 过期的显式信号 (2026-09-19 接入)。"""
    import sqlite3
    con = sqlite3.connect(f'file:{os.path.join(ROOT, "data", "events.db")}?mode=ro', uri=True)
    mx = con.execute('SELECT MAX(last_seen) FROM matches').fetchone()[0]
    live = con.execute("""SELECT COUNT(*) FROM matches WHERE status IN ('live','in_play','1H','2H')
          AND kickoff >= datetime('now', 'localtime', '-3 hour')""").fetchone()[0]
    con.close()
    stale_min = (time.time() - mx) / 60 if mx else None
    feed_flap = False
    try:
        with open(os.path.join(ROOT, 'gq', '.feed_state.json'), encoding='utf-8') as f:
            feed_flap = bool(json.load(f).get('feed_flap'))
    except Exception:
        pass
    return {'last_seen_age_min': round(stale_min, 1) if stale_min else None,
            'live_matches': live,
            'feed_flap': feed_flap,
            'alert': bool(stale_min is None or stale_min > 20 or feed_flap)}


def refresh_predictions():
    from pipeline.predict_export import TABLE_DDL, read_for_date, build_for_date
    today = datetime.now().strftime('%Y-%m-%d')
    out = {'date': today, 'before': 0, 'added': 0}
    with _conn_ro() as con:
        try:
            out['before'] = len(read_for_date(con, today))
        except Exception:
            out['before'] = 0
    # 2026-09-19 覆盖修复: 不只首建 — 每轮 refresh 未开赛行, 使采集器临场写入的
    # K线判定及时应用 (此前首建后永不更新, K线覆盖率被钉在首建时刻, 09-18 实测 28/155)。
    # refresh=True 仅触及未开赛行 (开赛定格由 build_for_date 的 ko_ts 守卫保证);
    # allow_candles_compute=False — 只读已定格判定, 不现算 torch。
    from gq.db import conn as gq_conn
    with gq_conn() as con:
        con.executescript(TABLE_DDL)
        build_for_date(con, today, refresh=True, allow_candles_compute=False)
    with _conn_ro() as con:
        out['after'] = len(read_for_date(con, today))
        out['sources'] = {k: n for k, n in con.execute(
            'SELECT model_source, COUNT(*) FROM daily_predictions WHERE match_date=? GROUP BY 1',
            (today,)).fetchall()}
    out['added'] = max(0, out['after'] - out['before'])
    return out


def _conn_ro():
    import sqlite3
    return sqlite3.connect(f'file:{os.path.join(ROOT, "data", "events.db")}?mode=ro', uri=True)


def narrative_increment():
    from gq.match_narrative import backfill
    return backfill(3, only_missing=True)


def calibration_check():
    rp = os.path.join(ROOT, 'reports', 'prediction_calibration_report.json')
    age_h = None
    if os.path.exists(rp):
        age_h = (time.time() - os.path.getmtime(rp)) / 3600
    ran = False
    if age_h is None or age_h > 12:
        try:
            subprocess.run([sys.executable, os.path.join(ROOT, 'scripts',
                           'eval_prediction_calibration.py')],
                           cwd=ROOT, capture_output=True, timeout=600)
            ran = True
        except Exception as e:
            log(f'校准评估失败: {e}', 'ERROR')
    drift = None
    try:
        with open(rp, encoding='utf-8') as f:
            rep = json.load(f)
        delta = (rep.get('candles_vs_market_same_set') or {}).get('log_loss_delta')
        if delta is not None:
            # 2026-09-19 假0-0守卫后真值基线: +0.013 (K线并不优于市场; 旧 -0.022 为污染伪信号,
            # 见 docs/prediction_refactor_checklist.md §五附)。漂移警报 = 劣化超出 0.03。
            drift = {'ll_delta_vs_market': delta,
                     'drift_alert': bool(delta > 0.03)}
    except Exception:
        pass
    return {'report_age_hours': round(age_h, 1) if age_h else None, 'rerun': ran, 'drift': drift}


def retrain_gate():
    import sqlite3
    con = sqlite3.connect(f'file:{os.path.join(ROOT, "data", "events.db")}?mode=ro', uri=True)
    n_new = con.execute(
        'SELECT COUNT(*) FROM prematch_candles_verdict WHERE captured_at > COALESCE(?, 0)',
        (_last_train_ts(),)).fetchone()[0]
    con.close()
    return {'new_verdicts_since_train': n_new,
            'suggest': n_new >= 100}


def _last_train_ts():
    try:
        with open(TRAIN_MARKER, encoding='utf-8') as f:
            return json.load(f)['ts']
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cycle', action='store_true')
    args = ap.parse_args()
    t0 = time.time()
    status = {'cycle_at': time.strftime('%Y-%m-%d %H:%M:%S')}

    status['bridge'] = check_bridge()
    try:
        status['collector'] = check_collector()
    except Exception as e:
        status['collector'] = {'error': str(e), 'alert': False}
        log(f'采集活性检查失败: {e}', 'ERROR')
    if status['collector'].get('alert'):
        c = status['collector']
        why = ' — WS 持续瞬断, 疑似会话/token 过期 (需更新 gq/.env)' if c.get('feed_flap') else ''
        log(f"采集疑似停滞: live={c.get('live_matches')} "
            f"last_seen距今 {c.get('last_seen_age_min')}min{why}", 'WARN')
    try:
        status['predictions'] = refresh_predictions()
    except Exception as e:
        status['predictions'] = {'error': str(e)}
        log(f'预测补算失败: {e}', 'ERROR')
    try:
        status['narrative_added'] = narrative_increment()
    except Exception as e:
        status['narrative_added'] = None
        log(f'叙事增量失败: {e}', 'ERROR')
    status['calibration'] = calibration_check()
    status['retrain_gate'] = retrain_gate()
    if status['retrain_gate']['suggest']:
        log(f"新定格判定已达 {status['retrain_gate']['new_verdicts_since_train']} — 建议评估重训 "
            f"(finalize 前需 walkforward 过线)", 'WARN')

    status['cycle_sec'] = round(time.time() - t0, 1)
    os.makedirs(os.path.dirname(STATUS), exist_ok=True)
    with open(STATUS, 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    with open(HISTORY, 'a', encoding='utf-8') as f:
        f.write(json.dumps(status, ensure_ascii=False) + '\n')
    drift = (status.get('calibration') or {}).get('drift') or {}
    log(f"周期完成 {status['cycle_sec']}s | bridge={'OK' if status['bridge']['ok'] else 'DOWN'} "
        f"| 今日预测 {status.get('predictions', {}).get('after', '?')} | "
        f"叙事+{status.get('narrative_added')} | LL偏差 {drift.get('ll_delta_vs_market')} "
        f"| 重训建议 {'是' if status['retrain_gate']['suggest'] else '否'}")


if __name__ == '__main__':
    main()
