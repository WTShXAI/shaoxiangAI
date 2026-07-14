#!/usr/bin/env python
"""
scripts/backfill_bet_records.py
================================
决策闭环回补（P0 第三步）：

  读 odds_db/*.json 的真实盘口 + 真实赛果，用与在线预测一致的 pipeline
  重建"价值层"结论（edge/EV/凯利/决策），落库到 bet_records；
  并回补 actual_result / is_correct / actual_score / resolved_at，形成 ROI 闭环。

幂等：以 (home_team, away_team, match_date) 去重 —— 已存在则仅更新赛果，不重复插入。

用法：
  python scripts/backfill_bet_records.py            # 跑全部 odds_db/*.json
  python scripts/backfill_bet_records.py --dry-run  # 只统计不写库

设计说明：
  - 模型概率来自 OIP 比分矩阵（与 bridge_service._live_predict 完全一致）。
  - 已知事实：当赔率存在时，模型对 1X2 无信息优势 → 多数场次 edge≈0 → PASS。
    这恰是系统的诚实边界；真正 edge 来自子市场（OIP 比分 / OU / 双庄平局共识），
    不在本 1X2 价值层范围，需另行量化（见 design 文档 P1）。
"""
import sqlite3
import json
import glob
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from pipeline.deep_report import (compute_value_layer, consensus_probs,
                                  ou_value, draw_consensus_value,
                                  correct_score_scan)
from pipeline.score_model import predict_score
from pipeline.draw_signal import multi_bookmaker_consensus

DB = os.path.join(PROJECT_ROOT, "data", "football_data.db")
ODDS_DIR = os.path.join(PROJECT_ROOT, "odds_db")
BANKROLL = 10000.0
FRAC_KELLY = 0.5
WIN_MAP = {"home": "H", "draw": "D", "away": "A"}


def parse_date(match_time):
    if not match_time:
        return None
    s = str(match_time).replace("Z", "").split("+")[0].split("T")[0]
    return s if len(s) == 10 else str(match_time)[:10]


def build_value_layer(home, away, oh, od, oa, d=None):
    """构建价值层。跨庄共识概率(诚实估计) vs 跨庄最优价。
    单庄时共识=该庄 → edge≈0 → 强制 PASS(edge 不可证伪)。"""
    price_books = [[oh, od, oa]]
    if d:
        for bk in (d.get("odds_1x2_multi") or []):
            bm = bk.get("bookmaker", "")
            if "(" in bm or ")" in bm:      # 跳过让球/变盘线(如 竞彩官方(-1)), 非 1X2 主胜平负
                continue
            h = bk.get("home_live") or bk.get("home_init")
            dr = bk.get("draw_live") or bk.get("draw_init")
            a = bk.get("away_live") or bk.get("away_init")
            if h and dr and a:
                try:
                    hh, dd, aa = float(h), float(dr), float(a)
                    inv = 1.0 / hh + 1.0 / dd + 1.0 / aa
                    if 1.0 < inv < 1.30:    # 合理 1X2 抽水区间, 过滤混入的让球盘(负抽水)
                        price_books.append([hh, dd, aa])
                except (ValueError, TypeError):
                    pass
    best_odds = [max(p[0] for p in price_books),
                 max(p[1] for p in price_books),
                 max(p[2] for p in price_books)]
    single_book = len(price_books) <= 1
    overround = (1.0 / oh + 1.0 / od + 1.0 / oa) - 1.0
    cons = consensus_probs(price_books)   # 跨庄共识隐含概率(诚实估计)
    vl = compute_value_layer(odds=best_odds,
                             model_probs=cons,
                             overround=overround)
    vl["best_odds"] = [round(x, 3) for x in best_odds]
    vl["books_count"] = len(price_books)
    if single_book:
        vl["decision"] = "PASS"
        vl["best_direction"] = "PASS"
        vl["single_book"] = True
        vl["decision_text"] = "PASS · 单庄无独立定价验证，edge 不可证伪→不接盘"
        vl["scenario"] = {"direction": None, "note": "单庄模式: 价值层仅展示 edge/EV, 不下注结论"}
    return vl, (not single_book)


def upsert(cur, home, away, date, vl, oh, od, oa, league, final_result, dry_run):
    """幂等写入：返回 (bet_id, status)。dry_run 时不真正写库。"""
    cur.execute(
        "SELECT bet_id FROM bet_records WHERE home_team=? AND away_team=? AND (match_date=? OR match_date IS NULL) LIMIT 1",
        (home, away, date))
    row = cur.fetchone()

    predicted = vl.get("best_direction")
    if predicted == "PASS":
        predicted = None
    mod = vl.get("model_prob", [0.0, 0.0, 0.0])
    best_edge = vl.get("best_edge_pct", 0.0)
    kelly_half = ev = 0.0
    for rr in vl.get("rows", []):
        if rr["outcome"] == (predicted or ""):
            kelly_half = rr.get("kelly_half", 0.0)
            ev = rr.get("ev", 0.0)
            break
    conf = max(mod) if mod else 0.0

    actual = actual_score = resolved_at = None
    is_correct = None
    if final_result:
        actual = WIN_MAP.get(final_result.get("winner"))
        actual_score = final_result.get("score")
        resolved_at = final_result.get("verified_at")
        if predicted and actual:
            is_correct = 1 if predicted == actual else 0

    notes = f"edge={best_edge:.2f}%, decision={vl.get('decision')}, backfilled"

    if row:
        bet_id = row[0]
        if not dry_run:
            cur.execute(
                """UPDATE bet_records SET
                       predicted_result=?, verdict_text=?, confidence=?,
                       home_prob=?, draw_prob=?, away_prob=?,
                       home_odds=?, draw_odds=?, away_odds=?,
                       value_gap=?, kelly=?, expected_value=?,
                       actual_result=?, is_correct=?, actual_score=?, resolved_at=?,
                       notes=?
                   WHERE bet_id=?""",
                (predicted, vl.get("decision_text", ""), conf,
                 mod[0], mod[1], mod[2], oh, od, oa,
                 round(best_edge, 2), round(kelly_half, 4), round(ev, 4),
                 actual, is_correct, actual_score, resolved_at, notes, bet_id))
        return bet_id, "updated"
    else:
        if not dry_run:
            cur.execute(
                """INSERT INTO bet_records
                   (match_id, home_team, away_team, league, match_date, bet_type, source,
                    predicted_result, verdict_text, confidence,
                    home_prob, draw_prob, away_prob,
                    home_odds, draw_odds, away_odds,
                    value_gap, kelly, expected_value,
                    actual_result, is_correct, actual_score, resolved_at, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (None, home, away, league, date, "recommendation", "prediction",
                 predicted, vl.get("decision_text", ""), conf,
                 mod[0], mod[1], mod[2], oh, od, oa,
                 round(best_edge, 2), round(kelly_half, 4), round(ev, 4),
                 actual, is_correct, actual_score, resolved_at, notes))
            return cur.lastrowid, "inserted"
        return None, "inserted"


def extract_match(d, basename):
    """从多种 odds_db schema 变体中抽取 (home, away, oh, od, oa, date, league, final_result)。"""
    teams = d.get("teams") or {}
    home = d.get("home") or teams.get("home")
    away = d.get("away") or teams.get("away")
    if not (home and away):
        # 退化: 从文件名 X_vs_Y_YYYYMMDD 解析
        base = basename.replace(".json", "")
        if "_vs_" in base:
            parts = base.split("_vs_")
            left = parts[0]
            right = parts[1].split("_")[0] if len(parts) > 1 else ""
            home, away = left, right
    o = d.get("odds_1x2") or {}
    oh, od, oa = o.get("home"), o.get("draw"), o.get("away")
    date = d.get("match_time") or d.get("date") or d.get("kickoff_beijing")
    league = d.get("competition") or d.get("tournament")

    # 赛果: 优先 final_result；其次 result(winner 为队名, 需映射)
    fr = d.get("final_result")
    if not fr:
        res = d.get("result")
        if isinstance(res, dict):
            w = res.get("winner")
            wkey = None
            if home and away and w:
                if w == home:
                    wkey = "home"
                elif w == away:
                    wkey = "away"
                else:
                    wkey = "draw"
            fr = {"winner": wkey, "score": res.get("score"),
                  "verified_at": d.get("captured_at")}
    return home, away, oh, od, oa, date, league, fr


# ───────────────────────────────────────────────────────────────────────────
# P1: 子市场价值层回补 (大小球 / 平局共识 / 波胆)
# 诚实约束: 子市场 edge 只来自跨盘/跨庄价差, 绝不"模型 vs 同源盘"循环论证。
# ───────────────────────────────────────────────────────────────────────────

def _extract_ou(d):
    """从 odds_ou 抽 (line, over_euro, under_euro)。支持两种 schema:
      - 简式: [{line, over, under}]  (over/under 已是欧盘十进制)
      - 多庄亚盘: [{bookmaker, over_live, under_live, line_live, ...}]
        (over/under 为亚盘水位 → 转欧盘 = 水位+1)"""
    ou = d.get("odds_ou")
    if not ou:
        return []
    out = []
    if isinstance(ou, list):
        for item in ou:
            if not isinstance(item, dict):
                continue
            # 简式
            if "over" in item and "under" in item and "line" in item:
                o, u, ln = item["over"], item["under"], item["line"]
                try:
                    o, u = float(o), float(u)
                    o = o + 1.0 if o < 1.5 else o     # 亚盘水位→欧盘
                    u = u + 1.0 if u < 1.5 else u
                    out.append((float(ln), o, u))
                except (ValueError, TypeError):
                    pass
            # 多庄亚盘 (取 line_live + 水位)
            elif "over_live" in item and "under_live" in item:
                ln = item.get("line_live") or item.get("line_init")
                try:
                    o = float(item["over_live"]) + 1.0
                    u = float(item["under_live"]) + 1.0
                    out.append((float(ln), o, u))
                except (ValueError, TypeError):
                    pass
    return out


def _extract_cs_odds(d):
    """从 odds_correct_score 抽 {(i,j): 欧盘十进制赔率} (单庄快照)。"""
    cs = d.get("odds_correct_score")
    if not isinstance(cs, list):
        return {}
    m = {}
    for it in cs:
        if not isinstance(it, dict):
            continue
        sc = it.get("score")
        od = it.get("odds")
        if not sc or not od:
            continue
        try:
            i, j = (int(x) for x in str(sc).split("-"))
            m[(i, j)] = float(od)
        except (ValueError, TypeError):
            pass
    return m


def build_sub_markets(home, away, oh, od, oa, d):
    """构建子市场价值层 (OU / 平局共识 / 波胆)。返回 dict。"""
    sm = {}
    # 比分矩阵 (OIP)
    try:
        r = predict_score(home, away, oh, od, oa)
        M = r["matrix"].tolist()
    except Exception:
        M = None

    # ① 大小球 (跨市场不一致)
    ou_list = _extract_ou(d)
    if ou_list and M is not None:
        # 多盘口取与主盘1X2期望总进球最接近的一个 (避免乱选)
        line, over, under = ou_list[0]
        try:
            sm["ou"] = ou_value(oh, od, oa, line, over, under, model_m=M)
        except Exception:
            pass

    # ② 平局共识 (跨庄溢价, 需 ≥2 家独立 1X2 盘)
    books = [[oh, od, oa]]
    for bk in (d.get("odds_1x2_multi") or []):
        bm = bk.get("bookmaker", "")
        if "(" in bm or ")" in bm:
            continue
        h = bk.get("home_live") or bk.get("home_init")
        dr = bk.get("draw_live") or bk.get("draw_init")
        a = bk.get("away_live") or bk.get("away_init")
        if h and dr and a:
            try:
                hh, dd, aa = float(h), float(dr), float(a)
                inv = 1.0 / hh + 1.0 / dd + 1.0 / aa
                if 1.0 < inv < 1.30:
                    books.append([hh, dd, aa])
            except (ValueError, TypeError):
                pass
    if len(books) >= 2:
        try:
            cons = multi_bookmaker_consensus([(str(i), b[0], b[1], b[2]) for i, b in enumerate(books)])
            if cons.get("available"):
                best_draw = min(b[1] for b in books)
                sm["draw"] = draw_consensus_value(
                    oh, od, oa, consensus_pd=cons["mean_pd"],
                    strong=cons.get("strong", False), best_draw_odds=best_draw)
        except Exception:
            pass

    # ③ 波胆 (单庄快照无跨庄最优价 → 只扫描 fair value, 不宣称 edge;
    #     跨庄 soft-line edge 须多庄 CS 赔率, odds_db 无 → 留待在线多庄场景)
    if M is not None:
        try:
            sm["correct_score_scan"] = correct_score_scan(M, top_n=3)
        except Exception:
            pass
    return sm


def ensure_submarket_table(cur):
    cur.execute(
        """CREATE TABLE IF NOT EXISTS submarket_bets (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               home_team TEXT, away_team TEXT, league TEXT, match_date TEXT,
               market TEXT, selection TEXT, model_prob REAL, best_odds REAL,
               value_gap REAL, kelly REAL, expected_value REAL,
               decision TEXT, decision_text TEXT,
               actual_result TEXT, is_correct INTEGER,
               actual_score TEXT, resolved_at TEXT, notes TEXT,
               created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")


def upsert_submarket(cur, home, away, date, league, sm, dry_run, fr=None):
    """将子市场结论(含 PASS/SCAN)落 submarket_bets, 返回 (写库行数, BET行数)。
    幂等: 先删该场旧子市场行, 再插入, 避免重复运行累积重复。
    fr = final_result dict (若 odds_db 有赛果) → 一并回补 actual_result/is_correct/actual_score,
    使子市场也形成 ROI 闭环 (P2)。"""
    rows_written = 0
    bet_count = 0
    if not dry_run:
        # 幂等: 先删该场旧子市场行(用 COALESCE 兼容 date 为 None 的情况), 再插入, 避免累积重复
        cur.execute(
            "DELETE FROM submarket_bets WHERE home_team=? AND away_team=? AND COALESCE(match_date,'')=COALESCE(?, '')",
            (home, away, date))
    # 赛果映射 (与 bet_records 一致)
    actual = actual_score = resolved_at = None
    if fr:
        actual = WIN_MAP.get(fr.get("winner"))
        actual_score = fr.get("score")
        resolved_at = fr.get("verified_at")

    def write(market, selection, model_prob, best_odds, ev, kelly, decision, text):
        nonlocal rows_written, bet_count
        # 子市场命中判定 (用于 is_correct)
        ic = None
        if actual is not None and decision == "BET":
            if market == "OU":
                side = "over" if selection.startswith("over") else ("under" if selection.startswith("under") else None)
                m = None
                import re as _re
                mm = _re.search(r"([\d.]+)$", str(selection))
                line = float(mm.group(1)) if mm else None
                tg = None
                ps = _re.search(r"(\d+)\s*[-:]\s*(\d+)", str(actual_score or ""))
                if ps:
                    tg = int(ps.group(1)) + int(ps.group(2))
                if side and line is not None and tg is not None:
                    ic = 1 if ((tg > line) if side == "over" else (tg < line)) else 0
            elif market == "DRAW_CONSENSUS":
                ic = 1 if actual == "D" else 0
            elif market == "CS":
                import re as _re
                a = _re.search(r"(\d+)\s*[-:]\s*(\d+)", str(selection))
                b = _re.search(r"(\d+)\s*[-:]\s*(\d+)", str(actual_score or ""))
                if a and b:
                    ic = 1 if (a.group(1) == b.group(1) and a.group(2) == b.group(2)) else 0
        if dry_run:
            rows_written += 1
            if decision == "BET":
                bet_count += 1
            return
        cur.execute(
            """INSERT INTO submarket_bets
               (home_team, away_team, league, match_date, market, selection,
                model_prob, best_odds, value_gap, kelly, expected_value,
                decision, decision_text, actual_result, is_correct, actual_score, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (home, away, league, date, market, selection,
             model_prob, best_odds, round((ev or 0), 4), round((kelly or 0), 4),
             round((ev or 0), 4), decision, text,
             actual, ic, actual_score, resolved_at))
        rows_written += 1
        if decision == "BET":
            bet_count += 1

    # 大小球
    ou = sm.get("ou")
    if ou:
        sc = ou.get("scenario", {})
        side = sc.get("side")
        if side:
            odds = ou.get("over_odds") if side == "over" else ou.get("under_odds")
            sel = f"{side}_{ou.get('ou_line')}"
            mp = ou.get("model_p_over") if side == "over" else ou.get("model_p_under")
            ev = ou.get("ev_over_pct") if side == "over" else ou.get("ev_under_pct")
            write("OU", sel, mp, odds, ev, None, ou.get("decision"), ou.get("decision_text", ""))
        else:
            write("OU", f"_{ou.get('ou_line')}", ou.get("model_p_over"), None,
                  None, None, "PASS", ou.get("decision_text", ""))
    # 平局共识
    dr = sm.get("draw")
    if dr:
        write("DRAW_CONSENSUS", "D", dr.get("consensus_pd"), dr.get("best_odds"),
              dr.get("ev_pct"), None, dr.get("decision"), dr.get("decision_text", ""))
    # 波胆 (value 或 scan)
    cs = sm.get("correct_score")
    if isinstance(cs, dict):
        if cs.get("decision") == "BET":
            for r in cs.get("rows", [])[:1]:
                write("CS", r.get("score"), r.get("prob"), r.get("odds"),
                      r.get("ev_pct"), r.get("kelly_half"), "BET", cs.get("decision_text", ""))
        else:
            write("CS", "scan", None, None, None, None, "SCAN",
                  cs.get("decision_text", "单庄快照, 仅扫描"))
    cs_scan = sm.get("correct_score_scan")
    if isinstance(cs_scan, list):
        write("CS", "scan", None, None, None, None, "SCAN", "单庄快照, 仅扫描 top3")
    return rows_written, bet_count


def main():
    dry_run = "--dry-run" in sys.argv
    files = glob.glob(os.path.join(ODDS_DIR, "*.json"))
    files = [f for f in files
             if os.path.basename(f) not in ("schema.json", "index.json")]
    con = sqlite3.connect(DB)
    cur = con.cursor()
    ensure_submarket_table(cur)
    stats = {"processed": 0, "inserted": 0, "updated": 0, "skipped": 0, "with_result": 0, "model_ok": 0}
    sm_stats = {"rows": 0, "bets": 0}

    for f in files:
        basename = os.path.basename(f)
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            stats["skipped"] += 1
            continue
        if not isinstance(d, dict):
            stats["skipped"] += 1
            continue
        home, away, oh, od, oa, date, league, fr = extract_match(d, basename)
        if not (oh and od and oa and home and away):
            stats["skipped"] += 1
            continue
        date = parse_date(date)
        vl, used_model = build_value_layer(home, away, oh, od, oa, d)
        if used_model:
            stats["model_ok"] += 1
        _, status = upsert(cur, home, away, date, vl, oh, od, oa, league, fr, dry_run)
        stats["processed"] += 1
        stats[status] += 1
        if fr:
            stats["with_result"] += 1
        # P1: 子市场价值层 (大小球/平局共识/波胆) — 并回补赛果形成 ROI 闭环 (P2)
        sm = build_sub_markets(home, away, oh, od, oa, d)
        rw, bc = upsert_submarket(cur, home, away, date, league, sm, dry_run, fr)
        sm_stats["rows"] += rw
        sm_stats["bets"] += bc

    if not dry_run:
        con.commit()
    print(f"{'[DRY-RUN] ' if dry_run else ''}回补完成:")
    print(f"  处理 {stats['processed']} 场 | 新增 {stats['inserted']} | 更新 {stats['updated']} "
          f"| 跳过 {stats['skipped']} | 含赛果 {stats['with_result']} | 模型可用 {stats['model_ok']}")
    print(f"  子市场记录: {sm_stats['rows']} 行 (其中 BET {sm_stats['bets']} 条)")
    if not dry_run:
        from pipeline.roi_report import print_summary
        print_summary(cur)
    con.close()


if __name__ == "__main__":
    main()
