# -*- coding: utf-8 -*-
"""断供比分批量回填 (clean rewrite 2026-09-09)"""
import math, os, sqlite3, sys, time
sys.path.insert(0, r'D:\Architecture')
sys.path.insert(0, r'D:\Architecture\gq')
DB = r'D:\Architecture\data\events.db'

def parse_msc(msc_str):
    sh = sa = None
    for entry in str(msc_str or '').split(','):
        e = entry.strip()
        if e.startswith('S0|'):
            try:
                a, b = e[3:].split(':'); sh, sa = int(a), int(b)
            except Exception: pass
    return sh, sa

def main():
    import auto_collector as ac
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA busy_timeout=20000')
    rows = con.execute(
        "SELECT match_key, mid FROM matches "
        "WHERE status='finished' AND mid IS NOT NULL AND mid != '' "
        "AND kickoff >= datetime('now', '-48 hours') "
        "AND (score_missing IS NULL OR score_missing = 0)").fetchall()
    print(f'回填目标: {len(rows)} 场', flush=True)
    updated = no_data = err = 0
    for (mk, mid) in rows:
        try:
            dec = ac.fetch_match_odds(str(mid))
            if not dec:
                no_data += 1; time.sleep(0.3); continue
            m_list = dec.get('data') or []
            if not m_list:
                no_data += 1; time.sleep(0.3); continue
            m0 = m_list[0]
            msc = m0.get('msc') or ''
            nsh, nsa = parse_msc(msc)
            if nsh is not None and nsa is not None:
                con.execute("UPDATE matches SET score_home=?, score_away=? WHERE match_key=?",
                            (nsh, nsa, mk))
                con.execute("UPDATE match_outcomes SET score_home=?, score_away=? WHERE mid=?",
                            (nsh, nsa, mid))
                updated += 1
            else:
                no_data += 1
        except Exception as e:
            err += 1
        time.sleep(0.3)
    con.commit()
    print(f'更新 {updated} | 不可恢复 {no_data} | 错误 {err}', flush=True)
    con.close()

if __name__ == '__main__':
    main()
