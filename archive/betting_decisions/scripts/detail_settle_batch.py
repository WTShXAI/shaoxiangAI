# -*- coding: utf-8 -*-
"""断供场批量详情结算 (2026-09-09): 用乐鱼详情官方终场比分(msc)直接结算 beat_under。

断供场 = WS 比分帧断供导致 matches 比分冻结/滞后的已完赛场。
乐鱼详情接口(fetch_match_odds)的 msc.S0 是官方终场比分 — 权威级, 直接结算。
节流 0.35s/场; 失败场跳过留待重跑。
"""
import sqlite3
import sys
import time

sys.path.insert(0, r'D:\Architecture')
sys.path.insert(0, r'D:\Architecture\scripts')
sys.path.insert(0, r'D:\Architecture\gq')

DB = r'D:\Architecture\data\events.db'


def main(limit=400):
    import auto_collector as ac
    con = sqlite3.connect(DB, timeout=20)
    con.execute('PRAGMA busy_timeout=15000')
    rows = con.execute("""
        SELECT b.id, b.match_key, m.mid FROM beat_under_log b
        JOIN matches m ON m.match_key = b.match_key
        WHERE b.settled_at IS NULL AND m.status='finished'
          AND m.mid IS NOT NULL AND m.mid != ''
        ORDER BY b.id LIMIT ?""", (limit,)).fetchall()
    print(f'待详情结算: {len(rows)} 场', flush=True)
    ok = err = 0
    for (bid, mk, mid) in rows:
        try:
            dec = ac.fetch_match_odds(str(mid))
            if not dec:
                err += 1
                time.sleep(0.35)
                continue
            m_list = dec.get('data') or []
            if not m_list:
                err += 1
                continue
            m0 = m_list[0]
            msc = m0.get('msc') or ''
            sh = sa = None
            for entry in str(msc).split(','):
                e = entry.strip()
                if e.startswith('S0|'):
                    try:
                        _h, _a = e[3:].split(':')
                        sh, sa = int(_h), int(_a)
                    except Exception:
                        pass
            if sh is None or sa is None:
                err += 1
                time.sleep(0.35)
                continue
            total = sh + sa
            line_row = con.execute("SELECT line, odds FROM beat_under_log WHERE id=?", (bid,)).fetchone()
            if not line_row:
                err += 1
                continue
            line, odds_v = line_row
            if total < line:
                st, val = 'win', 1.0
            elif total == line:
                st, val = 'push', 0.0
            else:
                st, val = 'lose', -1.0
            con.execute(
                "UPDATE beat_under_log SET settled_at=?, actual_goals=?, settle=?, correct=?, "
                "note=COALESCE(note,'')||' [详情官方比分]' WHERE id=?",
                (time.time(), total, val, 1 if st == 'win' else 0, bid))
            ok += 1
        except Exception as e:
            err += 1
        time.sleep(0.35)
    con.commit()
    print(f'详情结算完成: ok={ok} err={err}', flush=True)
    r = con.execute("SELECT sum(settle), count(*) FROM beat_under_log WHERE settled_at IS NOT NULL").fetchone()
    if r and r[1]:
        print(f'累计可信成绩: {r[1]} 条, 单位收益 {r[0]:+.2f} (等额 ROI {r[0]/r[1]*100:+.1f}%)', flush=True)


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 400)
