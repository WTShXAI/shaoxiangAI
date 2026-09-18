"""
events.db 赔率双时点提取器
========================
用户方法论 (2026-07-20): events.db 的 odds 快照按"第一行(初盘) + 中场结束后(中场收盘)"筛选即可用。
关键修正: "第一行"必须是 per-selection 首见 (每条赔率线取自己的最早 captured_at),
          不能取全局 min(captured_at) (只命中个别线 + 脏值 0.0)。

注意: 此提取器只处理 ODDS (市场数据)。matches.score_home/score_away 仍污染, 禁用于λ校准。
"""
import sqlite3, datetime

DB = 'data/events.db'

def _con():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def kickoff_ts(kickoff_str, tz_hours=8):
    """kickoff 字符串 -> UTC epoch。默认当 +8 北京时间解释(与采集器一致)。"""
    try:
        dt = datetime.datetime.strptime(kickoff_str, '%Y-%m-%d %H:%M')
        return dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=tz_hours))).timestamp()
    except Exception:
        return None

def get_open(mk, market):
    """初盘 = 每个 selection 的最早 captured_at 对应的 odds。返回 {selection: odds}。
    过滤 odds<=0 脏值(JS 未刷新的陈旧 cell 偶发 0.0) — 取最早的非零快照为初盘价。"""
    c = _con(); cur = c.cursor()
    cur.execute(f'''SELECT selection, odds, captured_at FROM odds_snapshots
        WHERE market=? AND match_key=? AND odds>0 AND (selection, captured_at) IN (
            SELECT selection, MIN(captured_at) FROM odds_snapshots
            WHERE market=? AND match_key=? AND odds>0 GROUP BY selection)''', (market, mk, market, mk))
    out = {r['selection']: r['odds'] for r in cur.fetchall()}
    c.close(); return out

def get_ht_close(mk, market, kickoff_str, win_before=44, win_after=52):
    """中场结束后(下半场开球前)收盘 = kick+44~52min 之间每条 selection 的最后一条。返回 {selection: odds}。

    回退策略: 严格窗口(44~52min)取不到时, 依次尝试更宽窗口(35~65, 30~75),
    以覆盖采集器在中场附近有数据但未精确落在 44~52 的比赛。优先真中场。
    注意: 若比赛采集在开赛<30min 就终止(采集器提前停采, 占比~72%), 任何窗口都取不到 -> 返回空。
    """
    kt = kickoff_ts(kickoff_str)
    if kt is None: return {}
    windows = [(win_before, win_after), (35, 65), (30, 75)]
    c = _con(); cur = c.cursor()
    out = {}
    for (wb, wa) in windows:
        lo, hi = kt + wb*60, kt + wa*60
        cur.execute(f'''SELECT selection, odds, captured_at FROM odds_snapshots
            WHERE market=? AND match_key=? AND captured_at>=? AND captured_at<=?''', (market, mk, lo, hi))
        rows = cur.fetchall()
        if not rows:
            continue
        d = {}
        for r in rows:
            if r['selection'] not in d or r['captured_at'] > d[r['selection']][1]:
                d[r['selection']] = (r['odds'], r['captured_at'])
        out = {s: v[0] for s, v in d.items()}
        break
    c.close()
    return out

def get_two_points(mk, market, kickoff_str):
    """返回 (open_dict, ht_close_dict) 双时点。"""
    return get_open(mk, market), get_ht_close(mk, market, kickoff_str)

if __name__ == '__main__':
    import sys
    mk = sys.argv[1] if len(sys.argv) > 1 else 'AB格莱萨克瑟 vs B93哥本哈根'
    mkt = sys.argv[2] if len(sys.argv) > 2 else 'CS'
    c = _con(); cur = c.cursor()
    ko = cur.execute('SELECT kickoff FROM matches WHERE match_key=?', (mk,)).fetchone()
    ko = ko['kickoff'] if ko else None
    c.close()
    op, ht = get_two_points(mk, mkt, ko)
    print(f'match={mk} market={mkt} kickoff={ko}')
    print(f'--- 初盘(per-selection首见) 共{len(op)}线 top8 ---')
    for s, o in sorted(op.items(), key=lambda x: -x[1])[:8]:
        print(f'  {s}: {o}')
    print(f'--- 中场收盘(kick+44~52min) 共{len(ht)}线 top8 ---')
    for s, o in sorted(ht.items(), key=lambda x: -x[1])[:8]:
        print(f'  {s}: {o}')
