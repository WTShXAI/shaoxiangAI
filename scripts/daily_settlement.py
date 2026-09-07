# -*- coding: utf-8 -*-
"""三台账每日自动结算 (2026-09-09, 自主优化: 复盘数据闭环引擎)

结算: ① prediction_ledger 积压 (9/1 后 OU/1X2 修复版成绩)
      ② beat_under_log (小球偏差策略)
      ③ sixline_log (六行框架 + 门控)
用法: .venv/Scripts/python.exe scripts/daily_settlement.py
建议: 每日 1-2 次 (本地计划任务或手动)。
"""
import sqlite3
import sys
import time

sys.path.insert(0, r'D:\Architecture')
sys.path.insert(0, r'D:\Architecture\scripts')

DB = r'D:\Architecture\data\events.db'


def settle_prediction_ledger(con):
    from pipeline.prediction_ledger import resolve_match
    rows = con.execute("""
        SELECT DISTINCT l.match_key FROM prediction_ledger l
        JOIN matches m ON m.match_key = l.match_key
        WHERE l.correct IS NULL AND m.status='finished' AND m.score_home IS NOT NULL
          AND m.ht_score_home IS NOT NULL
          AND m.ht_score_home + m.ht_score_away < m.score_home + m.score_away""").fetchall()
    done = 0
    for (mk,) in rows:
        try:
            done += resolve_match(con, mk)
        except Exception:
            pass
    return len(rows), done


def settle_beat_under(con):
    n_new = n_settled = 0
    fut = con.execute("""
        SELECT match_key, league, kickoff FROM matches
        WHERE status='scheduled' AND kickoff IS NOT NULL AND kickoff != ''
        AND kickoff > datetime('now') AND kickoff <= datetime('now', '+48 hours')
        AND (is_override IS NULL OR is_override=0)
        AND NOT EXISTS (SELECT 1 FROM beat_under_log b WHERE b.match_key = matches.match_key)""").fetchall()
    from scripts.beat_under_track import record as _rec, settle as _st  # noqa
    return n_new, n_settled


def settle_sixline(con):
    rows = con.execute("""
        SELECT id, match_key FROM sixline_log WHERE settled_at IS NULL""").fetchall()
    n = 0
    for (rid, mk) in rows:
        m = con.execute("SELECT status, score_home, score_away FROM matches WHERE match_key=?", (mk,)).fetchone()
        if not m or m[0] != 'finished' or m[1] is None:
            continue
        act_dir = 'home' if m[1] > m[2] else ('draw' if m[1] == m[2] else 'away')
        act_score = f"{m[1]}-{m[2]}"
        try:
            pool = con.execute("SELECT score_pool FROM sixline_log WHERE id=?", (rid,)).fetchone()[0]
            pool_l = __import__('json').loads(pool or '[]')
        except Exception:
            pool_l = []
        # 方向命中: direction 是 home/draw/away
        dr = con.execute("SELECT direction FROM sixline_log WHERE id=?", (rid,)).fetchone()[0]
        dir_hit = 1 if dr == act_dir else 0
        # 比分池命中: 精确比分或 top3 语义 —— 记录实际比分, pool_hit 由查询端按池内容算
        pool_hit = 1 if act_score in [p.replace(':', '-') for p in pool_l] else 0
        con.execute("UPDATE sixline_log SET settled_at=?, actual_score=?, dir_hit=?, pool_hit=? WHERE id=?",
                    (time.time(), act_score, dir_hit, pool_hit, rid))
        n += 1
    con.commit()
    return n


def report(con):
    print('── 结算成绩单 ──')
    try:
        r = con.execute("""SELECT gate, sum(dir_hit), count(*) FROM sixline_log
            WHERE settled_at=1 AND gate IS NOT NULL GROUP BY gate""")
        for g, w, n in r:
            try:
                lvl = __import__('json').loads(g).get('level', '?')
            except Exception:
                lvl = '?'
            print(f'  六行[{lvl:9}] 方向命中 {w}/{n} = {w/n*100:.1f}%')
    except Exception:
        pass
    try:
        r = con.execute("""SELECT sum(settle), count(*) FROM beat_under_log WHERE settled_at IS NOT NULL""")
        v = r.fetchone()
        if v and v[1]:
            print(f'  beat_under: {v[1]} 条, 单位收益 {v[0]:+.2f} (等额 ROI {v[0]/v[1]*100:+.1f}%)')
    except Exception:
        pass
    r = con.execute("""SELECT phase, count(*), sum(correct=1) FROM prediction_ledger
        WHERE correct IS NOT NULL AND predicted_at >= '2026-09-01' GROUP BY phase""")
    for ph, n, w in r:
        if n:
            print(f'  台账[{ph}] (9/1后): {w}/{n} = {w/n*100:.1f}%')


def main():
    con = sqlite3.connect(DB, timeout=20)
    con.execute('PRAGMA busy_timeout=15000')
    n1, d1 = settle_prediction_ledger(con)
    print(f'[ledger] 积压 {n1} 场, 结算 {d1} 条')
    n2 = settle_sixline(con)
    print(f'[sixline] 结算 {n2} 条')
    try:
        sys.path.insert(0, r'D:\Architecture\scripts')
        from beat_under_track import record as bu_record, settle as bu_settle
        con2 = sqlite3.connect(DB, timeout=15)
        con2.execute('PRAGMA busy_timeout=10000')
        a = bu_record(con2)
        b = bu_settle(con2)
        con2.close()
        print(f'[beat_under] 新记录 {a}, 结算 {b}')
    except Exception as e:
        print(f'[beat_under] 失败: {e}')
    con.commit()
    report(con)
    con.close()


if __name__ == '__main__':
    main()
