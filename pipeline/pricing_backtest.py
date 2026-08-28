"""
哨响AI — 定价模板回测验证器 v1.0
================================
基于定价模板反演引擎的产出，验证结构性 edge 的盈利能力。

验证维度:
  A. 模板还原能力: 赔率预测 RMSE, margin 函数拟合优度
  B. 残差盈利能力: 公平概率与市场偏差的 ROI 分层回测
  C. 跨市场套利: 内部不一致信号的实际可盈利性
  D. 初盘-收盘方向: CLV + Beat Closing %
  E. 波胆结构: 校准因子 vs 实际命中率

输入: pricing_template_report.json + triplet_table.csv
"""

from __future__ import annotations
import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Tuple
from collections import defaultdict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IN_DIR = os.path.join(ROOT, "data", "pricing_template")
OUT_DIR = IN_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pricing_backtest")


def load_triplet_table() -> pd.DataFrame:
    """加载三联表."""
    path = os.path.join(IN_DIR, "triplet_table.csv")
    df = pd.read_csv(path)
    log.info(f"Loaded {len(df)} triplets")
    return df


def load_report() -> Dict:
    """加载定价模板报告."""
    path = os.path.join(IN_DIR, "pricing_template_report.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ============================================================================
# A: 模板还原能力验证
# ============================================================================

def verify_template_recovery(df: pd.DataFrame, report: Dict):
    """验证: 公平概率能否预测市场赔率?"""
    log.info("\n" + "=" * 50)
    log.info("A. 模板还原能力验证")
    log.info("=" * 50)

    results = {}

    # A1: 赔率预测 RMSE
    # 用 DC fair prob → 预测市场 odds (via margin template)
    margin_a = report["margin_template"]["a"]
    margin_b = report["margin_template"]["b"]

    valid = df[(df["op_h"].notna()) & (df["dc_p_h"] > 0)].copy()
    if len(valid) > 0:
        eps = 1e-6
        # 预测市场隐含概率: market_p = exp(a * log(fair_p) + b)
        pred_h = np.exp(margin_a * np.log(np.clip(valid["dc_p_h"].values, eps, None)) + margin_b)
        pred_d = np.exp(margin_a * np.log(np.clip(valid["dc_p_d"].values, eps, None)) + margin_b)
        pred_a = np.exp(margin_a * np.log(np.clip(valid["dc_p_a"].values, eps, None)) + margin_b)

        # Actual market implied
        oh, od, oa = valid["op_h"].values, valid["op_d"].values, valid["op_a"].values
        inv_h = 1.0/oh; inv_d = 1.0/od; inv_a = 1.0/oa
        inv_sum = inv_h + inv_d + inv_a
        mkt_h = inv_h / inv_sum
        mkt_d = inv_d / inv_sum
        mkt_a = inv_a / inv_sum

        # RMSE
        rmse_h = np.sqrt(np.mean((pred_h - mkt_h)**2))
        rmse_d = np.sqrt(np.mean((pred_d - mkt_d)**2))
        rmse_all = np.sqrt(np.mean(np.concatenate([
            (pred_h - mkt_h)**2, (pred_d - mkt_d)**2, (pred_a - mkt_a)**2
        ])))

        results["odds_prediction_rmse"] = {
            "rmse_H": round(float(rmse_h), 5),
            "rmse_D": round(float(rmse_d), 5),
            "rmse_A": round(float(rmse_a), 5),
            "rmse_overall": round(float(rmse_all), 5),
            "n": len(valid),
        }

        # R²: how much variance in market prob does fair prob explain?
        for outcome, fair_col, mkt_arr in [
            ("H", "dc_p_h", mkt_h), ("D", "dc_p_d", mkt_d), ("A", "dc_p_a", mkt_a)
        ]:
            fair_vals = np.clip(valid[fair_col].values, eps, None)
            r = np.corrcoef(np.log(fair_vals), np.log(np.clip(mkt_arr, eps, None)))[0, 1]
            results[f"log_correlation_{outcome}"] = round(float(r), 4)

        log.info(f"  RMSE: H={results['odds_prediction_rmse']['rmse_H']}, "
                 f"D={results['odds_prediction_rmse']['rmse_D']}, "
                 f"A={results['odds_prediction_rmse']['rmse_A']}")

    # A2: 残差随机性检验
    margin_r2 = report["margin_template"]["r2"]
    results["margin_template_r2"] = margin_r2
    log.info(f"  Margin template R²: {margin_r2:.4f}")

    # A3: 按联赛分层 (top 10 leagues)
    league_counts = df["league"].value_counts().head(10)
    league_rmse = {}
    for league in league_counts.index:
        sub = valid[valid["league"] == league]
        if len(sub) < 20:
            continue
        sub_oh = sub["op_h"].values; sub_od = sub["op_d"].values; sub_oa = sub["op_a"].values
        inv_h = 1.0/sub_oh; inv_d = 1.0/sub_od; inv_a = 1.0/sub_oa
        inv_sum = inv_h + inv_d + inv_a
        mkt = np.concatenate([inv_h/inv_sum, inv_d/inv_sum, inv_a/inv_sum])
        fair = np.concatenate([
            np.clip(sub["dc_p_h"].values, eps, None),
            np.clip(sub["dc_p_d"].values, eps, None),
            np.clip(sub["dc_p_a"].values, eps, None),
        ])
        pred = np.exp(margin_a * np.log(fair) + margin_b)
        league_rmse[league] = {
            "rmse": round(float(np.sqrt(np.mean((pred - mkt)**2))), 5),
            "n": int(len(sub)),
        }
    results["league_rmse_top10"] = league_rmse
    log.info(f"  League RMSE range: {min(v['rmse'] for v in league_rmse.values()):.5f} - "
             f"{max(v['rmse'] for v in league_rmse.values()):.5f}")

    return results


# ============================================================================
# B: 残差盈利能力验证
# ============================================================================

def verify_structural_edge(df: pd.DataFrame):
    """验证: 公平概率与市场偏差能否产生正 EV?"""
    log.info("\n" + "=" * 50)
    log.info("B. 残差盈利能力验证")
    log.info("=" * 50)

    valid = df[(df["op_h"].notna()) & (df["dc_p_h"] > 0)].copy()
    results = {}

    for outcome, label, dev_col, dc_col in [
        ("H", "home", "op_dev_h", "dc_p_h"),
        ("D", "draw", "op_dev_d", "dc_p_d"),
        ("A", "away", "op_dev_a", "dc_p_a"),
    ]:
        # 按偏差分层
        edges = [(-999, -0.10), (-0.10, -0.05), (-0.05, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 999)]
        labels = ["<-10%", "-10%~-5%", "-5%~2%", "2%~5%", "5%~10%", ">10%"]

        layer_stats = []
        for (lo, hi), lb in zip(edges, labels):
            mask = (valid[dev_col] > lo) & (valid[dev_col] <= hi)
            n = mask.sum()
            if n < 5:
                layer_stats.append({"layer": lb, "n": n, "hit_rate": None, "dc_mean": None, "ev_signal": 0})
                continue

            sub = valid[mask]
            hit_rate = (sub["actual_result"] == label).mean()
            dc_mean = sub[dc_col].mean()

            # EV 信号: dc_p > market_implied + threshold
            ev_mask = mask & (valid[dev_col] > 0.03)
            ev_n = ev_mask.sum()
            ev_hit = (valid[ev_mask]["actual_result"] == label).mean() if ev_n > 0 else 0
            ev_dc = valid[ev_mask][dc_col].mean() if ev_n > 0 else 0

            layer_stats.append({
                "layer": lb,
                "n": int(n),
                "hit_rate": round(float(hit_rate), 4),
                "dc_mean": round(float(dc_mean), 4),
                "dc_vs_actual": round(float(dc_mean - hit_rate), 4),
                "ev_signals": int(ev_n),
                "ev_hit_rate": round(float(ev_hit), 4) if ev_n > 0 else None,
            })

        results[f"dev_layers_{outcome}"] = layer_stats

        # 正偏差层 (市场低估) 的整体统计
        pos_mask = valid[dev_col] > 0.02
        neg_mask = valid[dev_col] < -0.02
        results[f"positive_dev_{outcome}"] = {
            "n": int(pos_mask.sum()),
            "hit_rate": round(float((valid[pos_mask]["actual_result"] == label).mean()), 4),
            "dc_mean": round(float(valid[pos_mask][dc_col].mean()), 4),
        }
        results[f"negative_dev_{outcome}"] = {
            "n": int(neg_mask.sum()),
            "hit_rate": round(float((valid[neg_mask]["actual_result"] == label).mean()), 4),
            "dc_mean": round(float(valid[neg_mask][dc_col].mean()), 4),
        }

        log.info(f"  {outcome}: pos_dev(n={results[f'positive_dev_{outcome}']['n']}) "
                 f"hit={results[f'positive_dev_{outcome}']['hit_rate']}, "
                 f"neg_dev(n={results[f'negative_dev_{outcome}']['n']}) "
                 f"hit={results[f'negative_dev_{outcome}']['hit_rate']}")

    return results


# ============================================================================
# C: 跨市场套利回测
# ============================================================================

def verify_cross_market_arbitrage(df: pd.DataFrame, report: Dict):
    """验证: 跨市场不一致信号是否可盈利?"""
    log.info("\n" + "=" * 50)
    log.info("C. 跨市场套利验证")
    log.info("=" * 50)

    total_signals = report["analysis"]["total_cross_signals"]
    matches_with = report["analysis"]["matches_with_signals"]
    log.info(f"  Total signals: {total_signals}, Matches with signals: {matches_with}")

    results = {
        "total_signals": total_signals,
        "matches_with_signals": matches_with,
    }

    # 按信号数量分层看命中率
    if "cross_market_signals" in df.columns:
        sig_df = df[df["cross_market_signals"] > 0]
        if len(sig_df) > 0:
            for outcome, label in [("H", "home"), ("D", "draw"), ("A", "away")]:
                hit = (sig_df["actual_result"] == label).mean()
                results[f"signal_hit_{outcome}"] = round(float(hit), 4)
            log.info(f"  Signal matches hit: H={results.get('signal_hit_H',0):.3f}, "
                     f"D={results.get('signal_hit_D',0):.3f}, A={results.get('signal_hit_A',0):.3f}")

    return results


# ============================================================================
# D: 初盘-收盘方向验证
# ============================================================================

def verify_clv(df: pd.DataFrame, report: Dict):
    """验证: Beat Closing % 和开盘-收盘关系."""
    log.info("\n" + "=" * 50)
    log.info("D. 初盘-收盘方向验证 (CLV)")
    log.info("=" * 50)

    results = {}
    for outcome in ["H", "D", "A"]:
        col = f"beat_closing_{outcome.lower()}"
        pct = report["analysis"].get(f"beat_closing_{outcome}_pct", 0)
        results[f"beat_closing_{outcome}_pct"] = pct
        log.info(f"  Beat Closing {outcome}: {pct:.1%}")

    # 分析开盘→收盘赔率漂移方向 (仅当有 cl 数据时)
    cl_df = df[df["cl_h"].notna()]
    if len(cl_df) > 100:
        # 计算漂移
        _, mkt_h, mkt_d, mkt_a, _ = None, None, None, None, None  # placeholder

        # 简单的: 开盘偏差 vs 收盘偏差的变化
        valid_cl = cl_df[(cl_df["op_dev_h"].notna()) & (cl_df["cl_dev_h"].notna())]
        if len(valid_cl) > 100:
            # 高偏差组: 开盘市场低估 → 收盘是否修正?
            high_dev = valid_cl[valid_cl["op_dev_h"] > 0.05]
            n_dev = len(high_dev)
            if n_dev > 0:
                # 收盘偏差是否缩小?
                corrected = (high_dev["cl_dev_h"].abs() < high_dev["op_dev_h"].abs()).mean()
                results["dev_correction_rate"] = round(float(corrected), 4)
                log.info(f"  High-dev {n_dev} matches: {corrected:.1%} corrected toward fair at close")

    return results


# ============================================================================
# E: 波胆结构验证
# ============================================================================

def verify_cs_structure(report: Dict):
    """验证: 波胆校准因子的实际效果."""
    log.info("\n" + "=" * 50)
    log.info("E. 波胆结构验证")
    log.info("=" * 50)

    # 从 cs_calibration.json 读取
    cs_path = os.path.join(ROOT, "data", "cs_calibration.json")
    with open(cs_path) as f:
        cs_data = json.load(f)

    scores = cs_data["calibrated_scores"]

    # 按因子分组统计
    results = {
        "total_scores": len(scores),
        "scores": {},
    }

    for score, data in sorted(scores.items(), key=lambda x: -x[1]["factor"]):
        damped = 1.0 + (data["factor"] - 1.0) * 0.5
        results["scores"][score] = {
            "actual_pct": data["actual_pct"],
            "avg_implied": data["avg_implied"],
            "raw_factor": data["factor"],
            "damped_factor": round(damped, 4),
            "n": data["n"],
        }
        log.info(f"  {score}: actual={data['actual_pct']:.3f}, implied={data['avg_implied']:.3f}, "
                 f"factor={data['factor']:.3f}, damped={damped:.3f}")

    return results


# ============================================================================
# 综合报告
# ============================================================================

def generate_final_report(
    report: Dict,
    recovery: Dict,
    edge: Dict,
    cross: Dict,
    clv: Dict,
    cs_check: Dict,
):
    """生成综合验证报告."""
    final = {
        "generated_at": datetime.now().isoformat(),
        "engine_version": "1.0",
        "summary": report.get("build_stats", {}),
        "dc_model": {
            "rho": report["dc_model"]["rho"],
            "home_adv": report["dc_model"]["home_adv"],
            "intercept": report["dc_model"]["intercept"],
            "n_teams": report["dc_model"]["n_teams"],
            "n_train": report["dc_model"]["n_train"],
        },
        "margin_template": report["margin_template"],
        "A_template_recovery": recovery,
        "B_structural_edge": edge,
        "C_cross_market": cross,
        "D_clv_analysis": clv,
        "E_cs_structure": cs_check,
    }

    out_path = os.path.join(OUT_DIR, "pricing_backtest_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"\n综合回测报告: {out_path}")
    return final


def run_all_verifications():
    """运行全部验证."""
    df = load_triplet_table()
    report = load_report()

    results = {}

    log.info("开始定价模板回测验证...\n")

    recovery = verify_template_recovery(df, report)
    results["A_template_recovery"] = recovery

    edge = verify_structural_edge(df)
    results["B_structural_edge"] = edge

    cross = verify_cross_market_arbitrage(df, report)
    results["C_cross_market"] = cross

    clv = verify_clv(df, report)
    results["D_clv_analysis"] = clv

    cs_check = verify_cs_structure(report)
    results["E_cs_structure"] = cs_check

    final = generate_final_report(report, recovery, edge, cross, clv, cs_check)

    # 打印关键结论
    log.info("\n" + "=" * 60)
    log.info("关键结论")
    log.info("=" * 60)

    log.info(f"1. DC模型: ρ={report['dc_model']['rho']:.4f}, "
             f"主场优势={report['dc_model']['home_adv']:.3f} 球")

    rmse = recovery.get("odds_prediction_rmse", {})
    if rmse:
        log.info(f"2. 赔率预测 RMSE: H={rmse['rmse_H']:.5f}, "
                 f"D={rmse['rmse_D']:.5f}, A={rmse['rmse_A']:.5f}")

    log.info(f"3. Margin 模板 R²: {report['margin_template']['r2']:.4f}")

    for outcome in ["H", "D", "A"]:
        pos = edge.get(f"positive_dev_{outcome}", {})
        neg = edge.get(f"negative_dev_{outcome}", {})
        log.info(f"4{outcome}. 正偏差(n={pos.get('n',0)}) HR={pos.get('hit_rate',0):.3f}, "
                 f"负偏差(n={neg.get('n',0)}) HR={neg.get('hit_rate',0):.3f}")

    log.info(f"5. 跨市场信号: {cross.get('total_signals', 0)} 个, "
             f"覆盖 {cross.get('matches_with_signals', 0)} 场")

    for outcome in ["H", "D", "A"]:
        pct = clv.get(f"beat_closing_{outcome}_pct", 0)
        log.info(f"6. Beat Closing {outcome}: {pct:.1%}")

    return final


if __name__ == "__main__":
    run_all_verifications()
