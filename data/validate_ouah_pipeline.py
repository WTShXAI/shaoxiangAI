import sqlite3, tempfile, os, sys
sys.path.insert(0, "D:/Architecture")
from pipeline.cross_book_edge import (
    analyze_match, analyze_all_leisu, load_leisu_matches, actionable_bets, scan_actionable,
    BookRow, SEL_SPECS,
)
import scripts.leisu_collector as LC

print("=== A. 回归: leisu 1X2 路径 (应仍有 HIGH edge) ===")
edges1x2 = analyze_all_leisu(market="1X2")
n_high = 0
for e in edges1x2:
    for b in actionable_bets(e):
        if b["severity"] == "HIGH":
            n_high += 1
            print(f"  HIGH {e.home} vs {e.away}: {b['selection']} @ {b['odds']} ({b['book']}) +{b['price_edge_pp']}pp")
print(f"  1X2 场数={len(edges1x2)}, HIGH actionable bets={n_high}")

print("\n=== B. 合成 OU 数据全链路 (临时 DB) ===")
tmp = "D:/Architecture/data/_tmp_ouah_test.db"
if os.path.exists(tmp):
    os.remove(tmp)
LC.init_db(tmp)
conn = sqlite3.connect(tmp)
cur = conn.cursor()
# OU 场1: 多数庄 大2.5=1.90/小2.5=1.90, 但 BK_A 大2.5=2.30 (明显高估大球→软线)
ou1 = [
    ("BK_A", 2.30, 2.5, 1.90, None, ""),
    ("BK_B", 1.90, 2.5, 1.90, None, ""),
    ("BK_C", 1.88, 2.5, 1.92, None, ""),
    ("BK_D", 1.92, 2.5, 1.88, None, ""),
    ("BK_E", 1.85, 2.5, 1.95, None, ""),
]
ou2 = [
    ("BK_A", 1.95, 3.0, 1.95, None, ""),
    ("BK_B", 1.90, 3.0, 1.90, None, ""),
    ("BK_C", 1.88, 3.0, 1.92, None, ""),
    ("BK_D", 1.92, 3.0, 1.88, None, ""),
    ("BK_E", 1.85, 3.0, 1.95, None, ""),
]
# AH 场1: 多数 主-0.5=1.90/客-0.5=1.90, 但 BK_A 主-0.5=2.30 (异常)
ah1 = [
    ("BK_A", 2.30, -0.5, 1.90, None, ""),
    ("BK_B", 1.90, -0.5, 1.90, None, ""),
    ("BK_C", 1.88, -0.5, 1.92, None, ""),
    ("BK_D", 1.92, -0.5, 1.88, None, ""),
    ("BK_E", 1.85, -0.5, 1.95, None, ""),
]
for rows, mk in ((ou1, "OU"), (ou2, "OU"), (ah1, "AH")):
    LC.insert_leisu_odds(None, "HOME_T", "AWAY_T", 0, mk, rows, cur, source="leisu")
conn.commit()
conn.close()

for mk in ("OU", "AH"):
    print(f"\n--- market={mk} ---")
    ms = load_leisu_matches(db=tmp, market=mk)
    print(f"  加载场次={len(ms)}, 每场庄数={[len(m['books']) for m in ms]}")
    for m in ms:
        e = analyze_match(m, market=mk)
        print(f"  {m['home']} vs {m['away']} | market={e.market} | 离散 {e.max_spread_pp}pp | 共识={e.consensus} | 最佳={e.best}")
        acts = actionable_bets(e)
        for b in acts:
            print(f"    ▶ 可下注 {b['selection']} @ {b['odds']} ({b['book']}) +{b['price_edge_pp']}pp [{b['severity']}]")
        assert e.market == mk, "market 丢失!"
        # 验证 OU/AH 共识只有 2 项, 且 line 不参与
        assert len(e.consensus) == 2, f"consensus 应为2项, 实为{len(e.consensus)}"
print("\n=== 全部断言通过 ===")
os.remove(tmp)
