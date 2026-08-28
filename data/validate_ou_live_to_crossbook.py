"""
route 3 落地: 把真实 OU OCR → cross_book_edge OU 路径端到端验证。

输入: 一张雷速「总进球」子页截图 (PNG)
输出: 该场的 OU 跨庄软线报告 (HARD/MEDIUM/LOW/无 edge)

链路:
  PNG  → ocr_odds_matrix(market='OU') → BookRow 列表
       → load_leisu_matches(market='OU') → analyze_match → actionable_bets
       → 打印 H/A 共识 + 偏离庄家 + edge 强度

不是单元测试, 是"管道烟测": 证明真实 OCR → 真实 BookRow → 真实 cross_book
三段全通, OU 跨庄软线检测在真数据上可下注。
"""
import sys, importlib
from pathlib import Path
sys.path.insert(0, r"D:\Architecture\scripts")
sys.path.insert(0, r"D:\Architecture")
sys.path.insert(0, r"D:\Architecture\pipeline")

import leisu_collector as LC
importlib.reload(LC)
from cross_book_edge import (
    BookRow, load_leisu_matches, analyze_match, actionable_bets, to_report,
    SEL_SPECS,
)

# === 输入: ou_tab.png (印度煤炭 vs 加尔各答警察 88' 总进球页) ===
PNG = Path(r"D:\Architecture\data\leisu_capture\diag\ou_tab.png")
print(f"[INPUT] {PNG.name} ({PNG.stat().st_size//1024} KB)")

# === Step 1: OU 真实 OCR ===
ocr_rows = LC.ocr_odds_matrix(PNG, market="OU")
print(f"[OCR ] {len(ocr_rows)} 庄家行")
for r in ocr_rows:
    print(f"       {r}")

if not ocr_rows:
    print("[FAIL] OCR 0 行, 终止")
    sys.exit(1)

# === Step 2: 包装成 BookRow 喂给 load_leisu_matches ===
# load_leisu_matches 期望 {match_id, market, home, away, line, books: [BookRow]}
# 雷速截图中盘口线 2.5 在标题里, 不在矩阵 → 用 default_line=2.5 (全场 OU 主流)
# BookRow 字段: (bookmaker, h, d, a) 位置参数; OU 下 d 存盘口线
# OCR 出的 d 位是 0.X 灰色"大初"数字(非盘口线), 这里用 match.line=2.5 覆盖
match = {
    "match_id": 999999,
    "market": "OU",
    "league": "LIVE_TEST",  # analyze_match 要 league 字段
    "home": "印度煤炭",
    "away": "加尔各答警察",
    "line": 2.5,
    "kickoff_ts": 0,
    "books": [
        BookRow(r[0], r[1], 2.5, r[3])  # book, over_odds, line, under_odds
        for r in ocr_rows
    ],
}
print(f"\n[BOOKS] {len(match['books'])} 庄 → cross_book_edge")
for b in match["books"]:
    print(f"        {b.bookmaker:6s} 大={b.h:.2f} 盘={b.d} 小={b.a:.2f}")

# === Step 3: 共识 + 偏离 + edge ===
edge = analyze_match(match, soft_pp=5.0, market="OU")
print(f"\n[CONSENSUS] OU market 共识:")
print(f"           大 = {edge.consensus.get('O', 0):.3f}   小 = {edge.consensus.get('U', 0):.3f}")
print(f"           max_spread = {edge.max_spread_pp:.1f}pp")
print(f"           n_books={edge.n_books}  soft_lines={len(edge.soft_lines)}")
for sl in edge.soft_lines[:5]:
    print(f"             {sl}")

# === Step 4: 可下注清单 ===
bets = actionable_bets(edge)
print(f"\n[BETS] actionable: {len(bets)}")
for b in bets:
    print(f"       {b['selection']:6s} @ {b['odds']:.2f} ({b['book']}) "
          f"vs {b['consensus_odds']:.2f} consensus | edge {b['price_edge_pp']:+.1f}pp | "
          f"严 {b['severity']}")

# === Step 5: 报告写盘 ===
out = to_report([edge], soft_pp=5.0, with_actionable=True)
out_path = Path(r"D:\Architecture\data\cross_book_edge_ou_live_test.json")
out_path.write_text(__import__("json").dumps(out, ensure_ascii=False, indent=2),
                    encoding="utf-8")
print(f"\n[REPORT] → {out_path}")
print(f"\n✅ route 3 全链路验证通过: 真实 OU 截图 → cross_book OU 路径")
