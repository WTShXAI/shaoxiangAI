# -*- coding: utf-8 -*-
"""beat_under 策略追踪 (2026-09-09, 两系统打通 ③)

爱球客 beat_book 的验证结论: 乐鱼 OU 大球隐含被钉在 ~50%, 实际大球率显著偏低
→ 「始终 back 小球」历史 ROI +6.7% (t=6.7, n=8897, 唯一统计显著的系统性机会)。

本脚本把该策略纳入哨响台账做**持续实盘检验**(先验证后使用):
  - 每日跑一次: 对未来 48h 内开赛、有 OU 盘的场, 记录「back 小球」虚拟建议
  - 已过期建议自动按赛果结算 (total < line 赢, = line 走水, > line 输)
  - 只记录不下单 — 与 9/3 纯分析诊断拍板一致

用法:
  .venv/Scripts/python.exe scripts/beat_under_track.py            # 记录+结算
  .venv/Scripts/python.exe scripts/beat_under_track.py --report   # 输出成绩单

盘口来源: odds_snapshots 该场最活跃 OU 线最新帧(与滚球分析页同源)。
"""
import os
import sqlite3
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'events.db')


def _open():
    con = sqlite3.connect(DB, timeout=15)
    con.execute('PRAGMA busy_timeout=10000')
    return con


def _ensure_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS beat_under_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_key TEXT NOT NULL,
        league TEXT, kickoff TEXT,
        market TEXT DEFAULT 'OU',
        side TEXT DEFAULT 'under',
        line REAL, odds REAL,
        book_odds_total REAL,
        note TEXT,
        created_at REAL,
        settled_at REAL,
        actual_goals REAL,
        settle REAL,             -- +1 赢 / 0 走水 / -1 输 (per unit)
        correct INTEGER)""")
    con.commit()


def _active_ou_line(con, match_key):
    """该场最活跃 OU 线的最新帧 (与 get_latest_snapshot_odds 同源口径, 流内自洽)。"""
    rows = con.execute(
        "SELECT market, selection, odds, captured_at FROM odds_snapshots "
        "WHERE match_key=? AND market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' "
        "AND market NOT LIKE 'OU_2H%' AND odds>1.01 AND odds<1000 "
        "ORDER BY captured_at DESC LIMIT 200", (match_key,)).fetchall()
    streams = {}
    for mkt, sel, o, ts in rows:
        streams.setdefault(mkt, []).append((ts, sel, o))
    best = None
    for mkt, srows in streams.items():
        try:
            line = float(mkt.split('_')[1])
        except Exception:
            continue
        if not (0.5 <= line <= 10.0):
            continue
        first = {}
        for ts, sel, o in srows:          # srows 已按时间倒序, 首见即最新
            first.setdefault(sel, (o, ts))
        if 'over' in first and 'under' in first:
            latest = max(first['over'][1], first['under'][1])
            if best is None or latest > best[0]:
                best = (latest, line, first['over'][0], first['under'][0])
    if best:
        return best[1], best[2], best[3]
    return None


def record(con):
    now = time.time()
    rows = con.execute(
        "SELECT match_key, league, kickoff FROM matches "
        "WHERE status='scheduled' AND kickoff IS NOT NULL AND kickoff != '' "
        "AND kickoff > datetime('now') AND kickoff <= datetime('now', '+48 hours') "
        "AND (is_override IS NULL OR is_override=0)").fetchall()
    n = 0
    for mk, lg, ko in rows:
        dup = con.execute("SELECT 1 FROM beat_under_log WHERE match_key=?", (mk,)).fetchone()
        if dup:
            continue
        got = _active_ou_line(con, mk)
        if not got:
            continue
        line, over, under = got
        # 2026-09-10 数据驱动闸门收紧 (484 注已结样本聚类):
        #   ✓ 盈利核: 线 2.25(+19.0%)/2.5(+13.8%)/3.25(+6.1%)
        #   ✗ 亏损源: 线 1.5(-33.5%)/1.75(-32%)/2.0(-18.6%)/2.75(-13.9%)/3.0(-14.9%)
        #   ✗ 低赔档 <1.80: -25.6% (低赔=市场极度确信的小球, beat_book 优势被吃掉)
        #   ✗ 午夜档(00-06时): -16.0% (亚澳夜场, 样本 155 注)
        if not (2.2 <= line <= 2.6 or 3.2 <= line <= 3.3):
            continue
        if float(under) < 1.80:
            continue
        try:
            _dt = datetime.datetime.fromisoformat(str(ko).replace(' ', 'T'))
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
            if 0 <= _dt.hour < 6:
                continue
        except Exception:
            pass
        con.execute(
            "INSERT INTO beat_under_log (match_key, league, kickoff, line, odds, "
            "book_odds_total, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (mk, lg, ko, line, under, round(over, 2),
             'back 小球 (闸门收紧版: 线2.25-2.5/3.25 + 赔率≥1.80 + 非午夜档)', now))
        n += 1
    con.commit()
    return n


def settle(con):
    # 2026-09-09 比分可信度闸门 (用户抓包验证): 比分源断供场 FT 冻结 0-0,
    # 首批结算 255 条里 212 条 0-0 → ROI +88.2% 假象。闸门: matches 比分必须
    # == 进球轨迹末值 (进球单调, 轨迹末值恒为最新已见比分) 才结算; 否则保持未结算。
    rows = con.execute(
        "SELECT id, match_key, line FROM beat_under_log WHERE settled_at IS NULL").fetchall()
    n = 0
    for rid, mk, line in rows:
        m = con.execute(
            "SELECT status, score_home, score_away FROM matches WHERE match_key=?", (mk,)).fetchone()
        if not m or m[0] != 'finished' or m[1] is None or m[2] is None:
            continue
        sh, sa = int(m[1]), int(m[2])
        # 比分可信度双闸门 (2026-09-09):
        #   ① FT == 轨迹末值  ② 比分帧覆盖到 ≥80' (全程跟随)
        # 83% 断供率实测: obscure 场比分源从头断供 → 轨迹只有早期 0-0 帧,
        # 轨迹末值闸门拦不住 — 必须要求比分帧覆盖到比赛后段才可信。
        traj = con.execute(
            "SELECT score_at, minute_at FROM odds_snapshots WHERE match_key=? AND score_at != '' "
            "ORDER BY minute_at", (mk,)).fetchall()
        if traj:
            last = traj[-1][0].replace(':', '-')
            if last != f'{sh}-{sa}':
                continue   # ① FT 与轨迹末值不符
            max_min = max((int(mn or 0) for _, mn in traj), default=0)
            if max_min < 80:
                continue   # ② 比分帧未覆盖到后段 — 断供场, 不结算
        else:
            continue
        total = sh + sa
        st = 'win' if total < line else ('push' if total == line else 'lose')
        settle = 1.0 if st == 'win' else (0.0 if st == 'push' else -1.0)
        con.execute(
            "UPDATE beat_under_log SET settled_at=?, actual_goals=?, settle=?, correct=? WHERE id=?",
            (time.time(), total, settle, 1 if st == 'win' else 0, rid))
        n += 1
    con.commit()
    return n


def report(con):
    r = con.execute(
        "SELECT count(*), sum(settle=1), sum(settle=0), sum(settle=-1), avg(settle) "
        "FROM beat_under_log WHERE settled_at IS NOT NULL").fetchone()
    if not r or not r[0]:
        print('尚无已结算建议')
        return
    n, w, p, l, avg = r
    print(f'beat_under 已结算 {n} 条: 赢 {w} / 走水 {p} / 输 {l} | 单位收益 {avg:.3f} '
          f'(等额 ROI {avg*100:+.1f}%)')


def main():
    con = _open()
    _ensure_table(con)
    if '--report' in sys.argv:
        report(con)
        return
    con2 = _open()
    added = record(con2)
    settled = settle(con2)
    con2.close()
    print(f'[beat_under] 新记录 {added} 场, 结算 {settled} 条')
    report(con)


if __name__ == '__main__':
    main()
