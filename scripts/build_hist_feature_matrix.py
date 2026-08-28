#!/usr/bin/env python3
"""
哨响AI · 大规模历史特征矩阵构建
==================================
从 football_data.db 的 historical_matches (31.2万场) 提取 1X2 特征 +
tick 特征，构建带赛果标签的大规模特征矩阵。

与 GQ 特征矩阵的区别:
  - 数据源: historical_matches (close_home/draw/away odds + final_result)
  - 缺省: 无 AH/OU/CS 数据，对应特征填 0
  - 优势: 51,200 场在 tick 区间，tick 特征学习充分

输出: data/hist_feature_matrix.db (独立库，不影响 GQ 特征库)
"""

import sqlite3, math, sys

HIST_DB = r'D:\Architecture\data\football_data.db'
OUT_DB = r'D:\Architecture\data\hist_feature_matrix.db'

# 28维特征名（与 odds_feature_library.py 保持一致）
FEATS = [
    'x1_h','x1_d','x1_a','x1_margin','x1_fav','x1_drawgap','x1_homefav','x1_hminusa',
    'xou_line','xou_over','xou_under','xou_margin','xou_has',
    'xah_line','xah_home','xah_has',
    'xcs_top1','xcs_ent','xcs_cnt','xcs_has',
    'x_league_freq','x_kickoff_band',
    'ftick_home_trap_04','ftick_away_trap_04',
    'ftick_home_strong_129','ftick_away_strong_129',
    'ftick_any_trap','ftick_any_strong',
    'ftick_home_double_edge','ftick_away_double_edge',
]
N_FEAT = len(FEATS)


def compute(row, league_freq):
    """从 historical_matches 一行提取 28 维特征"""
    h = row['close_home_odds']
    d = row['close_draw_odds']
    a = row['close_away_odds']
    if not (h and d and a and h > 1.0):
        return None

    f = [0.0] * N_FEAT

    # 1X2 块（转换为概率空间与GQ特征库一致）
    ph, pd, pa = 1.0/h, 1.0/d, 1.0/a
    f[0], f[1], f[2] = ph, pd, pa
    inv = ph + pd + pa
    f[3] = inv - 1.0
    f[4] = max(ph, pd, pa)
    f[5] = pd - (ph + pa) / 2.0
    f[6] = 1.0 if ph == max(ph, pd, pa) else 0.0
    f[7] = ph - pa

    # AH/OU/CS 块: 历史库无数据，填 0
    # (f[8]~f[19] 已经是 0)

    # 上下文
    f[20] = league_freq
    f[21] = 0.0  # kickoff_band 默认

    # tick 特征 (v2.0 智谱修正: home/away分侧+double_edge)
    ht = int(round(h*100))%10 if 1.0<=h<1.5 else None
    at = int(round(a*100))%10 if 1.0<=a<1.5 else None
    f[22] = 1.0 if ht==4 else 0.0      # ftick_home_trap_04
    f[23] = 1.0 if at==4 else 0.0      # ftick_away_trap_04
    f[24] = 1.0 if ht in (1,2,9) else 0.0  # ftick_home_strong_129
    f[25] = 1.0 if at in (1,2,9) else 0.0  # ftick_away_strong_129
    f[26] = 1.0 if (ht==4 or at==4) else 0.0   # ftick_any_trap
    f[27] = 1.0 if (ht in (1,2,9) or at in (1,2,9)) else 0.0  # ftick_any_strong
    f[28] = 1.0 if (ht in (1,2,9) and at==4) else 0.0  # home_double_edge
    f[29] = 1.0 if (at in (1,2,9) and ht==4) else 0.0  # away_double_edge
    # f[24]~f[26] = 0 (无OU数据)

    return f


def main():
    print('='*60)
    print('哨响AI 大规模历史特征矩阵构建')
    print('='*60)

    src = sqlite3.connect(HIST_DB)
    src.row_factory = sqlite3.Row

    # 联赛频率
    leagues = {}
    for r in src.execute('SELECT league_name, count(*) n FROM historical_matches '
                          'WHERE close_home_odds IS NOT NULL AND close_home_odds > 1.0 '
                          'AND final_result IN ("H","D","A") '
                          'GROUP BY league_name'):
        leagues[r['league_name']] = r['n']
    total = sum(leagues.values())
    league_freq = {k: v/total for k, v in leagues.items()}

    # 读数据
    rows = src.execute('''
        SELECT league_name, match_date, home_team, away_team,
               close_home_odds, close_draw_odds, close_away_odds,
               home_score, away_score, final_result
        FROM historical_matches
        WHERE close_home_odds IS NOT NULL AND close_home_odds > 1.0
          AND final_result IN ("H","D","A")
          AND home_score IS NOT NULL
    ''').fetchall()
    src.close()
    print(f'原始: {len(rows)} 场')

    # 建库
    out = sqlite3.connect(OUT_DB)
    out.execute('DROP TABLE IF EXISTS features_hist')
    col_defs = ', '.join(f'{name} REAL' for name in FEATS)
    out.execute(f'''
        CREATE TABLE features_hist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            league TEXT, match_date TEXT, home TEXT, away TEXT,
            {col_defs},
            label_result TEXT, label_1x2 INTEGER
        )''')

    # 写入
    written = 0
    labels = {'H': 0, 'D': 1, 'A': 2}
    for r in rows:
        d = dict(r)
        f = compute(d, league_freq.get(d['league_name'], 0.0))
        if f is None:
            continue
        result = d['final_result']
        lb = labels.get(result)
        if lb is None:
            continue

        placeholders = ','.join('?' * (4 + N_FEAT + 2))
        vals = [d['league_name'], d['match_date'], d['home_team'], d['away_team']] + f + [result, lb]
        out.execute(f'INSERT INTO features_hist (league, match_date, home, away, {",".join(FEATS)}, label_result, label_1x2) VALUES ({placeholders})', vals)
        written += 1

    out.commit()

    # 统计
    n_trap = out.execute('SELECT count(*) FROM features_hist WHERE ftick_home_trap_04=1 OR ftick_away_trap_04=1').fetchone()[0]
    n_strong = out.execute('SELECT count(*) FROM features_hist WHERE ftick_home_strong_129=1 OR ftick_away_strong_129=1').fetchone()[0]
    n_double = out.execute('SELECT count(*) FROM features_hist WHERE ftick_home_double_edge=1 OR ftick_away_double_edge=1').fetchone()[0]
    n_total = out.execute('SELECT count(*) FROM features_hist').fetchone()[0]
    out.close()

    print(f'写入: {written} 场')
    print(f'trap(any):    {n_trap} ({n_trap/n_total*100:.1f}%)')
    print(f'strong(any):  {n_strong} ({n_strong/n_total*100:.1f}%)')
    print(f'double_edge:  {n_double} ({n_double/n_total*100:.2f}%)')
    print(f'输出: {OUT_DB}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
