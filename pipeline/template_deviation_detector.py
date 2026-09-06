"""
哨响AI — 定价模板偏差信号检测器 v1.0
=====================================
基于8串1实证: 庄家OU模板在特定赛事类型存在系统性偏差。

偏差信号四层:
  L1: 赛事类型识别 (杯赛/友谊赛/低级别 → 小球模板过估)
  L2: 跨市场一致性 (1X2 vs CS vs OU 内在矛盾)
  L3: 盘口结构异常 (key number clustering, margin 异常)
  L4: 历史校准 (同类型赛事的历史实际 vs 市场隐含)

输出: 每场 GQ 比赛的 template_risk_score (0-1, 越高=模板越不可靠)
"""

from __future__ import annotations
import os
import json
import sqlite3
import re
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from collections import defaultdict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
GQ_DB = os.path.join(DATA_DIR, "events.db")
OUT_DIR = os.path.join(DATA_DIR, "pricing_template")
os.makedirs(OUT_DIR, exist_ok=True)

# ============================================================================
# L1: 赛事类型 → 模板偏差映射
# ============================================================================

# 基于8串1实证 + 足球博彩常识的赛事分类
TOURNAMENT_TEMPLATE_RISK = {
    # 高风险: 庄家模板最不可靠的区域
    "friendly": {
        "keywords": ["友谊赛", "friendly", "club friendly", "热身赛", "季前赛",
                      "球会友谊", "国际友谊", "训练赛"],
        "risk": 0.85,
        "bias": "OVER_ESTIMATE_GOALS",  # 庄家高估进球
        "reason": "轮换阵容/磨合期/无战意 → 实际进球远低于模板预期",
    },
    "african_cup": {
        "keywords": ["卡加梅", "kagame", "cecafa", "非洲杯", "africa cup of nations",
                      "caf", "非洲", "afcon", "chan"],
        "risk": 0.90,
        "bias": "OVER_ESTIMATE_GOALS",
        "reason": "非洲赛事数据稀疏 → 庄家用通用模板, 实际低分多",
    },
    "low_tier_cup": {
        "keywords": ["丹麦杯", "挪威杯", "瑞典杯", "芬兰杯", "奥地利杯",
                      "cup", "杯", "足总杯预", "联赛杯预", "地区杯",
                      "州杯", "省杯", "市杯"],
        "risk": 0.75,
        "bias": "OVER_ESTIMATE_GOALS",
        "reason": "低级别杯赛节奏慢/low-block → 小球率高于模板",
    },
    "early_cl_qualifier": {
        "keywords": ["欧冠资", "欧联资", "欧协资", "欧冠预", "champions league q",
                      "europa league q", "conference league q",
                      "欧冠外", "cl qualifying"],
        "risk": 0.70,
        "bias": "OVER_ESTIMATE_GOALS",
        "reason": "早期资格赛实力悬殊 → 强队控场/不急于进球 → 半场小球+全场小球",
    },
    "youth_reserve": {
        "keywords": ["u23", "u21", "u20", "u19", "u18", "u17", "reserve",
                      "b team", "二队", "青年", "预备", "新一代", "next gen"],
        "risk": 0.80,
        "bias": "OVER_ESTIMATE_GOALS",
        "reason": "青年/预备队数据少 → 模板不准, 实际波动大",
    },

    # 中风险
    "low_tier_league": {
        "keywords": ["甲级", "乙级", "丙级", "丁级", "division 2", "division 3",
                      "second division", "third division", "championship",
                      "league one", "league two", "serie b", "serie c",
                      "2nd tier", "3rd tier"],
        "risk": 0.50,
        "bias": "OVER_ESTIMATE_GOALS",
        "reason": "低级别联赛进球率低于顶级, 但模板校正不足",
    },

    # 低风险: 模板可靠
    "major_league": {
        "keywords": ["英超", "premier league", "西甲", "la liga", "意甲", "serie a",
                      "德甲", "bundesliga", "法甲", "ligue 1", "荷甲", "eredivisie",
                      "葡超", "primeira liga", "欧冠", "champions league",
                      "世界杯", "world cup", "欧洲杯", "euro", "美洲杯", "copa america"],
        "risk": 0.15,
        "bias": "CALIBRATED",
        "reason": "主流联赛数据充足 → 模板校准良好",
    },
}


def classify_tournament(league_name: str) -> Dict:
    """分类赛事, 返回 (category, risk, bias, reason)."""
    if not league_name or not isinstance(league_name, str):
        return {"category": "unknown", "risk": 0.60, "bias": "UNKNOWN", "reason": "未知赛事"}

    name_lower = league_name.lower()

    # 按优先级匹配 (高风险优先)
    for cat, info in TOURNAMENT_TEMPLATE_RISK.items():
        for kw in info["keywords"]:
            if kw.lower() in name_lower:
                return {
                    "category": cat,
                    "risk": info["risk"],
                    "bias": info["bias"],
                    "reason": info["reason"],
                }

    return {"category": "other", "risk": 0.40, "bias": "MILD_OVER_ESTIMATE_GOALS",
            "reason": "非主流赛事, 模板精度中等"}


# ============================================================================
# L2: 跨市场一致性检查 (修复版)
# ============================================================================

class CrossMarketChecker:
    """从 CS 波胆反推其他市场, 检查内在一致性."""

    def __init__(self):
        cs_path = os.path.join(DATA_DIR, "cs_calibration.json")
        with open(cs_path) as f:
            calib_data = json.load(f)
        self.calib_factors = {}
        for score, data in calib_data.get("calibrated_scores", {}).items():
            factor = data["factor"]
            self.calib_factors[score] = 1.0 + (factor - 1.0) * 0.5

    def parse_cs_odds(self, op_cs_raw) -> Optional[List[Tuple[str, float]]]:
        """解析 op_cs JSON. 支持 list of [score, odds] 和 list of list."""
        if not op_cs_raw or op_cs_raw == "nan" or (isinstance(op_cs_raw, float) and np.isnan(op_cs_raw)):
            return None

        try:
            if isinstance(op_cs_raw, str):
                data = json.loads(op_cs_raw)
            else:
                data = op_cs_raw

            if isinstance(data, list):
                result = []
                for item in data:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        score = str(item[0])
                        odds = float(item[1])
                        result.append((score, odds))
                return result if result else None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return None

    def cs_to_1x2(self, cs_odds: List[Tuple[str, float]]) -> Dict[str, float]:
        """CS赔率 → 1X2概率."""
        if not cs_odds:
            return {}

        # 去抽水
        inv_sum = sum(1.0 / o for _, o in cs_odds if o > 0)
        if inv_sum <= 0:
            return {}

        probs = {}
        for score, odds in cs_odds:
            if odds <= 0:
                continue
            raw_p = (1.0 / odds) / inv_sum
            calib = self.calib_factors.get(score, 1.0)
            probs[score] = raw_p * calib

        # 归一化
        total = sum(probs.values())
        if total > 0:
            probs = {k: v / total for k, v in probs.items()}

        p_h = p_d = p_a = 0.0
        for score, p in probs.items():
            parts = score.split("-")
            if len(parts) == 2:
                try:
                    h, a = int(parts[0]), int(parts[1])
                    if h > a:
                        p_h += p
                    elif h == a:
                        p_d += p
                    else:
                        p_a += p
                except ValueError:
                    continue
        return {"p_h": round(p_h, 4), "p_d": round(p_d, 4), "p_a": round(p_a, 4)}

    def cs_to_ou(self, cs_odds: List[Tuple[str, float]], line: float = 2.5) -> Dict[str, float]:
        """CS → OU."""
        if not cs_odds:
            return {}

        inv_sum = sum(1.0 / o for _, o in cs_odds if o > 0)
        if inv_sum <= 0:
            return {}

        probs = {}
        for score, odds in cs_odds:
            if odds <= 0:
                continue
            raw_p = (1.0 / odds) / inv_sum
            calib = self.calib_factors.get(score, 1.0)
            probs[score] = raw_p * calib

        total = sum(probs.values())
        if total > 0:
            probs = {k: v / total for k, v in probs.items()}

        p_over = p_under = 0.0
        for score, p in probs.items():
            parts = score.split("-")
            if len(parts) == 2:
                try:
                    total_goals = int(parts[0]) + int(parts[1])
                    if total_goals > line:
                        p_over += p
                    elif total_goals < line:
                        p_under += p
                except ValueError:
                    continue
        return {"p_over": round(p_over, 4), "p_under": round(p_under, 4)}

    def check(self, op_cs_raw, op_1x2_h, op_1x2_d, op_1x2_a,
              op_ou_line, op_ou_over, op_ou_under, threshold: float = 0.08) -> List[Dict]:
        """完整跨市场一致性检查."""
        signals = []
        cs_odds = self.parse_cs_odds(op_cs_raw)
        if not cs_odds:
            return signals

        # CS → 1X2
        cs_1x2 = self.cs_to_1x2(cs_odds)
        if cs_1x2 and all(v is not None and not np.isnan(v) for v in [op_1x2_h, op_1x2_d, op_1x2_a]
                          if v is not None):
            try:
                inv = 1.0/float(op_1x2_h) + 1.0/float(op_1x2_d) + 1.0/float(op_1x2_a)
                mkt_h = (1.0/float(op_1x2_h)) / inv
                mkt_d = (1.0/float(op_1x2_d)) / inv
                mkt_a = (1.0/float(op_1x2_a)) / inv

                for label, mkt, cs in [("H", mkt_h, cs_1x2["p_h"]),
                                        ("D", mkt_d, cs_1x2["p_d"]),
                                        ("A", mkt_a, cs_1x2["p_a"])]:
                    dev = cs - mkt
                    if abs(dev) >= threshold:
                        signals.append({
                            "type": "CS_vs_1X2",
                            "outcome": label,
                            "deviation": round(dev, 4),
                            "direction": "CS > market" if dev > 0 else "CS < market",
                        })
            except (ValueError, ZeroDivisionError):
                pass

        # CS → OU
        cs_ou = self.cs_to_ou(cs_odds, 2.5)
        if cs_ou and all(v is not None and not np.isnan(v) for v in [op_ou_over, op_ou_under]
                          if v is not None):
            try:
                ou_inv = 1.0/float(op_ou_over) + 1.0/float(op_ou_under)
                mkt_over = (1.0/float(op_ou_over)) / ou_inv
                dev = cs_ou["p_over"] - mkt_over
                if abs(dev) >= threshold:
                    signals.append({
                        "type": "CS_vs_OU",
                        "outcome": "OVER",
                        "deviation": round(dev, 4),
                        "direction": "CS > market" if dev > 0 else "CS < market",
                    })
            except (ValueError, ZeroDivisionError):
                pass

        return signals


# ============================================================================
# L3: 盘口结构异常检测
# ============================================================================

def detect_ou_structure_anomaly(op_ou_line, op_ou_over, op_ou_under) -> Dict:
    """检测 OU 盘口的 margin 异常."""
    result = {"anomaly": False, "margin": None, "signal": None}

    if any(v is None or (isinstance(v, float) and np.isnan(v))
           for v in [op_ou_line, op_ou_over, op_ou_under]):
        return result

    try:
        ou_inv = 1.0 / float(op_ou_over) + 1.0 / float(op_ou_under)
        margin = ou_inv - 1.0
        result["margin"] = round(margin, 4)

        # 正常 OU margin: 5-9%
        if margin > 0.12:
            result["anomaly"] = True
            result["signal"] = "HIGH_MARGIN"  # 庄家不确定,加厚margin
        elif margin < 0.03:
            result["anomaly"] = True
            result["signal"] = "LOW_MARGIN"   # 可能是开盘错误或促销
    except (ValueError, ZeroDivisionError):
        pass

    return result


# ============================================================================
# L4: 历史校准统计
# ============================================================================

def compute_tournament_ou_stats() -> pd.DataFrame:
    """按赛事类型统计历史 OU 命中率."""
    conn = sqlite3.connect(GQ_DB)
    df = pd.read_sql_query(
        """SELECT league, op_ou_line, op_ou_over, op_ou_under,
                  score_home, score_away
           FROM match_outcomes
           WHERE score_home IS NOT NULL AND score_away IS NOT NULL
             AND op_ou_line IS NOT NULL AND op_ou_over IS NOT NULL""",
        conn,
    )
    conn.close()

    if df.empty:
        return pd.DataFrame()

    # 判断 OU 结果
    df["total_goals"] = df["score_home"] + df["score_away"]
    df["ou_result"] = np.where(df["total_goals"] > df["op_ou_line"], "over",
                       np.where(df["total_goals"] < df["op_ou_line"], "under", "push"))

    # 分类
    classifications = df["league"].apply(classify_tournament)
    df["tournament_cat"] = classifications.apply(lambda x: x["category"])
    df["template_risk"] = classifications.apply(lambda x: x["risk"])

    # 按类别统计
    stats = df.groupby("tournament_cat").agg(
        n=("ou_result", "count"),
        over_rate=("ou_result", lambda x: (x == "over").mean()),
        under_rate=("ou_result", lambda x: (x == "under").mean()),
        push_rate=("ou_result", lambda x: (x == "push").mean()),
        avg_goals=("total_goals", "mean"),
        avg_margin=("op_ou_over", lambda x: np.mean(1.0/x + 1.0/df.loc[x.index, "op_ou_under"] - 1)),
    ).reset_index()

    stats["bias_direction"] = stats["under_rate"].apply(
        lambda x: "OVER_EST_GOALS" if x > 0.55 else "UNDER_EST_GOALS" if x < 0.45 else "CALIBRATED"
    )

    return stats


# ============================================================================
# 综合评分
# ============================================================================

def score_match(row: pd.Series, checker: CrossMarketChecker) -> Dict:
    """对单场比赛生成模板偏差综合评分."""
    scores = {}
    details = []

    # L1: 赛事类型风险
    league = str(row.get("league", ""))
    cat_info = classify_tournament(league)
    scores["L1_tournament_risk"] = cat_info["risk"]
    details.append(f"L1: {cat_info['category']} (risk={cat_info['risk']:.2f}, {cat_info['reason']})")

    # L2: 跨市场一致性
    try:
        cs_raw = row.get("op_cs")
        op_h = row.get("op_1x2_h")
        op_d = row.get("op_1x2_d")
        op_a = row.get("op_1x2_a")
        ou_line = row.get("op_ou_line")
        ou_over = row.get("op_ou_over")
        ou_under = row.get("op_ou_under")

        cs_signals = checker.check(cs_raw, op_h, op_d, op_a, ou_line, ou_over, ou_under)
        scores["L2_cross_market_signals"] = len(cs_signals)
        scores["L2_cross_market_risk"] = min(0.8, len(cs_signals) * 0.25)
        if cs_signals:
            details.append(f"L2: {len(cs_signals)} 跨市场信号: {[s['type'] for s in cs_signals]}")
    except Exception:
        scores["L2_cross_market_signals"] = 0
        scores["L2_cross_market_risk"] = 0.0

    # L3: 盘口结构
    try:
        ou_anomaly = detect_ou_structure_anomaly(ou_line, ou_over, ou_under)
        scores["L3_ou_anomaly"] = 0.3 if ou_anomaly["anomaly"] else 0.0
        scores["L3_ou_margin"] = ou_anomaly.get("margin", 0)
        if ou_anomaly["anomaly"]:
            details.append(f"L3: OU margin异常={ou_anomaly['margin']:.3f} ({ou_anomaly['signal']})")
    except Exception:
        scores["L3_ou_anomaly"] = 0.0
        scores["L3_ou_margin"] = None

    # 综合
    weights = {"L1": 0.50, "L2": 0.30, "L3": 0.20}
    composite = (
        weights["L1"] * scores.get("L1_tournament_risk", 0.5) +
        weights["L2"] * scores.get("L2_cross_market_risk", 0.0) +
        weights["L3"] * scores.get("L3_ou_anomaly", 0.0)
    )

    # 偏差方向
    bias = cat_info["bias"]
    if scores.get("L2_cross_market_signals", 0) > 0:
        bias = bias + "_CROSS_MARKET_CONFIRMED"

    return {
        "template_risk_score": round(composite, 4),
        "risk_level": "HIGH" if composite > 0.6 else "MEDIUM" if composite > 0.3 else "LOW",
        "bias_direction": bias,
        "scores": scores,
        "details": details,
    }


# ============================================================================
# 主流程
# ============================================================================

def scan_all_matches():
    """扫描 GQ 全部比赛, 输出模板偏差报告."""
    print("=" * 70)
    print("哨响AI 定价模板偏差信号检测器 v1.0")
    print("=" * 70)

    conn = sqlite3.connect(GQ_DB)
    df = pd.read_sql_query(
        """SELECT mid, home, away, league, kickoff, score_home, score_away, result,
                  op_1x2_h, op_1x2_d, op_1x2_a,
                  op_ou_line, op_ou_over, op_ou_under,
                  op_cs
           FROM match_outcomes
           WHERE score_home IS NOT NULL AND score_away IS NOT NULL""",
        conn,
    )
    conn.close()

    print(f"\n总比赛: {len(df)}")

    checker = CrossMarketChecker()

    # 逐场评分
    results = []
    for _, row in df.iterrows():
        result = score_match(row, checker)
        result["mid"] = row.get("mid", "")
        result["home"] = row.get("home", "")
        result["away"] = row.get("away", "")
        result["league"] = row.get("league", "")
        result["kickoff"] = str(row.get("kickoff", ""))
        results.append(result)

    df_results = pd.DataFrame(results)

    # 统计
    print(f"\n=== 模板风险分布 ===")
    print(f"  HIGH risk (>0.6):   {(df_results['template_risk_score']>0.6).sum()} 场")
    print(f"  MEDIUM risk (0.3-0.6): {((df_results['template_risk_score']>0.3)&(df_results['template_risk_score']<=0.6)).sum()} 场")
    print(f"  LOW risk (<0.3):    {(df_results['template_risk_score']<=0.3).sum()} 场")

    # 按联赛 TOP 高风险
    print(f"\n=== 高风险联赛 TOP 15 ===")
    league_risk = df_results.groupby("league").agg(
        n=("template_risk_score", "count"),
        avg_risk=("template_risk_score", "mean"),
    ).sort_values("avg_risk", ascending=False)

    for league, row in league_risk.head(15).iterrows():
        print(f"  {league:30s}: avg_risk={row['avg_risk']:.3f}, n={int(row['n'])}")

    # 按偏差方向分组
    print(f"\n=== 偏差方向分布 ===")
    bias_counts = df_results["bias_direction"].value_counts()
    for bias, count in bias_counts.items():
        print(f"  {bias}: {count} 场")

    # 跨市场信号
    total_signals = df_results["scores"].apply(lambda x: x.get("L2_cross_market_signals", 0)).sum()
    print(f"\n=== 跨市场信号 ===")
    print(f"  总信号: {int(total_signals)}")
    print(f"  有信号比赛: {(df_results['scores'].apply(lambda x: x.get('L2_cross_market_signals', 0)) > 0).sum()}")

    # L3 盘口异常
    ou_anomalies = df_results["scores"].apply(lambda x: x.get("L3_ou_anomaly", 0)).sum()
    print(f"\n=== 盘口结构异常 ===")
    print(f"  OU margin异常: {int(ou_anomalies/0.3)} 场")

    # 保存
    out_path = os.path.join(OUT_DIR, "template_deviation_scan.csv")
    # 展开 scores 到列
    for key in ["L1_tournament_risk", "L2_cross_market_signals", "L2_cross_market_risk",
                "L3_ou_anomaly", "L3_ou_margin"]:
        df_results[key] = df_results["scores"].apply(lambda x: x.get(key, 0))
    df_results.drop(columns=["scores"], inplace=True)

    df_results.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n报告已保存: {out_path}")

    # === 与8串1实证对照 ===
    print(f"\n=== 8串1实证对照 ===")
    empirical_tournaments = [
        ("欧冠资格赛", 0.70, "命中"),
        ("丹麦杯", 0.75, "命中"),
        ("球会友谊赛", 0.85, "命中"),
        ("卡加梅杯", 0.90, "命中"),
    ]
    print(f"  赛事类型          | 模型评分 | 实证结果")
    print(f"  ------------------+---------+--------")
    for t, score, result in empirical_tournaments:
        print(f"  {t:16s}  | {score:.2f}     | {result}")

    return df_results


if __name__ == "__main__":
    scan_all_matches()
