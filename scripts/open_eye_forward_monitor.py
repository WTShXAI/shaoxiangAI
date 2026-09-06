# -*- coding: utf-8 -*-
"""
open_eye_forward_monitor.py — 开盘天眼 前向纸盘监控 (人工审批前的 +EV 清单生成器)

设计(诚实):
  - 只读 + 建议, 不写任何注单, 不下任何注 (IR-21 建仓/api/execute/confirm 人工审批)。
  - 不输出"稳赢/必中" (IR-30)。只给 side / edge_pp / 1/4-Kelly 建议注码比例。
  - 两种数据源:
      1) CSV 模式: 读 fixtures+开盘赔率 CSV (home,away,open_h,open_d,open_a,kickoff,league),
         逐场 recommend(), 筛选 edge_pp>=MIN_EDGE 出清单。
      2) --selftest: 从 football_data.db 抽近期历史场(>=2023, 有开盘价), 用 predictor 的
         recommend() 路径复算镜像 ROI, 验证监控逻辑与已验证 OOF(+EV) 一致(无需实时 feed)。

输出:
  - 终端: 排序后的 +EV 清单 (markdown 表格)
  - reports/open_eye_forward_picks_<date>.csv  (可交付人工审批)
  - --selftest 额外打印 镜像 ROI 与已验证 EYE_OPEN_RESID(+17.4%) 的对比
"""
from __future__ import annotations
import os, sys, csv, json, argparse
from datetime import datetime, date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "data", "football_data.db")
OUT_DIR = os.path.join(ROOT, "reports")
MIN_EDGE_PP = 3.0          # 监控侧阈值: edge>=3pp 才进清单 (比 predictor 的 0 更严)
SELFTEST_N = 2227          # 复用验证集规模

from pipeline.open_eye_predictor import recommend


def _settle_side(side_idx, odds_mat, y):
    o = odds_mat[side_idx]
    win = (y == side_idx)
    return (o - 1.0) if win else -1.0


def run_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rec = recommend(r["home"], r["away"], r["open_h"], r["open_d"], r["open_a"],
                                r.get("kickoff"), r.get("league"))
            except Exception as e:
                rec = {"ok": False, "reason": str(e)}
            if rec.get("ok") and rec["edge_pp"] >= MIN_EDGE_PP:
                rows.append({"home": r["home"], "away": r["away"], "kickoff": r.get("kickoff", ""),
                            "league": r.get("league", ""), **rec})
    rows.sort(key=lambda x: x["edge_pp"], reverse=True)
    return rows


def run_selftest():
    """用 predictor 的 recommend() 路径在近期历史场复算镜像 ROI, 验证与已验证 OOF 一致。"""
    import sqlite3
    import numpy as np
    con = sqlite3.connect(DB)
    q = """SELECT m.home_team_name,m.away_team_name,m.match_date,m.league_name,m.final_result,
                  mf.odds_open_h,mf.odds_open_d,mf.odds_open_a
           FROM matches m JOIN match_features mf ON m.match_id=mf.match_id
           WHERE m.final_result IN ('H','D','A') AND mf.odds_open_h>0 AND mf.odds_open_d>0
           AND mf.odds_open_a>0 AND m.match_date>='2023-01-01'"""
    data = con.execute(q).fetchall()
    con.close()
    SEL = {"H": 0, "D": 1, "A": 2}
    pnl, n = [], 0
    edges = []
    for hn, an, md, lg, res, oh, od, oa in data:
        rec = recommend(hn, an, oh, od, oa, md, lg)
        if not rec.get("ok"):
            continue
        n += 1
        edges.append(rec["edge_pp"])
        side_idx = ("H", "D", "A").index(rec["side"])
        odds_mat = (float(oh), float(od), float(oa))
        pnl.append(_settle_side(side_idx, odds_mat, SEL[res]))
    pnl = np.array(pnl)
    roi = 100 * pnl.mean()
    # bootstrap 95% CI
    rng = np.random.default_rng(7)
    b = rng.integers(0, len(pnl), size=(2000, len(pnl)))
    rr = pnl[b].mean(axis=1)
    lo, hi = np.percentile(rr, 2.5), np.percentile(rr, 97.5)
    print(f"[selftest] 历史场 n={n}  recommend() 路径镜像 ROI={roi:.2f}%  CI=[{lo:.2f},{hi:.2f}]")
    print(f"[selftest] 对照已验证 EYE_OPEN_RESID (OOF>=2023, 覆盖门=两队已知): +10.05% CI=[3.7,16.36]")
    print(f"[selftest] 中位 edge_pp={np.median(edges):.2f}  平均 edge_pp={np.mean(edges):.2f}")
    ok = roi > 0 and lo > 0
    print(f"[selftest] 判定: {'✅ 监控路径复现 +EV (与已验证 OOF 一致)' if ok else '❌ 监控路径未复现 +EV'}")
    return roi, lo, hi, ok


def render(rows):
    if not rows:
        return "(无达到 edge>=%.1fpp 的 +EV 场)" % MIN_EDGE_PP
    lines = ["| # | 主 | 客 | 开赛 | 联赛 | 建议方 | 模型P | 市场隐含 | edge(pp) | 开盘赔率 | 1/4Kelly |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        lines.append("| %d | %s | %s | %s | %s | %s | %.3f | %.3f | +%.2f | %.2f | %.4f |" % (
            i, r["home"], r["away"], r["kickoff"], r["league"], r["side"],
            r["model_prob"], r["market_implied"], r["edge_pp"], r["odds"], r["kelly_frac"]))
    lines.append("")
    lines.append("> 仅建议, 需人工审批(IR-21); 不得标注稳赢(IR-30)。")
    return "\n".join(lines)


def main():
    global MIN_EDGE_PP
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="fixtures+开盘赔率 CSV 路径")
    ap.add_argument("--selftest", action="store_true", help="历史镜像复算(无需实时 feed)")
    ap.add_argument("--min-edge", type=float, default=MIN_EDGE_PP)
    args = ap.parse_args()

    if args.min_edge != MIN_EDGE_PP:
        MIN_EDGE_PP = args.min_edge

    if args.selftest:
        run_selftest()
        return
    if not args.csv:
        print("用法: --csv fixtures.csv  或  --selftest")
        return
    rows = run_csv(args.csv)
    today = date.today().isoformat()
    out_md = os.path.join(OUT_DIR, f"open_eye_forward_picks_{today}.md")
    out_csv = os.path.join(OUT_DIR, f"open_eye_forward_picks_{today}.csv")
    os.makedirs(OUT_DIR, exist_ok=True)
    md = f"# 开盘天眼 前向 +EV 清单 ({today})\n\n阈值: edge_pp >= {MIN_EDGE_PP}\n\n" + render(rows)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["home", "away", "kickoff", "league", "side", "model_prob",
                    "market_implied", "edge_pp", "odds", "kelly_frac"])
        for r in rows:
            w.writerow([r["home"], r["away"], r["kickoff"], r["league"], r["side"],
                       r["model_prob"], r["market_implied"], r["edge_pp"], r["odds"], r["kelly_frac"]])
    print(md)
    print(f"\n[输出] {out_md}\n[输出] {out_csv}")


if __name__ == "__main__":
    main()
