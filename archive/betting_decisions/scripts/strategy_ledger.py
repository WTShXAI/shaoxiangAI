#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""策略台账 (2026-09-10, 用户策略手册回测后落地).

回测结论 (45 天 735 场):
  ✓ S5 让球+1 (弱队受让一球 @≈1.96): 覆盖率 63.0%, ROI +23.4% — 唯一正收益, 入台账
  ✓ S6 时段规律 (周三≥18点/周六 全天): 大球命中率 +6.1pp — 方向成立, 作标注
  ✗ 爆冷独赢盲买 -49% / 各类波胆盲买 -44~-66% — 不入台账 (仅条件策略时可复评)

功能:
  record: 扫未来 48h 已有开盘 1X2 的赛程, 满足 S5 条件(弱胜赔∈[2.9,6.0]
          且强胜赔≤3.5)的场写入 strategy_log
  settle: 已完场的 S5 注按覆盖规则结算(平/弱胜=覆盖赢 1.96, 强队净胜≥2=输)
  report: 打印策略台账成绩单

用法: python scripts/strategy_ledger.py [record|settle|report|all]  (默认 all)
"""
import collections
import datetime
import sqlite3
import sys
import warnings

sys.path.insert(0, r'D:\Architecture')
warnings.filterwarnings('ignore')
from analysis.live_goal_probe import _open_gq, _open_1x2_from_snapshots  # noqa

DB = r'D:\Architecture\data\events.db'
S5_ODDS = 1.96


def ensure_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS strategy_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy  TEXT,
        match_key TEXT,
        kickoff   TEXT,
        detail    TEXT,
        odds      REAL,
        stake     REAL DEFAULT 1.0,
        result    TEXT,
        settled_at REAL,
        created_at REAL,
        UNIQUE(strategy, match_key))""")
    con.commit()


def record(con):
    ensure_table(con)
    rows = con.execute("""
        SELECT match_key, kickoff FROM matches
        WHERE status IN ('scheduled', 'live') AND kickoff IS NOT NULL AND kickoff != ''
          AND kickoff >= datetime('now') AND kickoff <= datetime('now', '+48 hours')""").fetchall()
    n = 0
    for mk, ko in rows:
        try:
            h, d, a = _open_1x2_from_snapshots(con, mk)
            if not (h and d and a):
                continue
            inv = [1 / float(h), 1 / float(d), 1 / float(a)]
            s = sum(inv)
            ph, pd_, pa = [x / s for x in inv]
            under = 'home' if float(h) > float(a) else 'away'
            under_odds = float(h if under == 'home' else a)
            fav_odds = float(a if under == 'home' else h)
            if not (2.9 <= under_odds <= 6.0 and fav_odds <= 3.5):
                continue
            # 2026-09-13 崩盘日解剖(9/12: 凌晨2-3时 58注 覆盖23-33% ROI -35~-55%):
            # 与 beat_under 午夜档同模式 → 凌晨 02-05 时开赛不入账
            try:
                _dt = datetime.datetime.fromisoformat(str(ko).replace(' ', 'T'))
                if _dt.tzinfo is None:
                    _dt = _dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                if 2 <= _dt.hour < 5:
                    continue
            except Exception:
                pass
            detail = f"弱队{under} @{under_odds:.2f} (强队 @{fav_odds:.2f})"
            try:
                con.execute("""INSERT INTO strategy_log (strategy, match_key, kickoff, detail, odds, created_at)
                               VALUES ('让球+1', ?, ?, ?, ?, ?) ON CONFLICT(strategy, match_key) DO NOTHING""",
                            (mk, ko, detail, S5_ODDS, datetime.datetime.now().timestamp()))
                n += con.total_changes and 1 or 0
            except Exception:
                pass
        except Exception:
            continue
    con.commit()
    print(f'[strategy] 新记录 让球+1 候选 {n} 条 (候选池扫描 {len(rows)} 场)')


def settle(con):
    ensure_table(con)
    # 2026-09-10 幽灵finished守卫: 延期/未开赛场被 3.5h 规则打成 finished 0-0,
    # 让球+1 的 0-0 恰好=覆盖赢 → 假 win 刷高台账。守卫: 开赛≥110min + feed 必须有比分帧。
    rows = con.execute("""
        SELECT s.id, s.match_key, s.odds FROM strategy_log s
        JOIN matches m ON m.match_key = s.match_key
        WHERE s.strategy='让球+1' AND s.result IS NULL
          AND m.status='finished' AND m.score_home IS NOT NULL
          AND m.kickoff <= datetime('now', '-110 minutes')
          AND EXISTS (SELECT 1 FROM odds_snapshots o WHERE o.match_key = s.match_key
                      AND o.score_at IS NOT NULL AND o.score_at != '')""").fetchall()
    n = 0
    for sid, mk, odds in rows:
        sh, sa = con.execute("SELECT score_home, score_away FROM matches WHERE match_key=?", (mk,)).fetchone()
        # 弱队未定 → 从比分与盘口无法回溯弱侧, 重读开盘
        h, d, a = _open_1x2_from_snapshots(con, mk)
        if not (h and d and a):
            continue
        under = 'home' if float(h) > float(a) else 'away'
        margin = (sh - sa) if under == 'home' else (sa - sh)
        # 让球+1 覆盖: 弱队净胜 +1 后 > 0 ⇔ 平局(0)或弱队赢(>0); 强队净胜恰 1 → 走水按半输简化为输
        result = 'win' if margin >= 0 else 'lose'
        con.execute("UPDATE strategy_log SET result=?, settled_at=? WHERE id=?",
                    (result, datetime.datetime.now().timestamp(), sid))
        n += 1
    con.commit()
    print(f'[strategy] 结算 让球+1 {n} 条')


def report(con):
    ensure_table(con)
    rows = con.execute("""
        SELECT COUNT(*), SUM(CASE result WHEN 'win' THEN 1 ELSE 0 END),
               SUM(CASE result WHEN 'win' THEN ? ELSE -1.0 END)
        FROM strategy_log WHERE strategy='让球+1' AND result IS NOT NULL""", (S5_ODDS - 1.0,)).fetchone()
    pend = con.execute("SELECT COUNT(*) FROM strategy_log WHERE strategy='让球+1' AND result IS NULL").fetchone()[0]
    if rows and rows[0]:
        n, res, ret = rows   # ret = 净收益 (赢单 +odds-1, 输单 -1)
        roi = ret / n * 100 if n else 0
        hit = res / n * 100 if n else 0
        print(f'── 策略台账成绩单 ──')
        print(f"  让球+1: {n} 注 | 覆盖 {res} ({hit:.1f}%) | ROI {roi:+.1f}% | 待结 {pend}")
    else:
        print(f'── 策略台账成绩单 ──')
        print(f'  让球+1: 暂无已结算注 (待结 {pend})')


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'all'
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA busy_timeout=15000')
    if mode in ('record', 'all'):
        record(con)
    if mode in ('settle', 'all'):
        settle(con)
    if mode in ('report', 'all'):
        report(con)
    con.close()


if __name__ == '__main__':
    main()
