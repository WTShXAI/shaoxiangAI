"""
哨响AI 世界杯特征补算 (backfill_wc_features.py)
================================================
给世界杯2026的117场比赛补算特征写入 match_features 表。

赔率策略(混合):
  1. 真实赔率优先(从内置 ODDS_REAL 字典 + 可选截图OCR)
  2. 无真实赔率 → 用 FIFA 排名反推估算赔率

特征构造(原始值, 非标准化, 直接可写库):
  - 赔率类: odds_imp_h/d/a, odds_balance, odds_spread, odds_overround, odds_confidence,
            odds_entropy, imp_d_norm, a1-a8, sigma_trap, lambda_crush, epsilon_senti
  - 排名类: rank_diff_factor, rank_factor, form_factor, form_momentum (FIFA真实排名)
  - 冷启动标记: is_cold_start, feat_coverage_ratio, home/away_match_count_norm
  - 联赛上下文: league_draw_rate=0.35, league_avg_goals=2.5 (世界杯)

用法:
    python scripts/backfill_wc_features.py            # 补算全部
    python scripts/backfill_wc_features.py --dry-run  # 只统计不写库
"""
from __future__ import annotations

import os
import sys
import json
import math
import sqlite3
import argparse
import importlib.util
from datetime import datetime
from typing import Dict, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)

# 加载 enriched_builder 复用 FIFA 排名 + 球队名映射
_spec = importlib.util.spec_from_file_location(
    "eb", os.path.join(PROJECT_ROOT, "features", "enriched_builder.py"))
eb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eb)

DB_PATH = os.path.join(PROJECT_ROOT, "data", "football_data.db")
WC_LEAGUE_ID = 2000
WC_LEAGUE_NAME = "世界杯"

# ── 真实赔率字典 (从 enriched_builder.ODDS + validation 数据汇总) ──
# key = "主队_客队" (中文), value = (home_odds, draw_odds, away_odds)
REAL_ODDS = {
    "加拿大_波斯尼亚": (6.0, 2.58, 3.0), "加拿大_波黑": (6.0, 2.58, 3.0),
    "美国_巴拉圭": (7.8, 5.9, 1.6),
    "卡塔尔_瑞士": (2.14, 1.93, 6.7),
    "巴西_摩洛哥": (1.7, 3.6, 5.3),
    "海地_苏格兰": (5.9, 4.6, 2.07),
    "澳大利亚_土耳其": (4.95, 3.75, 1.71),
    "德国_库拉索": (1.91, 2.03, 4.95),
    "瑞典_突尼斯": (1.92, 3.4, 4.1),
    "科特迪瓦_厄瓜多尔": (3.5, 2.88, 2.36),
    "伊朗_新西兰": (1.85, 3.35, 4.55),
    "比利时_埃及": (1.63, 2.25, 5.2),
    "法国_塞内加尔": (1.45, 4.4, 7.5),
    "阿根廷_阿尔及利亚": (1.94, 1.93, 7.9),
    "乌兹别克斯坦_哥伦比亚": (8.4, 1.99, 2.01), "乌兹别克_哥伦比亚": (8.4, 1.99, 2.01),
    "英格兰_克罗地亚": (1.73, 3.65, 4.95),
    "葡萄牙_民主刚果": (1.28, 5.6, 1.84), "葡萄牙_民主刚果": (1.28, 5.6, 1.84),
    "墨西哥_韩国": (2.76, 3.25, 3.95), "墨西哥_南非": (1.65, 3.6, 5.5),
    "捷克_南非": (1.82, 3.6, 4.35),
    "瑞士_波黑": (1.58, 4.05, 5.7), "瑞士_波斯尼亚": (1.58, 4.05, 5.7),
    "厄瓜多尔_库拉索": (1.7, 6.1, 2.41),
    "突尼斯_日本": (4.9, 3.45, 1.69),
    "荷兰_瑞典": (1.63, 2.11, 4.7),
    "美国_波黑": (1.12, 8.20, 24.0),  # 7.2截图OCR(进行中盘口)
}


def estimate_odds_from_rank(home: str, away: str) -> Tuple[float, float, float]:
    """无真实赔率时, 用 FIFA 排名反推估算赔率"""
    h_rank = eb.get_team_rank(home)
    a_rank = eb.get_team_rank(away)
    h_str = eb.estimate_team_strength(h_rank)
    a_str = eb.estimate_team_strength(a_rank)
    # 主场(中立场比赛, 世界杯) + 排名强度 → 隐含概率
    # 世界杯相对中立, 主场优势弱
    base_h = h_str * 0.42 + 0.20
    base_a = a_str * 0.42 + 0.20
    base_d = max(0.18, 1.0 - base_h - base_a)
    # 加点随机扰动避免全部雷同(基于排名差的确定性hash)
    seed = (hash(home) + hash(away)) % 100 / 100.0
    base_h *= (0.95 + seed * 0.10)
    base_a *= (0.95 + (1 - seed) * 0.10)
    tot = base_h + base_d + base_a
    ih, id_, ia = base_h / tot, base_d / tot, base_a / tot
    # 赔率 = 1/概率 × (1+抽水约5%)
    margin = 1.05
    return (1.0 / ih * margin, 1.0 / id_ * margin, 1.0 / ia * margin)


def compute_raw_features(home: str, away: str, ho: float, do_: float, ao: float
                          ) -> Dict[str, float]:
    """从赔率 + FIFA排名 计算原始特征值(非标准化, 可直接写库)。
    复用 enriched_builder 的公式但跳过最后的 (vec-mean)/std 归一化。"""
    imp = 1 / ho + 1 / do_ + 1 / ao
    ih = (1 / ho) / imp
    id_ = (1 / do_) / imp
    ia = (1 / ao) / imp

    f: Dict[str, float] = {}
    # 赔率类
    f["odds_imp_h"] = ih
    f["odds_imp_d"] = id_
    f["odds_imp_a"] = ia
    f["odds_balance"] = abs(ih - ia)
    f["odds_spread"] = ao - ho  # 正=客队赔率高=主队强
    f["odds_overround"] = imp - 1.0
    f["odds_confidence"] = math.sqrt((ih - 1/3)**2 + (id_ - 1/3)**2 + (ia - 1/3)**2) * 3
    f["odds_entropy"] = -sum(p * math.log(max(p, 1e-9)) for p in [ih, id_, ia])
    f["imp_d_norm"] = id_
    f["match_evenness"] = min(1.0, 1.0 - abs(ih - ia))
    f["home_advantage_neutral"] = 0.5  # 世界杯中立场地
    f["real_home_odds"] = ho
    f["real_draw_odds"] = do_
    f["real_away_odds"] = ao
    f["odds_close_h"] = ho
    f["odds_close_d"] = do_
    f["odds_close_a"] = ao
    f["odds_open_h"] = ho
    f["odds_open_d"] = do_
    f["odds_open_a"] = ao

    # a 因子 (与 enriched_builder 一致)
    f["a1"] = ih
    f["a2"] = ia  # 客胜隐含
    f["a3"] = id_
    f["a4"] = min(ih, 1.0) * 0.5 + min(ia, 1.0) * 0.5
    f["a5"] = min(id_, 1.0)
    f["a6"] = min(1.0 - abs(ih - ia), 1.0)
    f["a7"] = min(ih * 0.5 + ia * 0.5, 1.0)
    f["a8"] = min(abs(id_ - 1/3) * 3, 1.0)

    # sigma/lambda/epsilon (赔率衍生)
    f["sigma_trap"] = 0.0  # 无赔率漂移历史, 默认0
    f["lambda_crush"] = min(f["a1"] * f["a5"] * 2, 1.0)
    f["epsilon_senti"] = min(f["a1"] * f["a6"] * 2, 1.0)
    f["v_value"] = max(0.0, imp - 1.05)  # 价值缺口
    f["p_implied"] = max(ih, id_, ia)

    # drift (无开收盘对比, 默认0)
    for k in ["drift_magnitude", "drift_direction", "drift_d",
              "drift_h_val", "drift_a_val", "drift_sharp_signal",
              "miss_drift"]:
        f[k] = 0.0

    # FIFA 排名类 (真实数据)
    h_rank = eb.get_team_rank(home)
    a_rank = eb.get_team_rank(away)
    rank_diff = a_rank - h_rank  # 正=主队排名靠前(强)
    h_str = eb.estimate_team_strength(h_rank)
    a_str = eb.estimate_team_strength(a_rank)
    f["rank_diff_factor"] = max(-1.0, min(1.0, rank_diff / 50.0))
    f["rank_factor"] = h_str
    f["form_factor"] = max(0.0, min(1.0, 0.5 + (h_str - a_str) * 0.5))
    f["form_momentum"] = max(0.2, min(0.8, h_str))

    # h2h (默认, 无历史)
    f["h2h_factor"] = 0.0

    # 赔率衍生补充
    f["odds_draw_dev"] = id_ - 1/3
    f["odds_model_diverge"] = ih - 0.33
    f["odds_move_h"] = 0.0
    f["odds_move_d"] = 0.0
    f["odds_move_a"] = 0.0
    f["odds_move_magnitude"] = 0.0
    f["odds_fav_move"] = 0.0
    f["market_fav_strength"] = max(ih, id_, ia)
    f["market_disagreement"] = f["odds_entropy"]
    f["draw_odds_attract"] = max(0.0, min(1.0, 1 - (do_ - 3) / 2))
    f["odds_source"] = ""  # 占位, 后面填

    # 冷启动标记
    f["is_cold_start"] = 1.0
    f["feat_coverage_ratio"] = 0.45  # 有排名+赔率, 约45%特征真实
    f["home_match_count_norm"] = 0.0
    f["away_match_count_norm"] = 0.0

    # handicap/otsm (无数据, 默认)
    f["handicap_cover_prob"] = 0.5
    f["feat_coverage_ratio"] = 0.45
    return f


def write_features(conn, match_id: int, feats: Dict[str, float]):
    """写入 match_features 表 (只写表里实际存在的列)"""
    # 获取表的实际列
    actual_cols = {r[1] for r in conn.execute("PRAGMA table_info(match_features)").fetchall()}
    cols = [c for c in feats.keys() if c in actual_cols]
    placeholders = ",".join(["?"] * (len(cols) + 1))
    col_list = ",".join(["match_id"] + cols)
    vals = [match_id] + [feats[c] for c in cols]
    conn.execute(
        f"INSERT OR REPLACE INTO match_features ({col_list}) VALUES ({placeholders})",
        vals,
    )


def write_odds(conn, match_id: int, ho: float, do_: float, ao: float, source: str):
    """写入 odds 表 (若表存在且无重复)"""
    try:
        existing = conn.execute(
            "SELECT 1 FROM odds WHERE match_id=?", (match_id,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT OR IGNORE INTO odds (match_id, home_odds, draw_odds, away_odds, source) "
                "VALUES (?,?,?,?,?)",
                (match_id, ho, do_, ao, source),
            )
    except sqlite3.OperationalError:
        pass  # odds 表结构可能不同, 忽略


def main():
    ap = argparse.ArgumentParser(description="世界杯特征补算")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    print("=" * 60)
    print("  世界杯2026 特征补算")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT match_id, match_date, home_team_name, away_team_name, "
        "home_score, away_score, league_id FROM matches "
        "WHERE league_id IN (2000, 6) ORDER BY match_date"
    ).fetchall()

    print(f"  世界杯比赛: {len(rows)} 场")

    real_cnt = est_cnt = skip_cnt = 0
    for r in rows:
        home, away = r["home_team_name"] or "", r["away_team_name"] or ""
        if not home or not away:
            skip_cnt += 1
            continue  # 队名缺失, 跳过
        # 查真实赔率
        odds = None
        for sep in ["_"]:
            for k, v in REAL_ODDS.items():
                parts = k.split(sep)
                if len(parts) == 2 and (parts[0] in home and parts[1] in away
                                        or parts[0] == home and parts[1] == away):
                    odds = v
                    break
            if odds:
                break
        # 模糊匹配: 主队含key前半 且 客队含key后半
        if not odds:
            for k, v in REAL_ODDS.items():
                parts = k.split("_")
                if len(parts) == 2 and parts[0] in home and parts[1] in away:
                    odds = v
                    break

        source = "fifa_estimated"
        if odds:
            ho, do_, ao = odds
            source = "real"
            real_cnt += 1
        else:
            ho, do_, ao = estimate_odds_from_rank(home, away)
            est_cnt += 1

        feats = compute_raw_features(home, away, ho, do_, ao)
        feats["odds_source"] = source

        if not args.dry_run:
            write_features(conn, r["match_id"], feats)
            write_odds(conn, r["match_id"], ho, do_, ao, source)

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"  真实赔率: {real_cnt} 场")
    print(f"  FIFA估算: {est_cnt} 场")
    if args.dry_run:
        print("  (dry-run 模式, 未写库)")
    else:
        print(f"  ✅ 已写入 match_features 表")
    print("=" * 60)


if __name__ == "__main__":
    main()
