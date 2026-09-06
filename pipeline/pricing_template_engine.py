# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ⚠ DEPRECATED — 2026-08-05 模型收敛 (M1-M7)                          ║
# ║  半死链: 仅被已弃用的 gq_dc_model 裸 import                        ║
# ║  替代: M7 pipeline/template_deviation_detector.py                    ║
# ║  单一真相源: pipeline/model_catalog.py                                ║
# ║  本文件保留仅为历史可追溯, 禁止在新代码中引用.                        ║
# ╚══════════════════════════════════════════════════════════════════════╝
"""
哨响AI — 定价模板反演引擎 v1.0
==============================
从"预测赛果"到"破解庄家定价模板"的战略转向核心引擎。

四层架构:
  Layer 1: Dixon-Coles 公平概率基线 (无市场污染)
  Layer 2: 庄家 margin 模板识别 (偏离模板 = 信号)
  Layer 3: 跨市场一致性套利扫描 (1X2/OU/AH/CS 内在矛盾)
  Layer 4: 开盘-收盘-赛果三联表 + CLV 验证

依赖: dc_score_model.py, score_distribution.py, events.db, football_data.db
"""

from __future__ import annotations
import os
import sys
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.stats import poisson
from scipy.optimize import minimize

# Paths
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
GQ_DB = os.path.join(DATA_DIR, "events.db")
FB_DB = os.path.join(DATA_DIR, "football_data.db")
CS_CALIB = os.path.join(DATA_DIR, "cs_calibration.json")
OUT_DIR = os.path.join(ROOT, "data", "pricing_template")
os.makedirs(OUT_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pricing_template")

# ============================================================================
# Layer 0: 数据加载
# ============================================================================

def load_gq_matches() -> pd.DataFrame:
    """加载 GQ match_outcomes (开盘赔率 + 赛果 + 半场)."""
    conn = sqlite3.connect(GQ_DB)
    df = pd.read_sql_query(
        """SELECT mid, home, away, league, kickoff,
                  score_home, score_away, result,
                  op_1x2_h, op_1x2_d, op_1x2_a,
                  op_ah_line, op_ah_home, op_ah_away,
                  op_ou_line, op_ou_over, op_ou_under,
                  op_cs,
                  captured_at, archived_at, is_valid
           FROM match_outcomes
           WHERE is_valid = 1 AND result IN ('home','draw','away')
             AND score_home IS NOT NULL AND score_away IS NOT NULL""",
        conn,
    )
    conn.close()
    df["kickoff"] = pd.to_datetime(df["kickoff"], errors="coerce")
    return df


def load_closing_odds(match_keys: List[str]) -> pd.DataFrame:
    """从 odds_snapshots 提取每场比赛的"最晚快照"作为收盘赔率近似."""
    conn = sqlite3.connect(GQ_DB)
    placeholders = ",".join(["?"] * len(match_keys))
    df = pd.read_sql_query(
        f"""SELECT match_key, captured_at, market, selection, odds, line, score_at, minute_at
            FROM odds_snapshots
            WHERE match_key IN ({placeholders})
              AND market IN ('1X2','OU_2.50','AH_0.00')
            ORDER BY captured_at DESC""",
        conn,
        params=match_keys,
    )
    conn.close()
    return df


def load_interwetten() -> pd.DataFrame:
    """加载 interwetten_odds 用于 DC 训练."""
    conn = sqlite3.connect(FB_DB)
    df = pd.read_sql_query(
        """SELECT home_team_norm AS home_team, away_team_norm AS away_team,
                  home_score, away_score, match_date AS date,
                  close_home_odds AS ch, close_draw_odds AS cd, close_away_odds AS ca,
                  open_home_odds AS oh, open_draw_odds AS od, open_away_odds AS oa,
                  final_result
           FROM interwetten_odds
           WHERE home_score IS NOT NULL AND away_score IS NOT NULL
             AND close_home_odds IS NOT NULL AND final_result IN ('H','D','A')""",
        conn,
    )
    conn.close()
    df["date"] = pd.to_datetime(df["date"])
    return df


# ============================================================================
# Layer 1: Dixon-Coles 公平概率引擎
# ============================================================================

class DCFairPricing:
    """
    Dixon-Coles 公平概率生成器.
    用历史比分 (非赔率) 训练 DC 模型, 生成无市场污染的公平 1X2/OU/AH/CS 概率.
    """

    MAX_GOALS = 8

    def __init__(self, xi: float = 0.0015, ridge: float = 0.01):
        self.xi = xi
        self.ridge = ridge
        self.model: Optional[Dict] = None
        self.teams: List[str] = []
        self._intercept = 0.0
        self._hadv = 0.0
        self._rho = 0.0
        self._attack: Dict[str, float] = {}
        self._defence: Dict[str, float] = {}

    def fit(self, df: pd.DataFrame, as_of: Optional[pd.Timestamp] = None):
        """拟合 DC 模型. df 必须有 home_team, away_team, home_score, away_score, date."""
        import numpy as np
        from scipy.optimize import minimize

        if as_of is None:
            as_of = df["date"].max() + pd.Timedelta(days=1)

        sub = df[df["date"] < as_of].copy()
        # 至少需要一定数量的比赛
        if len(sub) < 100:
            raise ValueError(f"Not enough training data: {len(sub)} matches before {as_of}")

        # 球队筛选: 至少出场 12 次
        counts = pd.concat([sub["home_team"], sub["away_team"]]).value_counts()
        teams = sorted(counts[counts >= 12].index.tolist())
        sub = sub[sub["home_team"].isin(teams) & sub["away_team"].isin(teams)].copy()
        tidx = {t: i for i, t in enumerate(teams)}
        n = len(teams)

        hi = sub["home_team"].map(tidx).to_numpy()
        ai = sub["away_team"].map(tidx).to_numpy()
        hs = sub["home_score"].to_numpy(dtype=int)
        aw = sub["away_score"].to_numpy(dtype=int)
        days = (as_of - sub["date"]).dt.days.to_numpy(dtype=float)
        w = np.exp(-self.xi * days)

        def unpack(p):
            intercept = p[0]
            hadv = p[1]
            atk = np.append(p[2:2 + (n - 1)], 0.0)
            dfc = np.append(p[2 + (n - 1):2 + 2 * (n - 1)], 0.0)
            rho = p[-1]
            return intercept, hadv, atk, dfc, rho

        def negll(p):
            intercept, hadv, atk, dfc, rho = unpack(p)
            lam = np.exp(intercept + hadv + atk[hi] - dfc[ai])
            mu = np.exp(intercept + atk[ai] - dfc[hi])
            lam = np.clip(lam, 1e-6, 25)
            mu = np.clip(mu, 1e-6, 25)
            ll_h = hs * np.log(lam) - lam
            ll_a = aw * np.log(mu) - mu
            # DC tau
            tau = np.ones_like(lam)
            mask00 = (hs == 0) & (aw == 0)
            mask01 = (hs == 0) & (aw == 1)
            mask10 = (hs == 1) & (aw == 0)
            mask11 = (hs == 1) & (aw == 1)
            tau[mask00] = 1.0 - lam[mask00] * mu[mask00] * rho
            tau[mask01] = 1.0 + lam[mask01] * rho
            tau[mask10] = 1.0 + mu[mask10] * rho
            tau[mask11] = 1.0 - rho
            tau = np.clip(tau, 1e-9, None)
            ll_tau = np.log(tau)
            pen = self.ridge * (np.sum(atk**2) + np.sum(dfc**2))
            return -(w * (ll_h + ll_a + ll_tau)).sum() + pen

        x0 = np.zeros(2 + 2 * (n - 1) + 1)
        x0[0] = 0.0
        x0[1] = 0.25
        x0[-1] = -0.05
        bounds = [(-2, 2), (-1, 1)] + [(-3, 3)] * (2 * (n - 1)) + [(-0.2, 0.2)]

        log.info(f"Fitting DC on {len(sub)} matches, {n} teams...")
        res = minimize(negll, x0, method="L-BFGS-B", bounds=bounds,
                       options={"maxiter": 500, "maxfun": 50000})
        intercept, hadv, atk, dfc, rho = unpack(res.x)

        # 强度中心化
        intercept = intercept + atk.mean() - dfc.mean()
        atk = atk - atk.mean()
        dfc = dfc - dfc.mean()

        self._intercept = intercept
        self._hadv = hadv
        self._rho = rho
        self._attack = {t: float(atk[i]) for t, i in tidx.items()}
        self._defence = {t: float(dfc[i]) for t, i in tidx.items()}
        self.teams = teams
        self.model = {
            "intercept": intercept,
            "home_adv": hadv,
            "rho": rho,
            "attack": self._attack,
            "defence": self._defence,
            "n_teams": n,
            "n_train": len(sub),
            "xi": self.xi,
        }
        log.info(f"DC fitted: n_teams={n}, n_train={len(sub)}, rho={rho:.4f}, "
                 f"hadv={hadv:.3f}, intercept={intercept:.3f}")

    def _lambdas(self, home: str, away: str) -> Tuple[float, float]:
        """计算主客队预期进球 lambda."""
        ah = self._attack.get(home, 0.0)
        aa = self._attack.get(away, 0.0)
        dh = self._defence.get(home, 0.0)
        da = self._defence.get(away, 0.0)
        lam = np.exp(self._intercept + self._hadv + ah - da)
        mu = np.exp(self._intercept + aa - dh)
        return float(lam), float(mu)

    def scoreline_matrix(self, home: str, away: str, maxg: int = 8) -> np.ndarray:
        """生成 DC 比分分布矩阵."""
        lam, mu = self._lambdas(home, away)
        h_prob = poisson.pmf(np.arange(maxg + 1), lam)
        a_prob = poisson.pmf(np.arange(maxg + 1), mu)
        M = np.outer(h_prob, a_prob)
        # DC tau 修正
        M[0, 0] *= 1.0 - lam * mu * self._rho
        M[0, 1] *= 1.0 + lam * self._rho
        M[1, 0] *= 1.0 + mu * self._rho
        M[1, 1] *= 1.0 - self._rho
        M = np.clip(M, 0, None)
        return M / M.sum()

    def fair_probabilities(self, home: str, away: str) -> Dict[str, Any]:
        """从 DC 比分矩阵推导所有市场的公平概率."""
        M = self.scoreline_matrix(home, away)
        lam, mu = self._lambdas(home, away)
        maxg = M.shape[0] - 1

        # 1X2
        p_h = float(np.sum(np.tril(M, -1)))
        p_d = float(np.trace(M))
        p_a = float(np.sum(np.triu(M, 1)))

        # OU 2.5
        p_over = float(sum(M[i, j] for i in range(maxg + 1) for j in range(maxg + 1) if i + j > 2.5))
        p_under = float(sum(M[i, j] for i in range(maxg + 1) for j in range(maxg + 1) if i + j < 2.5))

        # AH 0.0 (平手盘)
        p_ah_home = float(sum(M[i, j] for i in range(maxg + 1) for j in range(maxg + 1)
                              if i > j or (i == j and np.random.random() < 0.5)))
        p_ah_away = 1.0 - p_ah_home

        # Top CS
        flat = [(i, j, float(M[i, j])) for i in range(min(6, maxg + 1))
                for j in range(min(6, maxg + 1))]
        flat.sort(key=lambda x: -x[2])
        top_cs = {f"{i}-{j}": p for i, j, p in flat[:10]}

        return {
            "home": home, "away": away,
            "lambda_h": round(lam, 3), "lambda_a": round(mu, 3),
            "rho": round(self._rho, 4),
            "p_h": round(p_h, 4), "p_d": round(p_d, 4), "p_a": round(p_a, 4),
            "p_over25": round(p_over, 4), "p_under25": round(p_under, 4),
            "p_ah_home": round(p_ah_home, 4), "p_ah_away": round(p_ah_away, 4),
            "top_cs": top_cs,
            "matrix": M,
        }


# ============================================================================
# Layer 2: 庄家 Margin 模板识别
# ============================================================================

class MarginTemplateFitter:
    """对每个庄家反推 margin 函数: margin(prob) = f(implied_prob)."""

    @staticmethod
    def deoverround_1x2(oh: float, od: float, oa: float) -> Tuple[float, float, float, float]:
        """1X2 去抽水, 返回 (p_h, p_d, p_a, overround)."""
        inv = 1.0 / oh + 1.0 / od + 1.0 / oa
        overround = inv - 1.0
        return (1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv, overround

    @staticmethod
    def fit_margin_curve(implied_probs: np.ndarray, fair_probs: np.ndarray) -> Dict:
        """拟合适配: market_implied = f(fair_prob) → margin = market_implied - fair_prob.
        
        使用对数线性模型: log(market_p) = a * log(fair_p) + b
        即 margin 随概率增大而增大 (favorite-longshot bias).
        """
        eps = 1e-6
        mask = (implied_probs > eps) & (implied_probs < 1 - eps) & \
               (fair_probs > eps) & (fair_probs < 1 - eps)
        x = np.log(np.clip(fair_probs[mask], eps, None))
        y = np.log(np.clip(implied_probs[mask], eps, None))

        if len(x) < 10:
            return {"a": 1.0, "b": 0.0, "r2": 0.0, "n": len(x)}

        # OLS
        A = np.column_stack([x, np.ones_like(x)])
        coeff, residuals, rank, sv = np.linalg.lstsq(A, y, rcond=None)
        a, b = coeff[0], coeff[1]
        y_pred = A @ coeff
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        return {"a": float(a), "b": float(b), "r2": float(r2), "n": int(len(x))}

    @staticmethod
    def detect_template_deviation(
        fair_p: float, market_implied: float, template_a: float, template_b: float
    ) -> Dict:
        """检测单场赔率是否偏离模板."""
        eps = 1e-6
        expected_market = np.exp(template_a * np.log(max(fair_p, eps)) + template_b)
        expected_market = np.clip(expected_market, 0, 1)
        deviation = market_implied - expected_market
        return {
            "fair_p": round(fair_p, 4),
            "market_implied": round(market_implied, 4),
            "template_expected": round(expected_market, 4),
            "deviation": round(deviation, 4),
            "is_signal": abs(deviation) > 0.05,
        }


# ============================================================================
# Layer 3: 跨市场一致性套利扫描
# ============================================================================

class CrossMarketArbitrageScanner:
    """
    从 CS (波胆) 市场反推 1X2/OU/AH, 与各市场直接报价对比.
    分歧 ≥ 0.10 → 套利/错价信号.
    """

    def __init__(self, cs_calibration_path: str = CS_CALIB):
        with open(cs_calibration_path) as f:
            self.cs_calib = json.load(f)
        self.calib_factors: Dict[str, float] = {}
        for score, data in self.cs_calib.get("calibrated_scores", {}).items():
            # 50% 阻尼校准因子
            factor = data["factor"]
            damped = 1.0 + (factor - 1.0) * 0.5
            self.calib_factors[score] = damped

    def cs_to_1x2(self, cs_odds: List[Tuple[str, float]]) -> Dict[str, float]:
        """从 CS 赔率推导 1X2 隐含概率. cs_odds: [(score, odds), ...]"""
        if not cs_odds:
            return {"p_h": 0, "p_d": 0, "p_a": 0}

        # 去抽水
        inv_sum = sum(1.0 / o for _, o in cs_odds)
        probs = {}
        for score, odds in cs_odds:
            raw_p = (1.0 / odds) / inv_sum
            # 应用校准因子
            calib = self.calib_factors.get(score, 1.0)
            probs[score] = raw_p * calib

        # 归一化
        total = sum(probs.values())
        if total > 0:
            probs = {k: v / total for k, v in probs.items()}

        # 聚合成 1X2
        p_h = p_d = p_a = 0.0
        for score, p in probs.items():
            parts = score.split("-")
            if len(parts) == 2:
                h, a = int(parts[0]), int(parts[1])
                if h > a:
                    p_h += p
                elif h == a:
                    p_d += p
                else:
                    p_a += p
        return {"p_h": round(p_h, 4), "p_d": round(p_d, 4), "p_a": round(p_a, 4)}

    def cs_to_ou(self, cs_probs: Dict[str, float], line: float = 2.5) -> Dict[str, float]:
        """从 CS 推导 OU."""
        p_over = p_under = 0.0
        for score, p in cs_probs.items():
            parts = score.split("-")
            if len(parts) == 2:
                total = int(parts[0]) + int(parts[1])
                if total > line:
                    p_over += p
                elif total < line:
                    p_under += p
        return {"p_over": round(p_over, 4), "p_under": round(p_under, 4)}

    def scan_match(
        self,
        op_cs_raw: Optional[str],
        op_1x2_h: Optional[float],
        op_1x2_d: Optional[float],
        op_1x2_a: Optional[float],
        op_ou_line: Optional[float],
        op_ou_over: Optional[float],
        op_ou_under: Optional[float],
        threshold: float = 0.10,
    ) -> Dict:
        """扫描一场比赛的跨市场一致性."""
        result = {
            "has_cs_data": False,
            "has_1x2_data": False,
            "has_ou_data": False,
            "signals": [],
            "1x2_from_cs": None,
            "ou_from_cs": None,
        }

        # 解析 CS
        if op_cs_raw:
            try:
                cs_data = json.loads(op_cs_raw)
                if isinstance(cs_data, list) and len(cs_data) > 0:
                    cs_odds = [(str(item[0]), float(item[1])) for item in cs_data]
                    cs_1x2 = self.cs_to_1x2(cs_odds)
                    result["has_cs_data"] = True
                    result["1x2_from_cs"] = cs_1x2

                    # CS → OU
                    cs_probs = {}
                    inv_sum = sum(1.0 / o for _, o in cs_odds)
                    for score, odds in cs_odds:
                        raw_p = (1.0 / odds) / inv_sum
                        calib = self.calib_factors.get(score, 1.0)
                        cs_probs[score] = raw_p * calib
                    total = sum(cs_probs.values())
                    if total > 0:
                        cs_probs = {k: v / total for k, v in cs_probs.items()}
                    result["ou_from_cs"] = self.cs_to_ou(cs_probs)
            except (json.JSONDecodeError, ValueError, IndexError):
                pass

        # 对比 1X2
        if all(v is not None for v in [op_1x2_h, op_1x2_d, op_1x2_a]) and result["1x2_from_cs"]:
            result["has_1x2_data"] = True
            _, market_h, market_d, market_a, _ = MarginTemplateFitter.deoverround_1x2(
                op_1x2_h, op_1x2_d, op_1x2_a
            )
            cs = result["1x2_from_cs"]
            for label, mkt_val, cs_val in [("H", market_h, cs["p_h"]),
                                            ("D", market_d, cs["p_d"]),
                                            ("A", market_a, cs["p_a"])]:
                dev = cs_val - mkt_val
                if abs(dev) >= threshold:
                    result["signals"].append({
                        "type": "1X2_vs_CS",
                        "outcome": label,
                        "market_implied": round(mkt_val, 4),
                        "cs_derived": round(cs_val, 4),
                        "deviation": round(dev, 4),
                        "direction": "UNDERVALUED" if dev > 0 else "OVERVALUED",
                    })

        # 对比 OU
        if all(v is not None for v in [op_ou_over, op_ou_under]) and result["ou_from_cs"]:
            result["has_ou_data"] = True
            ou_inv = 1.0 / op_ou_over + 1.0 / op_ou_under
            mkt_over = (1.0 / op_ou_over) / ou_inv
            cs_over = result["ou_from_cs"]["p_over"]
            dev = cs_over - mkt_over
            if abs(dev) >= threshold:
                result["signals"].append({
                    "type": "OU_vs_CS",
                    "outcome": "OVER",
                    "market_implied": round(mkt_over, 4),
                    "cs_derived": round(cs_over, 4),
                    "deviation": round(dev, 4),
                    "direction": "UNDERVALUED" if dev > 0 else "OVERVALUED",
                })

        return result


# ============================================================================
# Layer 4: 开盘-收盘-赛果三联表
# ============================================================================

@dataclass
class TripletRow:
    """单场三联表记录."""
    match_id: str
    home: str
    away: str
    league: str
    kickoff: str
    actual_result: str  # home/draw/away
    actual_score: str   # "2-1"

    # 开盘 (GQ 初盘)
    op_h: Optional[float] = None
    op_d: Optional[float] = None
    op_a: Optional[float] = None

    # 收盘 (最晚快照)
    cl_h: Optional[float] = None
    cl_d: Optional[float] = None
    cl_a: Optional[float] = None

    # DC 公平概率
    dc_p_h: float = 0.0
    dc_p_d: float = 0.0
    dc_p_a: float = 0.0

    # 偏差
    op_dev_h: float = 0.0  # dc_p_h - market_implied_h (正=市场低估主胜)
    op_dev_d: float = 0.0
    op_dev_a: float = 0.0
    cl_dev_h: float = 0.0
    cl_dev_d: float = 0.0
    cl_dev_a: float = 0.0

    # CLV
    beat_closing_h: bool = False
    beat_closing_d: bool = False
    beat_closing_a: bool = False

    # 跨市场信号
    cross_market_signals: int = 0

    def to_dict(self) -> Dict:
        return {
            "match_id": self.match_id,
            "home": self.home,
            "away": self.away,
            "league": self.league,
            "kickoff": self.kickoff,
            "actual_result": self.actual_result,
            "actual_score": self.actual_score,
            "op_h": self.op_h, "op_d": self.op_d, "op_a": self.op_a,
            "cl_h": self.cl_h, "cl_d": self.cl_d, "cl_a": self.cl_a,
            "dc_p_h": round(self.dc_p_h, 4),
            "dc_p_d": round(self.dc_p_d, 4),
            "dc_p_a": round(self.dc_p_a, 4),
            "op_dev_h": round(self.op_dev_h, 4),
            "op_dev_d": round(self.op_dev_d, 4),
            "op_dev_a": round(self.op_dev_a, 4),
            "cl_dev_h": round(self.cl_dev_h, 4),
            "cl_dev_d": round(self.cl_dev_d, 4),
            "cl_dev_a": round(self.cl_dev_a, 4),
            "beat_closing_h": self.beat_closing_h,
            "beat_closing_d": self.beat_closing_d,
            "beat_closing_a": self.beat_closing_a,
            "cross_market_signals": self.cross_market_signals,
        }


def build_triplet_table(
    gq_df: pd.DataFrame,
    dc_engine: DCFairPricing,
    scanner: CrossMarketArbitrageScanner,
) -> Tuple[List[TripletRow], Dict]:
    """构建完整的三联表."""
    rows = []
    stats = {
        "total": len(gq_df),
        "with_op_1x2": 0,
        "with_cl_1x2": 0,
        "cross_signals": 0,
        "dev_gt_5pct_h": 0,
        "dev_gt_5pct_d": 0,
        "dev_gt_5pct_a": 0,
        "beat_cl_h": 0,
        "beat_cl_d": 0,
        "beat_cl_a": 0,
    }

    for _, r in gq_df.iterrows():
        tr = TripletRow(
            match_id=str(r.get("mid", "")),
            home=str(r["home"]),
            away=str(r["away"]),
            league=str(r.get("league", "")),
            kickoff=str(r.get("kickoff", "")),
            actual_result=str(r["result"]),
            actual_score=f"{int(r['score_home'])}-{int(r['score_away'])}",
        )

        # 开盘赔率
        if r.get("op_1x2_h") is not None:
            tr.op_h = float(r["op_1x2_h"])
            tr.op_d = float(r["op_1x2_d"])
            tr.op_a = float(r["op_1x2_a"])
            stats["with_op_1x2"] += 1

        # DC 公平概率
        try:
            fair = dc_engine.fair_probabilities(str(r["home"]), str(r["away"]))
            tr.dc_p_h = fair["p_h"]
            tr.dc_p_d = fair["p_d"]
            tr.dc_p_a = fair["p_a"]
        except Exception:
            pass

        # 计算偏差
        if tr.op_h and tr.op_d and tr.op_a:
            mkt_h, mkt_d, mkt_a, _ = MarginTemplateFitter.deoverround_1x2(
                tr.op_h, tr.op_d, tr.op_a
            )
            tr.op_dev_h = tr.dc_p_h - mkt_h
            tr.op_dev_d = tr.dc_p_d - mkt_d
            tr.op_dev_a = tr.dc_p_a - mkt_a

            if abs(tr.op_dev_h) > 0.05:
                stats["dev_gt_5pct_h"] += 1
            if abs(tr.op_dev_d) > 0.05:
                stats["dev_gt_5pct_d"] += 1
            if abs(tr.op_dev_a) > 0.05:
                stats["dev_gt_5pct_a"] += 1

            # CLV: dc_p > market_closing_implied?
            if tr.cl_h and tr.cl_d and tr.cl_a:
                _, cl_h, cl_d, cl_a, _ = MarginTemplateFitter.deoverround_1x2(
                    tr.cl_h, tr.cl_d, tr.cl_a
                )
                tr.beat_closing_h = tr.dc_p_h > cl_h
                tr.beat_closing_d = tr.dc_p_d > cl_d
                tr.beat_closing_a = tr.dc_p_a > cl_a
                stats["beat_cl_h"] += int(tr.beat_closing_h)
                stats["beat_cl_d"] += int(tr.beat_closing_d)
                stats["beat_cl_a"] += int(tr.beat_closing_a)

        # 跨市场扫描
        try:
            scan = scanner.scan_match(
                op_cs_raw=r.get("op_cs"),
                op_1x2_h=tr.op_h,
                op_1x2_d=tr.op_d,
                op_1x2_a=tr.op_a,
                op_ou_line=r.get("op_ou_line"),
                op_ou_over=r.get("op_ou_over"),
                op_ou_under=r.get("op_ou_under"),
            )
            tr.cross_market_signals = len(scan.get("signals", []))
            stats["cross_signals"] += tr.cross_market_signals
        except Exception:
            pass

        rows.append(tr)

    return rows, stats


# ============================================================================
# Layer 5: 验证分析
# ============================================================================

def analyze_triplets(rows: List[TripletRow]) -> Dict:
    """对三联表做统计验证."""
    df = pd.DataFrame([r.to_dict() for r in rows])

    results = {"total_matches": len(df)}

    # 1. 偏差分层 ROI
    for outcome, label in [("H", "home"), ("D", "draw"), ("A", "away")]:
        col_dev = f"op_dev_{outcome.lower()}"
        bins = [(-999, -0.10), (-0.10, -0.05), (-0.05, 0.05), (0.05, 0.10), (0.10, 999)]
        bin_labels = ["<-10%", "-10%~-5%", "-5%~5%", "5%~10%", ">10%"]

        bin_stats = []
        for (lo, hi), bl in zip(bins, bin_labels):
            mask = (df[col_dev] > lo) & (df[col_dev] <= hi)
            n = mask.sum()
            if n == 0:
                bin_stats.append({"bin": bl, "n": 0, "actual_win_rate": None, "dc_prob_mean": None})
                continue
            sub = df[mask]
            actual_win = (sub["actual_result"] == label).mean()
            dc_mean = sub[f"dc_p_{outcome.lower()}"].mean()
            bin_stats.append({
                "bin": bl, "n": int(n),
                "actual_win_rate": round(float(actual_win), 4),
                "dc_prob_mean": round(float(dc_mean), 4),
                "dc_vs_actual": round(float(dc_mean - actual_win), 4),
            })
        results[f"dev_stratification_{outcome}"] = bin_stats

    # 2. Beat Closing % 统计
    for outcome in ["H", "D", "A"]:
        col = f"beat_closing_{outcome.lower()}"
        if col in df.columns:
            results[f"beat_closing_{outcome}_pct"] = round(
                float(df[col].mean()) if df[col].sum() > 0 else 0, 4
            )

    # 3. 跨市场信号统计
    results["total_cross_signals"] = int(df["cross_market_signals"].sum())
    results["matches_with_signals"] = int((df["cross_market_signals"] > 0).sum())

    # 4. 偏差 > 5% 的比赛实际胜率 vs DC 概率
    for outcome, label in [("H", "home"), ("D", "draw"), ("A", "away")]:
        mask = abs(df[f"op_dev_{outcome.lower()}"]) > 0.05
        n = mask.sum()
        if n > 0:
            sub = df[mask]
            actual = (sub["actual_result"] == label).mean()
            dc_mean = sub[f"dc_p_{outcome.lower()}"].mean()
            results[f"large_dev_{outcome}_n"] = int(n)
            results[f"large_dev_{outcome}_actual"] = round(float(actual), 4)
            results[f"large_dev_{outcome}_dc"] = round(float(dc_mean), 4)

    return results


# ============================================================================
# 主流程
# ============================================================================

def run_full_pipeline():
    """执行完整定价模板反演流程."""
    log.info("=" * 60)
    log.info("哨响AI 定价模板反演引擎 v1.0")
    log.info("=" * 60)

    # Step 1: 加载数据
    log.info("\n[Step 1/6] 加载数据...")
    iw_df = load_interwetten()
    gq_df = load_gq_matches()
    log.info(f"  Interwetten: {len(iw_df)} 场 (2016-2025)")
    log.info(f"  GQ match_outcomes: {len(gq_df)} 场")

    # Step 2: 拟合 DC 模型
    log.info("\n[Step 2/6] 拟合 Dixon-Coles 公平概率模型...")
    dc = DCFairPricing(xi=0.0015, ridge=0.01)
    dc.fit(iw_df)
    log.info(f"  ρ = {dc._rho:.4f}, home_adv = {dc._hadv:.3f}, "
             f"intercept = {dc._intercept:.3f}")

    # Step 3: 初始化跨市场扫描器
    log.info("\n[Step 3/6] 初始化跨市场套利扫描器...")
    scanner = CrossMarketArbitrageScanner()

    # Step 4: 构建三联表
    log.info("\n[Step 4/6] 构建开盘-收盘-DC-赛果三联表...")
    rows, build_stats = build_triplet_table(gq_df, dc, scanner)
    log.info(f"  共 {len(rows)} 条记录")
    log.info(f"  有开盘1X2: {build_stats['with_op_1x2']}")
    log.info(f"  偏差>5%: H={build_stats['dev_gt_5pct_h']}, "
             f"D={build_stats['dev_gt_5pct_d']}, A={build_stats['dev_gt_5pct_a']}")
    log.info(f"  跨市场信号: {build_stats['cross_signals']} 个")

    # Step 5: 验证分析
    log.info("\n[Step 5/6] 验证分析...")
    analysis = analyze_triplets(rows)

    # Step 6: 拟合 margin 模板
    log.info("\n[Step 6/6] 拟合庄家 margin 模板...")
    # 从三联表提取 (fair_p, market_implied) 对
    fair_vals = []
    mkt_vals = []
    for r in rows:
        if r.op_h and r.op_d and r.op_a:
            mh, md, ma, _ = MarginTemplateFitter.deoverround_1x2(r.op_h, r.op_d, r.op_a)
            fair_vals.extend([r.dc_p_h, r.dc_p_d, r.dc_p_a])
            mkt_vals.extend([mh, md, ma])
    margin_fit = MarginTemplateFitter.fit_margin_curve(
        np.array(fair_vals), np.array(mkt_vals)
    )
    log.info(f"  Margin 模板: log(market_p) = {margin_fit['a']:.4f} * log(fair_p) + {margin_fit['b']:.4f}")
    log.info(f"  R² = {margin_fit['r2']:.4f}, n = {margin_fit['n']}")

    # 输出
    output = {
        "engine_version": "1.0",
        "generated_at": datetime.now().isoformat(),
        "dc_model": dc.model,
        "margin_template": margin_fit,
        "build_stats": build_stats,
        "analysis": analysis,
        "triplet_count": len(rows),
        "sample_triplets": [r.to_dict() for r in rows[:5]],
    }

    # 保存
    out_path = os.path.join(OUT_DIR, "pricing_template_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"\n报告已保存: {out_path}")

    # 保存完整三联表 CSV
    df_triplet = pd.DataFrame([r.to_dict() for r in rows])
    csv_path = os.path.join(OUT_DIR, "triplet_table.csv")
    df_triplet.to_csv(csv_path, index=False, encoding="utf-8-sig")
    log.info(f"三联表 CSV: {csv_path} ({len(df_triplet)} 行)")

    return output, rows, dc


if __name__ == "__main__":
    output, rows, dc = run_full_pipeline()
    print("\n" + "=" * 60)
    print("定价模板反演完成!")
    print(f"  DC ρ = {dc._rho:.4f}")
    print(f"  Margin 模板 R² = {output['margin_template']['r2']:.4f}")
    print(f"  三联表: {len(rows)} 行")
    print(f"  跨市场信号: {output['analysis']['total_cross_signals']} 个")
    print(f"  Beat Closing H: {output['analysis'].get('beat_closing_H_pct', 'N/A')}")
    print("=" * 60)
