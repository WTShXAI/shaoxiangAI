"""
赛后回验脚本 — 载入快照 → 查终场比分 → 对比预测 → 输出报告

用法:
  python scripts/verify_snapshot.py data/verification/verification_20260726_2119.json
"""
import json,sys,sqlite3,time
from pathlib import Path
from collections import Counter

def verify(snapshot_path: str):
    snap = json.loads(Path(snapshot_path).read_text(encoding='utf-8'))
    matches = snap['matches']
    ts = snap['snapshot_time']

    c = sqlite3.connect('data/events.db'); c.row_factory = sqlite3.Row

    hits = 0; total = 0; expired = 0
    details = []

    for m in matches:
        mk = m.get('match_key','')
        if not mk: expired += 1; continue

        # 查询终场结果
        row = c.execute("""
            SELECT score_home,score_away,status,minute FROM matches WHERE match_key=?
        """, (mk,)).fetchone()
        if not row:
            expired += 1; continue

        hg = row['score_home']; ag = row['score_away']
        if hg is None or row['status'] != 'finished':
            expired += 1; continue

        total += 1
        actual = 'H' if hg > ag else ('A' if ag > hg else 'D')
        pred = m['prediction']['verdict']
        hit = actual == pred
        if hit: hits += 1

        details.append({
            'match': f"{m['home']} vs {m['away']}",
            'current': m['current_score'], 'elapsed': m['elapsed'],
            'final': f"{hg}-{ag}", 'pred': pred, 'actual': actual,
            'hit': hit, 'source': m['prediction']['source'],
            'conf': m['prediction']['conf'],
            'spread': m['spread'],
        })

    c.close()
    rate = hits / max(total, 1)

    # ── 报告 ──
    print(f"\n📊 验证报告: {Path(snapshot_path).name}")
    print(f"   快照时间: {ts}")
    print(f"   总场次: {len(matches)}  已终场: {total}  未结束: {total}")
    print(f"   命中: {hits}/{total} = {rate:.1%}\n")

    # 按路由分组
    by_src = Counter()
    src_hits = Counter()
    for d in details:
        by_src[d['source']] += 1
        if d['hit']: src_hits[d['source']] += 1

    print("   路由准确率:")
    for s in sorted(by_src.keys()):
        n = by_src[s]; h = src_hits[s]
        print(f"     {s}: {h}/{n} = {h/max(n,1)*100:.0f}%")

    # 按赔率差分组
    print("\n   赔率差分组:")
    for lo, hi in [(0, 2), (2, 5), (5, 99)]:
        sub = [d for d in details if lo <= d['spread'] < hi]
        if not sub: continue
        sub_h = sum(d['hit'] for d in sub)
        print(f"     spread {lo}-{hi}: {sub_h}/{len(sub)} = {sub_h/len(sub)*100:.0f}%")

    # 逆转信号分组
    print("\n   逆转信号分组:")
    for lo, hi in [(0, 0.4), (0.4, 0.6), (0.6, 1.0)]:
        sub = [d for d in details if lo <= d.get('rev', 0) * 100 < hi * 100]
        if not sub: continue
        sub_h = sum(d['hit'] for d in sub)
        print(f"     rev {lo:.1f}-{hi:.1f}: {sub_h}/{len(sub)} = {sub_h/len(sub)*100:.0f}%")

    print(f"\n   {expired}场未结束/无结果")

    # 更新索引
    idx_path = Path(snapshot_path).parent / 'index.json'
    if idx_path.exists():
        idx = json.loads(idx_path.read_text(encoding='utf-8'))
        for entry in idx:
            if entry['file'] == Path(snapshot_path).name:
                entry['status'] = 'verified'
                entry['accuracy'] = round(rate, 4)
                entry['verified_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f"\n   索引已更新")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python scripts/verify_snapshot.py data/verification/verification_XXXX.json")
        sys.exit(1)
    verify(sys.argv[1])
