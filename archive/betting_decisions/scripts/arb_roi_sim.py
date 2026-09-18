#!/usr/bin/env python3
"""arb_roi_sim.py — 跨庄套利 ROI 模拟器
=======================================
从"预测工具"到"套利系统"的切换脚本。

功能:
  1. 跨庄最优价 (cross_book_edge._best) → EV/edge 计算
  2. EV > 0 门控 + 跨庄分歧 ≥ 0.10 过滤
  3. 按 EV 阈值分层报告 ROI
  4. 并排单庄 GQ 对比 (同场同模型, 换赔率源)
  5. AH 市场验证: Q1/Q5 两端 + 让球方/受让方 baseline
  6. ROI 对比: 单庄 GQ vs 跨庄最优价
"""
import sys, os, sqlite3, json, math
from datetime import datetime
from collections import defaultdict
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
from sklearn.metrics import roc_auc_score

FEATURE_DB = os.path.join(ROOT, "data", "shaoxiang_feature_library.db")
GQ_DB = os.path.join(ROOT, "data", "events.db")
FOOTBALL_DB = os.path.join(ROOT, "data", "football_data.db")
OUTPUT = os.path.join(ROOT, "分析报告", "arb_roi_report.md")
CS_CALIB = os.path.join(ROOT, "data", "cs_calibration.json")

N_TEST_FRAC = 0.20
MIN_TEST = 50
KELLY_FRAC = 0.25
MIN_SPREAD_PP = 0.10   # 跨庄分歧阈值 (≥10pp 才用跨庄最优价)
EV_TIERS = [(0.0, 0.05, "边缘"), (0.05, 0.10, "正期望"), (0.10, 0.20, "强期望"), (0.20, 99.0, "极高期望")]

# ─────── 数据加载 ───────

def load_match_data():
    """加载: 特征库标签 + match_outcomes 单庄赔率 + leisu 多庄数据."""
    feat_db = sqlite3.connect(FEATURE_DB)
    gq_db = sqlite3.connect(GQ_DB)

    # 特征库有标签的行 (无 home/away, 从 match_outcomes 补)
    feat_rows = feat_db.execute("""
        SELECT source, league, kickoff,
               label_1x2, label_ou, label_ah,
               x1_h, x1_d, x1_a, xou_line
        FROM features WHERE label_1x2 IS NOT NULL
        ORDER BY kickoff
    """).fetchall()

    # match_outcomes 索引
    mo_rows = gq_db.execute("""
        SELECT source, league, kickoff, home, away,
               op_1x2_h, op_1x2_d, op_1x2_a,
               op_ou_line, op_ou_over, op_ou_under,
               op_ah_line, op_ah_home, op_ah_away,
               score_home, score_away
        FROM match_outcomes
        WHERE op_1x2_h IS NOT NULL
          AND score_home IS NOT NULL
    """).fetchall()

    feat_db.close()
    gq_db.close()

    # 建 match_outcomes 索引: (source, league, kickoff) → row
    mo_index = {}
    for r in mo_rows:
        key = (r[0] or "", (r[1] or "").strip(), (r[2] or "").strip())
        if key not in mo_index:
            mo_index[key] = r

    data = []
    for fr in feat_rows:
        key = (fr[0] or "", (fr[1] or "").strip(), (fr[2] or "").strip())
        mo = mo_index.get(key)
        if not mo:
            continue
        # mo: source(0), league(1), kickoff(2), home(3), away(4),
        #   op_1x2_h(5), op_1x2_d(6), op_1x2_a(7),
        #   op_ou_line(8), op_ou_over(9), op_ou_under(10),
        #   op_ah_line(11), op_ah_home(12), op_ah_away(13),
        #   score_home(14), score_away(15)
        sh, sa = int(mo[14]), int(mo[15])
        label_1x2 = fr[3]  # 0=H,1=D,2=A
        expected_1x2 = 0 if sh > sa else (1 if sh == sa else 2)
        if expected_1x2 != label_1x2:
            continue

        data.append({
            "source": fr[0], "league": fr[1] or "", "kickoff": fr[2],
            "home": mo[3] or "", "away": mo[4] or "",
            # 单庄 GQ 赔率
            "h": mo[5], "d": mo[6], "a": mo[7],
            "ou_line": mo[8], "ou_over": mo[9], "ou_under": mo[10],
            "ah_line": mo[11], "ah_home": mo[12], "ah_away": mo[13],
            # 标签
            "score_home": sh, "score_away": sa,
            "label_1x2": label_1x2,
            "label_ou": fr[4],
            "label_ah": fr[5],
            # 特征库已有的去水概率
            "x1_h": fr[6], "x1_d": fr[7], "x1_a": fr[8],
            "xou_line": fr[9],
        })

    return data


def load_leisu_index():
    """加载 leisu 多庄数据, 按 (home, away) 建索引 → list[{bookmaker, h, d, a}]."""
    db = sqlite3.connect(FOOTBALL_DB)
    rows = db.execute("""
        SELECT home_raw, away_raw, book, odds_h, odds_d, odds_a
        FROM leisu_odds WHERE market='1X2'
          AND odds_h > 0 AND odds_d > 0 AND odds_a > 0
    """).fetchall()
    db.close()

    idx = defaultdict(list)
    for home, away, bm, oh, od, oa in rows:
        idx[(home.strip(), away.strip())].append({"book": bm, "h": oh, "d": od, "a": oa})
    return idx


def load_ah_data():
    """从特征库取有 AH 标签的场次 + match_outcomes AH 赔率."""
    feat_db = sqlite3.connect(FEATURE_DB)
    gq_db = sqlite3.connect(GQ_DB)

    feat_rows = feat_db.execute("""
        SELECT source, league, kickoff, label_1x2, label_ah
        FROM features WHERE label_ah IS NOT NULL
        ORDER BY kickoff
    """).fetchall()

    mo_rows = gq_db.execute("""
        SELECT source, league, kickoff, home, away,
               op_1x2_h, op_1x2_d, op_1x2_a,
               op_ah_line, op_ah_home, op_ah_away,
               score_home, score_away
        FROM match_outcomes
        WHERE op_ah_line IS NOT NULL
          AND score_home IS NOT NULL
    """).fetchall()

    feat_db.close()
    gq_db.close()

    mo_index = {}
    for r in mo_rows:
        key = (r[0] or "", (r[1] or "").strip(), (r[2] or "").strip())
        if key not in mo_index:
            mo_index[key] = r

    data = []
    for fr in feat_rows:
        key = (fr[0] or "", (fr[1] or "").strip(), (fr[2] or "").strip())
        mo = mo_index.get(key)
        if not mo:
            continue
        # mo: source(0), league(1), kickoff(2), home(3), away(4),
        #   op_1x2_h(5), op_1x2_d(6), op_1x2_a(7),
        #   op_ah_line(8), op_ah_home(9), op_ah_away(10),
        #   score_home(11), score_away(12)
        sh, sa = int(mo[11]), int(mo[12])
        label_1x2 = fr[3]
        expected_1x2 = 0 if sh > sa else (1 if sh == sa else 2)
        if expected_1x2 != label_1x2:
            continue

        data.append({
            "kickoff": fr[2],
            "home": mo[3], "away": mo[4],
            "h": mo[5], "d": mo[6], "a": mo[7],
            "ah_line": mo[8], "ah_home": mo[9], "ah_away": mo[10],
            "score_home": sh, "score_away": sa,
            "label_ah": fr[4],  # 0=H(home), 1=A(away)
            "label_1x2": label_1x2,
        })

    return data


# ─────── 跨庄工具 ───────

def cross_book_best(match_data, leisu_idx):
    """取跨庄最优赔率 + 共识概率 + 离散度."""
    home = match_data["home"].strip()
    away = match_data["away"].strip()
    key = (home, away)

    # 尝试精确匹配
    books = leisu_idx.get(key)
    if not books:
        # 尝试模糊匹配
        for (lh, la), b in leisu_idx.items():
            if (home in lh or lh in home) and (away in la or la in away):
                books = b
                break
    if not books or len(books) < 2:
        return None  # 至少 2 家才叫跨庄

    # 去水概率
    devigged = []
    for b in books:
        try:
            inv = 1.0 / b["h"] + 1.0 / b["d"] + 1.0 / b["a"]
            devigged.append({"book": b["book"],
                             "h_prob": (1.0 / b["h"]) / inv,
                             "d_prob": (1.0 / b["d"]) / inv,
                             "a_prob": (1.0 / b["a"]) / inv,
                             "raw_h": b["h"], "raw_d": b["d"], "raw_a": b["a"]})
        except (ZeroDivisionError, KeyError):
            continue

    if len(devigged) < 2:
        return None

    # 共识 = 去水概率中位数
    h_probs = sorted(d["h_prob"] for d in devigged)
    d_probs = sorted(d["d_prob"] for d in devigged)
    a_probs = sorted(d["a_prob"] for d in devigged)
    n = len(devigged)
    consensus = {
        "h": h_probs[n // 2], "d": d_probs[n // 2], "a": a_probs[n // 2]
    }

    # 最优赔率 = 各家最大原始赔率
    best_h = max(d["raw_h"] for d in devigged)
    best_d = max(d["raw_d"] for d in devigged)
    best_a = max(d["raw_a"] for d in devigged)

    # 离散度 = 去水概率极差
    spread_pp = max(
        (h_probs[-1] - h_probs[0]) * 100,
        (d_probs[-1] - d_probs[0]) * 100,
        (a_probs[-1] - a_probs[0]) * 100,
    )

    return {
        "n_books": len(devigged),
        "best_h": best_h, "best_d": best_d, "best_a": best_a,
        "consensus": consensus,
        "spread_pp": spread_pp,
        "bookmakers": [d["book"] for d in devigged],
    }


# ─────── 预测 ───────

def run_predictions(data, max_n=None):
    """跑 ranked_predictor, 返回概率."""
    from pipeline.ranked_predictor import predict as ranked_predict

    records = []
    for i, d in enumerate(data):
        if max_n and i >= max_n:
            break
        try:
            kwargs = {"h": d["h"], "d": d["d"], "a": d["a"], "league": d.get("league", "")}
            if d.get("ou_line") and d.get("ou_over") and d.get("ou_under"):
                kwargs["ou_line"] = d["ou_line"]
            if d.get("ah_line") is not None and d.get("ah_home"):
                kwargs["ah_line"] = d["ah_line"]
                kwargs["ah_home"] = d["ah_home"]
                kwargs["ah_away"] = d.get("ah_away")

            r = ranked_predict(d["home"], d["away"], **kwargs)
            if not r or "markets" not in r:
                continue

            m1x2 = r["markets"].get("1x2", {})
            mou = r["markets"].get("ou", {})
            mah = r["markets"].get("ah", {})
            # AH: convert p_fav/p_dog to p_home_cover
            p_ah_home = None
            if mah and mah.get("fav_side"):
                fav_side = mah.get("fav_side", "")
                p_fav = float(mah.get("p_fav", 0.5) or 0.5)
                p_dog = float(mah.get("p_dog", 0.5) or 0.5)
                p_ah_home = p_fav if fav_side == "主队" else p_dog

            records.append({
                "home": d["home"], "away": d["away"], "kickoff": d.get("kickoff", ""),
                "actual_1x2": d["label_1x2"],
                "actual_ou": d.get("label_ou"),
                "actual_ah": d.get("label_ah"),
                "p_h": float(m1x2.get("p_h", 0) or 0),
                "p_d": float(m1x2.get("p_d", 0) or 0),
                "p_a": float(m1x2.get("p_a", 0) or 0),
                "p_over": float(mou.get("p_over", 0.5) or 0.5),
                "p_under": float(mou.get("p_under", 0.5) or 0.5),
                "p_ah_home": p_ah_home,
                "gq_h": d["h"], "gq_d": d["d"], "gq_a": d["a"],
                "gq_ou_line": d.get("ou_line"),
                "gq_ah_line": d.get("ah_line"),
            })

            if (i + 1) % 200 == 0:
                print(f"  预测进度: {i + 1}/{len(data)}")
        except Exception as e:
            continue

    return records


# ─────── 套利 ROI 模拟 ───────

def simulate_arb_roi(records, cross_book_data, bankroll=1000):
    """跨庄最优价 ROI 模拟, 按 EV 分层 + 并排单庄对比.

    cross_book_data: dict[match_key → cross_book dict] 或 None.
    """
    # 分层统计
    tier_stats = {name: {"n": 0, "n_correct": 0, "bank_cross": bankroll, "bank_gq": bankroll,
                          "stakes_cross": 0, "stakes_gq": 0}
                  for _, _, name in EV_TIERS}
    # 全局
    bank_cross, bank_gq = bankroll, bankroll
    n_bets_cross, n_bets_gq = 0, 0
    n_cross_used = 0

    for r in records:
        probs = [r["p_h"], r["p_d"], r["p_a"]]
        gq_odds = [r["gq_h"], r["gq_d"], r["gq_a"]]
        pred = np.argmax(probs)
        pred_prob = probs[pred]
        actual = r["actual_1x2"]

        # ── 跨庄路径 ──
        cb = cross_book_data.get((r["home"].strip(), r["away"].strip())) if cross_book_data else None
        use_cross = False
        if cb and cb["spread_pp"] >= MIN_SPREAD_PP * 100:  # spread_pp 是百分比
            use_cross = True
            n_cross_used += 1
            cb_best = [cb["best_h"], cb["best_d"], cb["best_a"]]
            cross_odd = cb_best[pred]
            # 共识概率
            cons = cb["consensus"]
            cons_probs = [cons["h"], cons["d"], cons["a"]]
            market_prob = cons_probs[pred]
        else:
            cross_odd = gq_odds[pred]
            market_prob = 1.0 / gq_odds[pred]  # 粗略

        # EV = 模型概率 × 最优赔率 − 1
        ev_cross = pred_prob * cross_odd - 1

        # ── 单庄 GQ 路径 (对照) ──
        gq_odd = gq_odds[pred]
        ev_gq = pred_prob * gq_odd - 1

        # 确定 EV 层级
        tier_name = None
        for lo, hi, name in EV_TIERS:
            if ev_cross >= lo and ev_cross < hi:
                tier_name = name
                break

        # ── 下注模拟 ──
        # 跨庄: EV > 0 + 分歧 ≥ 0.10 才下注
        if ev_cross > 0 and use_cross:
            kelly_bet = bank_cross * KELLY_FRAC * (ev_cross / (cross_odd - 1)) if cross_odd > 1 else 0
            kelly_bet = min(kelly_bet, bank_cross * 0.25)
            kelly_bet = max(kelly_bet, 0)
            n_bets_cross += 1
            if actual == pred:
                bank_cross += kelly_bet * (cross_odd - 1)
                if tier_name:
                    tier_stats[tier_name]["n_correct"] += 1
            else:
                bank_cross -= kelly_bet
            if tier_name:
                tier_stats[tier_name]["n"] += 1
                tier_stats[tier_name]["bank_cross"] = bank_cross
                tier_stats[tier_name]["stakes_cross"] += kelly_bet

        # 单庄 (对照): 不做交叉门控, 仅 EV > 0
        if ev_gq > 0:
            kelly_bet_gq = bank_gq * KELLY_FRAC * (ev_gq / (gq_odd - 1)) if gq_odd > 1 else 0
            kelly_bet_gq = min(kelly_bet_gq, bank_gq * 0.25)
            kelly_bet_gq = max(kelly_bet_gq, 0)
            n_bets_gq += 1
            if actual == pred:
                bank_gq += kelly_bet_gq * (gq_odd - 1)
            else:
                bank_gq -= kelly_bet_gq
            if tier_name:
                tier_stats[tier_name]["bank_gq"] = bank_gq
                tier_stats[tier_name]["stakes_gq"] += kelly_bet_gq

    return {
        "bank_cross": bank_cross, "roi_cross": (bank_cross - bankroll) / bankroll,
        "bank_gq": bank_gq, "roi_gq": (bank_gq - bankroll) / bankroll,
        "n_bets_cross": n_bets_cross, "n_bets_gq": n_bets_gq,
        "n_cross_used": n_cross_used,
        "tier_stats": tier_stats,
    }


# ─────── AH 验证 ───────

def run_ah_validation(data):
    """AH 市场验证: 时间序列切分, Q1/Q5 两端, vs baseline."""
    from pipeline.ranked_predictor import predict as ranked_predict

    data.sort(key=lambda x: x["kickoff"] or "")
    split = int(len(data) * (1 - N_TEST_FRAC))
    test_data = data[split:]

    records = []
    for d in test_data:
        try:
            r = ranked_predict(
                d["home"], d["away"],
                h=d["h"], d=d["d"], a=d["a"],
                ah_line=d["ah_line"], ah_home=d["ah_home"], ah_away=d["ah_away"],
                league=d.get("league", ""),
            )
            if not r or "markets" not in r:
                continue
            mah = r["markets"].get("ah", {})
            if not mah:
                continue

            # AH returns p_fav/p_dog/fav_side, need to convert to p_home_cover
            fav_side = mah.get("fav_side", "")
            p_fav = float(mah.get("p_fav", 0.5) or 0.5)
            p_dog = float(mah.get("p_dog", 0.5) or 0.5)
            if fav_side == "主队":
                p_ah_home = p_fav
            elif fav_side == "客队":
                p_ah_home = p_dog
            else:
                p_ah_home = 0.5

            records.append({
                "home": d["home"], "away": d["away"],
                "ah_line": d["ah_line"],
                "actual_ah": d["label_ah"],  # 0=H(主队赢盘), 1=A(客队赢盘)
                "p_ah_home": p_ah_home,
                "score_home": d["score_home"],
                "score_away": d["score_away"],
            })
        except Exception:
            continue

    if len(records) < 20:
        return None

    # 按 p_ah_home 排序 → Q1(低)/Q5(高)
    sorted_rec = sorted(records, key=lambda x: x["p_ah_home"])
    n = len(sorted_rec)
    q1_end = n // 5
    q5_start = n - q1_end
    q1 = sorted_rec[:q1_end]
    q5 = sorted_rec[q5_start:]

    # 命中率: AH 预测让球方 (p_ah_home > 0.5 → 预测 H=0, else A=1)
    def ah_acc(recs):
        if not recs:
            return 0, 0
        correct = sum(1 for r in recs if (r["p_ah_home"] > 0.5 and r["actual_ah"] == 0) or
                                        (r["p_ah_home"] <= 0.5 and r["actual_ah"] == 1))
        return correct, len(recs)

    q1_c, q1_n = ah_acc(q1)
    q5_c, q5_n = ah_acc(q5)
    all_c, all_n = ah_acc(records)

    # Baseline: 永远让球方 / 受让方
    home_side = sum(1 for r in records if r["actual_ah"] == 0)  # 让球方赢
    home_side_pct = home_side / all_n
    away_side_pct = 1 - home_side_pct

    # Q1/Q5 AH line 分布
    q1_lines = [r["ah_line"] for r in q1]
    q5_lines = [r["ah_line"] for r in q5]

    return {
        "n_total": all_n,
        "all_acc": f"{all_c}/{all_n} ({all_c/all_n*100:.1f}%)",
        "q1_acc": f"{q1_c}/{q1_n} ({q1_c/q1_n*100:.1f}%)",
        "q5_acc": f"{q5_c}/{q5_n} ({q5_c/q5_n*100:.1f}%)",
        "q1_q5_spread": (q5_c/q5_n - q1_c/q1_n) * 100 if q1_n and q5_n else 0,
        "baseline_home": f"{home_side_pct*100:.1f}% (让球方)",
        "baseline_away": f"{away_side_pct*100:.1f}% (受让方)",
        "q1_ah_lines": f"{min(q1_lines):+.2f}~{max(q1_lines):+.2f} (n={q1_n})",
        "q5_ah_lines": f"{min(q5_lines):+.2f}~{max(q5_lines):+.2f} (n={q5_n})",
        "records": records,
    }


# ─────── 报告 ───────

def generate_report(arb_result, ah_result, data_info):
    lines = []
    lines.append("# 哨响AI 套利系统验证报告")
    lines.append(f"\n**从预测工具到套利系统的切换评估**")
    lines.append(f"\n生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"\n## 数据概况")
    lines.append(f"- 全量匹配: {data_info['total_matched']} 场")
    lines.append(f"- 测试集 (时间序列后 {N_TEST_FRAC:.0%}): {data_info['test_n']} 场")
    lines.append(f"- 跨庄可用 (leisu 多庄枚举): {data_info['n_leisu_groups']} 组")
    lines.append(f"- 跨庄有效 (≥2 庄家可对齐): {data_info['n_cross_valid']} 场")
    lines.append(f"- 跨庄分歧 ≥{MIN_SPREAD_PP*100:.0f}pp: {arb_result['n_cross_used']} 场")

    # ── ROI 主表 ──
    lines.append("\n---")
    lines.append("\n## ROI 对比: 单庄 GQ vs 跨庄最优价")
    lines.append("")
    lines.append(f"| 指标 | 跨庄最优价 | 单庄 GQ | 差额 |")
    lines.append(f"|------|----------|--------|------|")
    lines.append(f"| 下注次数 | {arb_result['n_bets_cross']} | {arb_result['n_bets_gq']} | — |")
    lines.append(f"| 终值 (初始1000) | {arb_result['bank_cross']:.2f} | {arb_result['bank_gq']:.2f} | {arb_result['bank_cross']-arb_result['bank_gq']:+.2f} |")
    lines.append(f"| ROI | {arb_result['roi_cross']:+.2%} | {arb_result['roi_gq']:+.2%} | {arb_result['roi_cross']-arb_result['roi_gq']:+.2%} |")

    # ── EV 分层 ──
    lines.append("\n### EV 阈值分层 ROI")
    lines.append("")
    lines.append(f"| EV 层级 | 场数 | 命中率 | 跨庄终值 | 跨庄 ROI | 单庄终值 | 单庄 ROI |")
    lines.append(f"|--------|------|--------|---------|---------|---------|---------|")
    for lo, hi, name in EV_TIERS:
        ts = arb_result["tier_stats"][name]
        if ts["n"] == 0:
            continue
        hit_rate = ts["n_correct"] / ts["n"] if ts["n"] else 0
        roi_c = (ts["bank_cross"] - 1000) / 1000 if ts["stakes_cross"] > 0 else 0
        roi_g = (ts["bank_gq"] - 1000) / 1000 if ts["stakes_gq"] > 0 else 0
        lines.append(f"| {name} (EV {lo:.2f}~{hi:.2f}) | {ts['n']} | {hit_rate:.1%} | {ts['bank_cross']:.2f} | {roi_c:+.2%} | {ts['bank_gq']:.2f} | {roi_g:+.2%} |")

    # ── AH 验证 ──
    if ah_result:
        lines.append("\n---")
        lines.append("\n## AH 市场验证 (Q1/Q5 两端)")
        lines.append("")
        lines.append(f"| 指标 | 值 |")
        lines.append(f"|------|-----|")
        lines.append(f"| 总场数 | {ah_result['n_total']} |")
        lines.append(f"| 全量命中率 | {ah_result['all_acc']} |")
        lines.append(f"| Q1 命中率 (模型最不看好让球方) | **{ah_result['q1_acc']}** |")
        lines.append(f"| Q5 命中率 (模型最看好让球方) | **{ah_result['q5_acc']}** |")
        lines.append(f"| Q1→Q5 单调差 | {ah_result['q1_q5_spread']:+.1f}pp |")
        lines.append(f"| Baseline 让球方 | {ah_result['baseline_home']} |")
        lines.append(f"| Baseline 受让方 | {ah_result['baseline_away']} |")
        lines.append(f"| Q1 AH 盘口范围 | {ah_result['q1_ah_lines']} |")
        lines.append(f"| Q5 AH 盘口范围 | {ah_result['q5_ah_lines']} |")

        ah_recs = ah_result["records"]
        q1_acc = float(ah_result["q1_acc"].split("(")[1].rstrip("%)"))
        q5_acc = float(ah_result["q5_acc"].split("(")[1].rstrip("%)"))
        lines.append("")
        if q5_acc > q1_acc + 3:
            lines.append("✅ **AH 分箱单调, 模型概率有区分度**")
        elif q5_acc > q1_acc:
            lines.append("⚠️ AH 分箱微弱单调, 区分度不足")
        else:
            lines.append("❌ AH 分箱不单调, 模型概率无区分度")

    # ── 风险提示 ──
    lines.append("\n---")
    lines.append("\n## ⚠️ 关键约束")
    lines.append("")
    lines.append("- **跨庄数据冻结**: leisu_odds 仅 44 组 (MuMu 离线), 跨庄 ROI 基于历史数据, 非实时")
    lines.append("- **分歧阈值 0.10**: 仅 ≥10pp 跨庄共识离散度才用最优价, 否则回退单庄")
    lines.append("- **Kelly 25% fractional**: 安全边际, 真实交易可调")
    lines.append("- **AH 仅取 Q1/Q5**: 符合铁律8 \"AH 只吃两端\", 中间箱区分度弱")
    lines.append("- **EV > 0 门控**: 负期望不下注, 是套利系统的基本纪律")

    return "\n".join(lines)


# ─────── main ───────

def main():
    print("=" * 64)
    print("arb_roi_sim.py — 跨庄套利 ROI 模拟器")
    print("=" * 64)

    # 1. 加载数据
    print("\n[1/5] 加载数据...")
    data = load_match_data()
    data.sort(key=lambda x: x["kickoff"] or "")
    split = int(len(data) * (1 - N_TEST_FRAC))
    test_data = data[split:]

    leisu_idx = load_leisu_index()
    print(f"  特征库匹配: {len(data)} 场, 测试集: {len(test_data)} 场")
    print(f"  leisu 多庄组: {len(leisu_idx)} 组")

    # 2. 跑预测
    print(f"\n[2/5] 对测试集 {len(test_data)} 场跑预测...")
    records = run_predictions(test_data)
    print(f"  预测成功: {len(records)} 场")

    # 3. 跨庄数据匹配
    print("\n[3/5] 匹配跨庄最优价...")
    cross_book_data = {}
    n_cross = 0
    for r in records:
        cb = cross_book_best(r, leisu_idx)
        if cb:
            cross_book_data[(r["home"].strip(), r["away"].strip())] = cb
            n_cross += 1
    print(f"  跨庄可用: {n_cross} 场")

    # 4. 套利 ROI 模拟
    print("\n[4/5] ROI 模拟...")
    arb_result = simulate_arb_roi(records, cross_book_data)
    print(f"  跨庄 ROI: {arb_result['roi_cross']:+.2%} ({arb_result['n_bets_cross']} 注)")
    print(f"  单庄 ROI: {arb_result['roi_gq']:+.2%} ({arb_result['n_bets_gq']} 注)")

    # 5. AH 验证
    print("\n[5/5] AH 验证...")
    ah_data = load_ah_data()
    ah_result = run_ah_validation(ah_data) if ah_data else None
    if ah_result:
        print(f"  AH 测试: {ah_result['n_total']} 场, Q1={ah_result['q1_acc']}, Q5={ah_result['q5_acc']}")
    else:
        print("  AH 数据不足, 跳过")

    # 6. 报告
    info = {
        "total_matched": len(data),
        "test_n": len(records),
        "n_leisu_groups": len(leisu_idx),
        "n_cross_valid": n_cross,
    }
    report = generate_report(arb_result, ah_result, info)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n✅ 报告: {OUTPUT}")

    # 打印 CS 校准文件内容
    print("\n" + "=" * 64)
    print("cs_calibration.json 内容:")
    print("=" * 64)
    calib = json.loads(Path(CS_CALIB).read_text(encoding="utf-8"))
    print(json.dumps(calib, ensure_ascii=False, indent=2))
    print("=" * 64)

    return arb_result, ah_result, report


if __name__ == "__main__":
    main()
