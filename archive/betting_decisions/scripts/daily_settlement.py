# -*- coding: utf-8 -*-
"""三台账每日自动结算 (2026-09-09, 自主优化: 复盘数据闭环引擎)

结算: ① prediction_ledger 积压 (9/1 后 OU/1X2 修复版成绩)
      ② beat_under_log (小球偏差策略)
      ③ sixline_log (六行框架 + 门控)
用法: .venv/Scripts/python.exe scripts/daily_settlement.py
建议: 每日 1-2 次 (本地计划任务或手动)。
"""
import collections
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
        WHERE l.correct IS NULL AND m.status='finished' AND m.score_home IS NOT NULL""").fetchall()
    done = 0
    for (mk,) in rows:
        try:
            done += resolve_match(con, mk)
        except Exception:
            pass
    return len(rows), done



def settle_halftime_conclusion(con):
    """中场冻结判定结算 (2026-09-10 双锚点架构): HT 冻结的 OU/1X2/CS vs 终果.

    OU:  direction='OVER'  → 赢 iff FT总球 > 冻结线 (FT总球 == 线 = 走水 void)
         direction='UNDER' → 赢 iff FT总球 < 冻结线
    1X2: x2_direction vs 终果胜平负;  CS: top1/top3 是否含终场比分。
    结果写回表内列 (ou_hit/x2_hit/cs1_hit/cs3_hit), 并打印成绩单。
    """
    for col, typ in [('ou_hit', 'INTEGER'), ('x2_hit', 'INTEGER'),
                     ('cs1_hit', 'INTEGER'), ('cs3_hit', 'INTEGER')]:
        try:
            con.execute("ALTER TABLE halftime_conclusion ADD COLUMN " + col + " " + typ)
            con.commit()
        except Exception:
            pass
    rows = con.execute("""
        SELECT h.match_key, h.ht_home, h.ht_away, h.ou_line, h.ou_direction,
               h.x2_direction, h.cs_top1, h.cs_top3,
               m.score_home, m.score_away
        FROM halftime_conclusion h JOIN matches m ON m.match_key = h.match_key
        WHERE m.status='finished' AND m.score_home IS NOT NULL
          AND m.kickoff <= datetime('now', '-110 minutes')
          AND EXISTS (SELECT 1 FROM odds_snapshots o WHERE o.match_key = h.match_key
                      AND o.score_at IS NOT NULL AND o.score_at != '')
          AND h.ou_hit IS NULL""").fetchall()
    n_set = 0
    ou = {'win': 0, 'lose': 0, 'void': 0}
    x2 = {'win': 0, 'lose': 0}
    c1 = {'win': 0, 'lose': 0}
    c3 = {'win': 0, 'lose': 0}
    for (mk, hh, ha, oline, odir, xdir, ct1, ct3, fsh, fsa) in rows:
        ft_tot = fsh + fsa
        ou_hit = x2_hit = c1_hit = c3_hit = None
        if odir in ('OVER', 'UNDER') and oline:
            if ft_tot == oline:
                ou_hit = 0; ou['void'] += 1
            elif (ft_tot > oline) == (odir == 'OVER'):
                ou_hit = 1; ou['win'] += 1
            else:
                ou_hit = -1; ou['lose'] += 1
        actual = 'home' if fsh > fsa else ('draw' if fsh == fsa else 'away')
        if xdir:
            x2_hit = 1 if xdir == actual else -1
            x2['win' if x2_hit == 1 else 'lose'] += 1
        fs = str(fsh) + '-' + str(fsa)
        if ct1:
            c1_hit = 1 if ct1 == fs else -1
            c1['win' if c1_hit == 1 else 'lose'] += 1
        if ct3:
            t3 = [x.strip() for x in str(ct3).split(',')]
            c3_hit = 1 if fs in t3 else -1
            c3['win' if c3_hit == 1 else 'lose'] += 1
        con.execute("UPDATE halftime_conclusion SET ou_hit=?, x2_hit=?, cs1_hit=?, cs3_hit=? "
                    "WHERE match_key=?", (ou_hit, x2_hit, c1_hit, c3_hit, mk))
        n_set += 1
    con.commit()
    rate = lambda d: ((str(round(d['win']/(d['win']+d['lose'])*100, 1)) + '%') if d['win']+d['lose'] else '—')
    print('── 中场冻结判定成绩单 ──')
    print('  本轮结算', n_set, '场')
    # 赛前锚点成绩单 (prematch_conclusion 赛前固化 1X2 vs 终果, 现算)
    try:
        pro = con.execute("""
            SELECT p.verdict_code, m.score_home, m.score_away
            FROM prematch_conclusion p JOIN matches m ON m.match_key = p.match_key
            WHERE m.status='finished' AND m.score_home IS NOT NULL""").fetchall()
        pp = collections.Counter()
        _vc_map = {'H': 'home', 'D': 'draw', 'A': 'away',
                   'home': 'home', 'draw': 'draw', 'away': 'away'}
        for vc, fsh, fsa in pro:
            actual = 'home' if fsh > fsa else ('draw' if fsh == fsa else 'away')
            pp[_vc_map.get(str(vc).strip().upper(), vc) == actual] += 1
        if pp:
            tot = pp[True] + pp[False]
            print('  [赛前锚点] 1X2 固化判定:', pp[True], '/', tot, '=', round(pp[True]/tot*100, 1), '%')
    except Exception as _pe:
        print('  [赛前锚点] 结算失败:', _pe)
    print("  OU 方向:  赢", ou['win'], "/ 输", ou['lose'], "/ 走水", ou['void'], "→ 命中率", rate(ou))
    print("  1X2 方向:", rate(x2), "(赢", x2['win'], "/ 输", x2['lose'], ")")
    print("  CS TOP1: ", rate(c1), "| CS TOP3:", rate(c3))
    return n_set


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

    # 中场冻结判定结算 (2026-09-10 双锚点架构)
    try:
        settle_halftime_conclusion(con)
    except Exception as e:
        print(f'[halftime] 结算失败(不阻塞): {e}')

    # 策略台账 (2026-09-10: 让球+1 回测 ROI+23.4%, 时段规律+6.1pp)
    try:
        sys.path.insert(0, r'D:\Architecture\scripts')
        from strategy_ledger import main as sl_main
        sl_main()
    except Exception as e:
        print(f'[strategy] 失败(不阻塞): {e}')

    # 比分源同步: odds_changes 镜像 -> matches
    try:
        rows2 = con.execute(
            "SELECT m.match_key, m.score_home, m.score_away FROM matches m "
            "WHERE m.status='finished' AND m.score_home IS NOT NULL "
            "AND m.kickoff >= datetime('now', '-7 days')").fetchall()
        fixed = 0
        for (mk, sh, sa) in rows2:
            r = con.execute(
                "SELECT score_at FROM odds_changes WHERE match_key=? AND score_at != '' "
                "ORDER BY minute_at DESC, id DESC LIMIT 1", (mk,)).fetchone()
            if r:
                oc = str(r[0]).replace(':', '-')
                if oc != f'{sh}-{sa}':
                    parts = oc.split('-')
                    con.execute('UPDATE matches SET score_home=?, score_away=? WHERE match_key=?',
                                (int(parts[0]), int(parts[1]), mk))
                    fixed += 1
        if fixed:
            con.commit()
            print(f'[sync] odds_changes->matches 同步修正 {fixed} 场')
    except Exception as e:
        print(f'[sync] ERR: {e}')

    report(con)
    con.close()


if __name__ == '__main__':
    main()
