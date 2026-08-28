"""
回填 data/events.db → bet_analysis.odds_structure (历史44注盘口结构)
================================================================
现状: analyze_all.py 原用博彩API按mid拉赔率, 但都是已完赛比赛 → API空 → odds_structure 全{}。
本脚本: 按 home/away 队名反查 data/events.db → odds_snapshots (gq/db.py 轮询写入的全市场快照),
取赛前(minute_at=0)最早一批, 聚合出 1X2/让球/大小球, 回填到 bet_analysis.odds_structure。

用法: python backfill_odds_structure.py [--dry-run]
"""
import sqlite3, json, sys

GQ_DB = r"D:\Architecture\data\events.db"
SNAP_DB = r"D:\Architecture\data\events.db"


def aggregate_snapshot(rows, flipped=False):
    """单selection行 → {全场独赢/全场让球/全场大小: [{name,odds}]}。取每市场最早出现赔率。"""
    seen = set()
    by_market = {}
    for r in rows:
        m = r['market']
        sk = (m, r['selection'], r.get('line'))
        if sk in seen:
            continue
        seen.add(sk)
        by_market.setdefault(m, []).append((r['selection'], r['odds'], r.get('line')))

    structure = {}
    if '1X2' in by_market:
        h = next((o for s, o, _ in by_market['1X2'] if s == 'home'), None)
        d = next((o for s, o, _ in by_market['1X2'] if s == 'draw'), None)
        a = next((o for s, o, _ in by_market['1X2'] if s == 'away'), None)
        if flipped:
            h, a = a, h
        items = []
        if h: items.append({'name': '主', 'odds': round(h, 3)})
        if d: items.append({'name': '平', 'odds': round(d, 3)})
        if a: items.append({'name': '客', 'odds': round(a, 3)})
        if items: structure['全场独赢'] = items

    ah_markets = {m: v for m, v in by_market.items() if m.startswith('AH_')}
    if ah_markets:
        best_m = max(ah_markets.keys(), key=lambda m: len(ah_markets[m]))
        line = ah_markets[best_m][0][2] if ah_markets[best_m] else None
        hh = next((o for s, o, _ in ah_markets[best_m] if s == 'home'), None)
        aa = next((o for s, o, _ in ah_markets[best_m] if s == 'away'), None)
        if flipped:
            hh, aa = aa, hh
        items = []
        if hh: items.append({'name': str(line) if line is not None else '0', 'odds': round(hh, 3)})
        if aa: items.append({'name': str(line) if line is not None else '0', 'odds': round(aa, 3)})
        if items: structure['全场让球'] = items

    ou_markets = {m: v for m, v in by_market.items() if m.startswith('OU_') or m == 'OU'}
    if ou_markets:
        best_m = max(ou_markets.keys(), key=lambda m: len(ou_markets[m]))
        line = ou_markets[best_m][0][2] if ou_markets[best_m] else None
        ov = next((o for s, o, _ in ou_markets[best_m] if s == 'over'), None)
        un = next((o for s, o, _ in ou_markets[best_m] if s == 'under'), None)
        items = []
        if ov: items.append({'name': str(line) if line is not None else '2.5', 'odds': round(ov, 3)})
        if un: items.append({'name': str(line) if line is not None else '2.5', 'odds': round(un, 3)})
        if items: structure['全场大小'] = items
    return structure


def query_odds(home, away):
    """正/反向匹配 data/events.db, 返回盘口结构 dict 或 {}。"""
    con = sqlite3.connect(SNAP_DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    mk = f"{home} vs {away}"
    for key in (mk, f"{away} vs {home}"):
        cur.execute("""SELECT market, selection, odds, line
                       FROM odds_snapshots
                       WHERE match_key = ? AND minute_at = 0
                       ORDER BY captured_at ASC""", (key,))
        rows = [dict(r) for r in cur.fetchall()]
        if rows:
            con.close()
            return aggregate_snapshot(rows, flipped=(key != mk)), (key != mk)
    con.close()
    return {}, False


def main():
    dry = '--dry-run' in sys.argv
    con = sqlite3.connect(GQ_DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT id, home, away, odds_structure FROM bet_analysis ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]
    print(f"待回填: {len(rows)} 行  (dry-run={dry})\n")

    updated, already, missed = 0, 0, []
    for r in rows:
        # 已有非空结构则跳过
        existing = r['odds_structure']
        try:
            ex = json.loads(existing) if existing else {}
        except Exception:
            ex = {}
        if ex:
            already += 1
            continue

        snap, flipped = query_odds(r['home'], r['away'])
        if not snap:
            missed.append(f"  ✗ id={r['id']} {r['home']} vs {r['away']}")
            continue
        tag = '(反向)' if flipped else ''
        print(f"  ✓ id={r['id']} {r['home']} vs {r['away']} {tag}")
        print(f"      独赢={snap.get('全场独赢')} 让球={snap.get('全场让球')} 大小={snap.get('全场大小')}")
        if not dry:
            cur.execute("UPDATE bet_analysis SET odds_structure=? WHERE id=?",
                        (json.dumps(snap, ensure_ascii=False), r['id']))
        updated += 1

    if not dry and updated:
        con.commit()
    con.close()

    print(f"\n=== 汇总 ===")
    print(f"  已有非空(跳过): {already}")
    print(f"  本次回填: {updated}")
    print(f"  仍未命中: {len(missed)}")
    for m in missed:
        print(m)
    print(f"\n回填后 odds_structure 非空率: {(already + updated)}/{len(rows)} "
          f"({(already + updated)/len(rows)*100:.0f}%)")


if __name__ == '__main__':
    main()
