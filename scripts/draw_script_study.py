#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
draw_script_study.py — "闷平/后发平局"剧本校准研究
====================================================
目的: 用真实历史数据回答三个问题, 为 pipeline/draw_script_detector.py 提供校准依据。

  Q1 赔率结构能否预测终场平局? (分箱单调性 + 校准 + AUC vs naive)
  Q2 "均势盘口"是否比"隐含平局概率"提供增量信息? (二维联合分箱)
  Q3 开盘→收盘的平局漂移(操盘手往平局压钱)是否带来额外提升?
  Q4 (真实半场子集) 半场0-0 与 终场平局+进球 的条件频率是多少?

数据源(全部真实, 无虚拟):
  - football_data.db :: historical_matches  n=312,010  (开/收盘1X2 + 终场赛果)
  - football_data.db :: matches             n=1,829 有真实半场 (halftime_home/away)

⚠️ 数据护栏: william_ht.ht_home/ht_away 是**伪造**的(由 ht_total_code 桶码反推,
   HT==FT 占 75.9%, 2-0 竟是最常见半场比分), 本研究**一律不用**。详见 verify_ht_integrity()。

铁律遵循: 命中率并排 naive 基线; 分箱查单调性; AUC; 未知不填0。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(PROJECT_ROOT, "data", "football_data.db")


# ─────────────────────────── 工具 ───────────────────────────

def devig3(h: float, d: float, a: float) -> Optional[Tuple[float, float, float]]:
    """三向去水 → (p_h, p_d, p_a), 和为1。任一赔率非法返回 None(不填0)。"""
    try:
        h, d, a = float(h), float(d), float(a)
    except (TypeError, ValueError):
        return None
    if h <= 1.0 or d <= 1.0 or a <= 1.0:
        return None
    ih, idr, ia = 1.0 / h, 1.0 / d, 1.0 / a
    s = ih + idr + ia
    if s <= 0:
        return None
    return ih / s, idr / s, ia / s


def auc_binary(scores: List[float], labels: List[int]) -> Optional[float]:
    """Mann-Whitney U 求 AUC (含并列的 0.5 权重)。纯 python, 无 sklearn 依赖。"""
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    sum_pos = sum(r for r, y in zip(ranks, labels) if y == 1)
    n1, n0 = len(pos), len(neg)
    return (sum_pos - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def _bar(rate: float, base: float, width: int = 24) -> str:
    """相对 base 的偏离条, 直观看单调性。"""
    n = int(round(rate * width / max(base * 2, 1e-9)))
    return "#" * max(0, min(width, n))


# ─────────────────────── 数据完整性护栏 ───────────────────────

def verify_ht_integrity(con: sqlite3.Connection) -> None:
    """证伪 william_ht 半场比分, 留下审计痕迹(防未来有人误用)。"""
    print("=" * 78)
    print("【数据护栏】william_ht 半场比分真伪体检")
    print("=" * 78)
    row = con.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN ht_home = h_ft AND ht_away = a_ft THEN 1 ELSE 0 END),
               SUM(CASE WHEN ht_home = 0 AND ht_away = 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN ht_home = 0 AND ht_away = 0 AND h_ft = 0 AND a_ft = 0 THEN 1 ELSE 0 END)
        FROM william_ht
        WHERE ht_home IS NOT NULL AND h_ft IS NOT NULL
    """).fetchone()
    n, same, ht00, both00 = row
    print(f"  样本 n={n:,}")
    print(f"  HT 完全等于 FT      : {same:,} ({same/n:.1%})   ← 真实足球约 22%, >70% 即为反推伪造")
    print(f"  HT 0-0             : {ht00:,}")
    print(f"  其中 FT 也 0-0     : {both00:,} ({both00/max(ht00,1):.1%})  ← 真实约 22%, 100% 即为由FT反推")
    verdict = "伪造(禁用)" if (same / n > 0.5 or both00 / max(ht00, 1) > 0.9) else "可用"
    print(f"  判定: {verdict}")

    row2 = con.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN halftime_home = 0 AND halftime_away = 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN halftime_home = 0 AND halftime_away = 0
                         AND home_score = 0 AND away_score = 0 THEN 1 ELSE 0 END)
        FROM matches
        WHERE halftime_home IS NOT NULL AND home_score IS NOT NULL
    """).fetchone()
    n2, ht00_2, both00_2 = row2
    print(f"\n  对照 matches.halftime_* : n={n2:,}  HT0-0={ht00_2:,}"
          f"  其中FT也0-0={both00_2:,} ({both00_2/max(ht00_2,1):.1%}) ← 符合真实 → 可用")
    print()


# ─────────────────────────── Q1-Q3 ───────────────────────────

def load_corpus(con: sqlite3.Connection) -> List[dict]:
    """加载 312K 真实赔率+赛果。剔除赔率非法/赛果缺失的行(不填0)。"""
    rows = []
    for r in con.execute("""
        SELECT open_home_odds, open_draw_odds, open_away_odds,
               close_home_odds, close_draw_odds, close_away_odds,
               final_result, home_score, away_score
        FROM historical_matches
        WHERE final_result IN ('H','D','A')
    """):
        oh, od, oa, ch, cd, ca, res, hs, as_ = r
        pc = devig3(ch, cd, ca)
        if pc is None:
            continue
        po = devig3(oh, od, oa)
        tot = (hs + as_) if (hs is not None and as_ is not None) else None
        rows.append({
            "ph": pc[0], "pd": pc[1], "pa": pc[2],
            "pd_open": po[1] if po else None,
            "is_draw": 1 if res == "D" else 0,
            "total": tot,
        })
    return rows


def q1_pd_calibration(rows: List[dict]) -> None:
    base = sum(r["is_draw"] for r in rows) / len(rows)
    print("=" * 78)
    print(f"【Q1】隐含平局概率 pd 的分箱与校准   (naive 基线平局率 = {base:.2%})")
    print("=" * 78)
    edges = [0.0, 0.20, 0.23, 0.26, 0.29, 0.32, 0.35, 1.01]
    print(f"  {'pd 区间':>14} | {'n':>7} | {'实际平局率':>9} | {'隐含均值':>8} | {'校准差':>7} | {'相对naive':>9}")
    print("  " + "-" * 74)
    for lo, hi in zip(edges[:-1], edges[1:]):
        sub = [r for r in rows if lo <= r["pd"] < hi]
        if len(sub) < 200:
            continue
        act = sum(r["is_draw"] for r in sub) / len(sub)
        imp = sum(r["pd"] for r in sub) / len(sub)
        print(f"  [{lo:.2f},{hi:.2f}) | {len(sub):7,} | {act:9.2%} | {imp:8.2%} | "
              f"{act-imp:+7.2%} | {act/base:8.2f}x  {_bar(act, base)}")
    a = auc_binary([r["pd"] for r in rows], [r["is_draw"] for r in rows])
    print(f"\n  AUC(pd → 平局) = {a:.4f}   (0.5=无判别力)")


def q2_balance_joint(rows: List[dict]) -> None:
    base = sum(r["is_draw"] for r in rows) / len(rows)
    print()
    print("=" * 78)
    print("【Q2】均势度 |ph-pa| 是否提供 pd 之外的增量信息 (二维联合)")
    print("=" * 78)
    for r in rows:
        r["gap"] = abs(r["ph"] - r["pa"])
    gap_edges = [(0.0, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.55), (0.55, 1.01)]
    pd_edges = [(0.0, 0.26), (0.26, 0.31), (0.31, 1.01)]
    hdr = "  {:>16} |".format("|ph-pa| 区间")
    for plo, phi in pd_edges:
        hdr += f" pd[{plo:.2f},{phi:.2f}){'':>3}|"
    print(hdr)
    print("  " + "-" * 72)
    for glo, ghi in gap_edges:
        line = f"  [{glo:.2f},{ghi:.2f}){'':>6}|"
        for plo, phi in pd_edges:
            sub = [r for r in rows if glo <= r["gap"] < ghi and plo <= r["pd"] < phi]
            if len(sub) < 200:
                line += f" {'n<200':>13} |"
                continue
            act = sum(r["is_draw"] for r in sub) / len(sub)
            line += f" {act:6.2%} n={len(sub)//1000:3d}k |"
        print(line)
    print(f"\n  (格内=实际平局率, naive={base:.2%}; 看行内是否随均势加深而升 → 均势有增量)")

    a_gap = auc_binary([-r["gap"] for r in rows], [r["is_draw"] for r in rows])
    print(f"  AUC(均势度 → 平局) = {a_gap:.4f}")
    combo = [0.65 * r["pd"] + 0.35 * (1.0 - r["gap"]) * 0.34 for r in rows]
    a_combo = auc_binary(combo, [r["is_draw"] for r in rows])
    print(f"  AUC(pd + 均势 组合) = {a_combo:.4f}")


def q3_draw_drift(rows: List[dict]) -> None:
    base = sum(r["is_draw"] for r in rows) / len(rows)
    print()
    print("=" * 78)
    print("【Q3】开盘→收盘 平局漂移 (操盘手往平局压钱 = 平赔缩水)")
    print("=" * 78)
    sub_all = [r for r in rows if r["pd_open"] is not None]
    print(f"  可用样本 n={len(sub_all):,}")
    edges = [(-1.0, -0.02), (-0.02, -0.005), (-0.005, 0.005), (0.005, 0.02), (0.02, 1.0)]
    labels = ["平局大幅冷(≤-2pp)", "平局小冷", "无漂移", "平局小热", "平局大幅热(≥+2pp)"]
    print(f"  {'Δpd = pd收 - pd开':>22} | {'n':>7} | {'实际平局率':>9} | {'相对naive':>9}")
    print("  " + "-" * 62)
    for (lo, hi), lab in zip(edges, labels):
        s = [r for r in sub_all if lo <= (r["pd"] - r["pd_open"]) < hi]
        if len(s) < 200:
            continue
        act = sum(r["is_draw"] for r in s) / len(s)
        print(f"  {lab:>22} | {len(s):7,} | {act:9.2%} | {act/base:8.2f}x  {_bar(act, base)}")


def q4_real_halftime(con: sqlite3.Connection) -> None:
    """真实半场子集: 量化"半场0-0 → 终场平局且有进球"这个剧本的真实频率。"""
    print()
    print("=" * 78)
    print("【Q4】真实半场子集: '上半场0-0 → 终场平局(有进球)' 剧本频率")
    print("=" * 78)
    rows = con.execute("""
        SELECT halftime_home, halftime_away, home_score, away_score
        FROM matches
        WHERE halftime_home IS NOT NULL AND halftime_away IS NOT NULL
          AND home_score IS NOT NULL AND away_score IS NOT NULL
    """).fetchall()
    n = len(rows)
    if n == 0:
        print("  无真实半场数据, 跳过(不伪造)")
        return
    ht00 = [r for r in rows if r[0] == 0 and r[1] == 0]
    ft_draw = [r for r in rows if r[2] == r[3]]
    script = [r for r in rows if r[0] == 0 and r[1] == 0 and r[2] == r[3] and (r[2] + r[3]) >= 2]
    goalless = [r for r in rows if r[0] == 0 and r[1] == 0 and r[2] == 0 and r[3] == 0]
    print(f"  样本 n={n:,} (真实观测半场)")
    print(f"  半场 0-0                        : {len(ht00):5,} ({len(ht00)/n:.1%})")
    print(f"  终场平局                        : {len(ft_draw):5,} ({len(ft_draw)/n:.1%})")
    print(f"  ★ 半场0-0 且 终场平局且总进球≥2 : {len(script):5,} ({len(script)/n:.2%})  ← 目标剧本")
    print(f"    (对照) 半场0-0 且 终场0-0     : {len(goalless):5,} ({len(goalless)/n:.2%})")
    if ht00:
        d_given = [r for r in ht00 if r[2] == r[3]]
        s_given = [r for r in ht00 if r[2] == r[3] and (r[2] + r[3]) >= 2]
        print(f"\n  条件概率 P(终场平局 | 半场0-0)        = {len(d_given)/len(ht00):.1%}")
        print(f"  条件概率 P(目标剧本 | 半场0-0)        = {len(s_given)/len(ht00):.1%}")
    if ft_draw:
        h_given = [r for r in ft_draw if r[0] == 0 and r[1] == 0]
        print(f"  条件概率 P(半场0-0 | 终场平局)        = {len(h_given)/len(ft_draw):.1%}")
    print("\n  ⚠️ 结论口径: 终场平局可由赔率结构预测(Q1-Q3, 31万场支撑);")
    print("     但'进球集中在下半场'的时序无赛前可靠预测源, 只能作为条件剧本呈现, 不做硬预测。")


def apply_case(rows: List[dict], name: str, oh: float, od: float, oa: float) -> None:
    """把一场真实比赛代入分箱, 看它落在哪个平局档位。"""
    p = devig3(oh, od, oa)
    if p is None:
        print(f"  {name}: 赔率非法")
        return
    ph, pd_, pa = p
    gap = abs(ph - pa)
    base = sum(r["is_draw"] for r in rows) / len(rows)
    peers = [r for r in rows
             if abs(r["pd"] - pd_) <= 0.02 and abs(abs(r["ph"] - r["pa"]) - gap) <= 0.08]
    print()
    print("=" * 78)
    print(f"【个案回测】{name}   赔率 {oh}/{od}/{oa}")
    print("=" * 78)
    print(f"  去水隐含: 主 {ph:.1%} / 平 {pd_:.1%} / 客 {pa:.1%}   均势度|ph-pa|={gap:.3f}")
    if len(peers) >= 100:
        act = sum(r["is_draw"] for r in peers) / len(peers)
        print(f"  同结构历史样本 n={len(peers):,}  实际平局率 = {act:.2%}"
              f"  (naive {base:.2%}, 提升 {act/base:.2f}x)")
        tot_ok = [r for r in peers if r["total"] is not None]
        if tot_ok:
            o25 = sum(1 for r in tot_ok if r["total"] >= 3) / len(tot_ok)
            dr_hi = [r for r in tot_ok if r["is_draw"] == 1 and r["total"] >= 2]
            print(f"  同结构大球率(总≥3) = {o25:.1%}"
                  f" | 平局且总进球≥2 占比 = {len(dr_hi)/len(tot_ok):.2%}")
    else:
        print(f"  同结构样本不足(n={len(peers)}), 不给结论(不伪造)")


def main() -> int:
    if not os.path.exists(DB):
        print(f"数据库不存在: {DB}")
        return 1
    con = sqlite3.connect(DB)
    verify_ht_integrity(con)
    print("加载 312K 语料 ...")
    rows = load_corpus(con)
    print(f"有效样本 n={len(rows):,}\n")
    q1_pd_calibration(rows)
    q2_balance_joint(rows)
    q3_draw_drift(rows)
    q4_real_halftime(con)
    # 涛哥给的真实个案: 博多格林特 vs 圣吉罗斯 (欧会杯), 真实赛果 HT 0-0 / FT 2-2
    apply_case(rows, "博多格林特 vs 圣吉罗斯 (真实 HT0-0 → FT2-2)", 2.11, 2.74, 4.10)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
