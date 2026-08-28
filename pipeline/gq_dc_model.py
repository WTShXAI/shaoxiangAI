# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ⚠ DEPRECATED — 2026-08-05 模型收敛 (M1-M7)                          ║
# ║  已下线: gq_dc_model 权重已归档                                    ║
# ║  替代: M2 pipeline/score_model.py + M7 模板偏离                      ║
# ║  单一真相源: pipeline/model_catalog.py                                ║
# ║  本文件保留仅为历史可追溯, 禁止在新代码中引用.                        ║
# ╚══════════════════════════════════════════════════════════════════════╝
"""
哨响AI — GQ-native DC 公平概率模型 v1.0
========================================
用 GQ 自己的 match_outcomes 比分 (非赔率) 训练 DC 模型,
产生与 GQ 同源的公平概率。

对比: Interwetten-DC (之前跑的) vs GQ-DC (这里跑的)
目标: 验证 GQ-native DC 能否避免之前的 "反信号" 问题

训练参数调整 (相比 Interwetten-DC):
  - min_matches: 12 → 6 (GQ 球队样本少)
  - train_years: 3 → 2 (GQ 数据时间跨度短)
  - 不加时间衰减 (GQ 数据都是 2026, 衰减无意义)
"""

from __future__ import annotations
import os
import sys
import json
import sqlite3
import logging
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any

import numpy as np
import pandas as pd
from scipy.stats import poisson
from scipy.optimize import minimize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
GQ_DB = os.path.join(DATA_DIR, "events.db")
OUT_DIR = os.path.join(DATA_DIR, "pricing_template")
os.makedirs(OUT_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gq_dc")


class GQDCFairPricing:
    """
    GQ-native Dixon-Coles 公平概率生成器.
    用 GQ match_outcomes 的比分训练, 不碰任何赔率.
    """

    MAX_GOALS = 8

    def __init__(self, min_matches: int = 6, train_years: float = 2.0, ridge: float = 0.05):
        self.min_matches = min_matches
        self.train_years = train_years
        self.ridge = ridge
        self.teams: List[str] = []
        self._intercept = 0.0
        self._hadv = 0.0
        self._rho = 0.0
        self._attack: Dict[str, float] = {}
        self._defence: Dict[str, float] = {}
        self.model: Optional[Dict] = None

    def fit(self, df: pd.DataFrame, as_of: Optional[pd.Timestamp] = None):
        """拟合 DC 模型. df 必须有 home_team, away_team, home_score, away_score, date."""
        if as_of is None:
            as_of = df["date"].max() + pd.Timedelta(days=1)

        sub = df[df["date"] < as_of].copy()
        if self.train_years:
            sub = sub[sub["date"] >= as_of - pd.Timedelta(days=int(365.25 * self.train_years))]

        if len(sub) < 50:
            log.warning(f"Insufficient training data: {len(sub)} matches before {as_of}")
            return False

        counts = pd.concat([sub["home_team"], sub["away_team"]]).value_counts()
        teams = sorted(counts[counts >= self.min_matches].index.tolist())
        if len(teams) < 4:
            log.warning(f"Not enough teams with >= {self.min_matches} matches: {len(teams)}")
            return False

        sub = sub[sub["home_team"].isin(teams) & sub["away_team"].isin(teams)].copy()
        tidx = {t: i for i, t in enumerate(teams)}
        n = len(teams)

        hi = sub["home_team"].map(tidx).to_numpy()
        ai = sub["away_team"].map(tidx).to_numpy()
        hs = sub["home_score"].to_numpy(dtype=int)
        aw = sub["away_score"].to_numpy(dtype=int)

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
            return -(ll_h + ll_a + ll_tau).sum() + pen

        x0 = np.zeros(2 + 2 * (n - 1) + 1)
        x0[0] = 0.0
        x0[1] = 0.25
        x0[-1] = -0.05
        bounds = [(-2, 2), (-1, 1)] + [(-3, 3)] * (2 * (n - 1)) + [(-0.2, 0.2)]

        log.info(f"Fitting GQ-DC on {len(sub)} matches, {n} teams...")
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
            "train_window": f"{self.train_years}yr",
            "min_matches": self.min_matches,
        }
        log.info(f"GQ-DC fitted: n_teams={n}, n_train={len(sub)}, "
                 f"rho={rho:.4f}, hadv={hadv:.3f}, intercept={intercept:.3f}")
        return True

    def _lambdas(self, home: str, away: str) -> Tuple[float, float]:
        ah = self._attack.get(home, 0.0)
        aa = self._attack.get(away, 0.0)
        dh = self._defence.get(home, 0.0)
        da = self._defence.get(away, 0.0)
        lam = np.exp(self._intercept + self._hadv + ah - da)
        mu = np.exp(self._intercept + aa - dh)
        return float(lam), float(mu)

    def scoreline_matrix(self, home: str, away: str, maxg: int = 8) -> np.ndarray:
        lam, mu = self._lambdas(home, away)
        h_prob = poisson.pmf(np.arange(maxg + 1), lam)
        a_prob = poisson.pmf(np.arange(maxg + 1), mu)
        M = np.outer(h_prob, a_prob)
        M[0, 0] *= 1.0 - lam * mu * self._rho
        M[0, 1] *= 1.0 + lam * self._rho
        M[1, 0] *= 1.0 + mu * self._rho
        M[1, 1] *= 1.0 - self._rho
        M = np.clip(M, 0, None)
        return M / M.sum()

    def fair_probabilities(self, home: str, away: str) -> Dict[str, Any]:
        if home not in self._attack and away not in self._attack:
            # 两队都不在训练集 → 返回先验
            return {"home": home, "away": away, "unknown": True,
                    "p_h": 0.42, "p_d": 0.27, "p_a": 0.31,
                    "lambda_h": 1.3, "lambda_a": 1.2, "rho": self._rho}

        M = self.scoreline_matrix(home, away)
        lam, mu = self._lambdas(home, away)
        maxg = M.shape[0] - 1
        p_h = float(np.sum(np.tril(M, -1)))
        p_d = float(np.trace(M))
        p_a = float(np.sum(np.triu(M, 1)))

        p_over = float(sum(M[i, j] for i in range(maxg + 1) for j in range(maxg + 1) if i + j > 2.5))
        p_under = float(sum(M[i, j] for i in range(maxg + 1) for j in range(maxg + 1) if i + j < 2.5))

        return {
            "home": home, "away": away, "unknown": False,
            "lambda_h": round(lam, 3), "lambda_a": round(mu, 3),
            "rho": round(self._rho, 4),
            "p_h": round(p_h, 4), "p_d": round(p_d, 4), "p_a": round(p_a, 4),
            "p_over25": round(p_over, 4), "p_under25": round(p_under, 4),
        }


def load_gq_scores() -> pd.DataFrame:
    """加载 GQ match_outcomes 的比分数据."""
    conn = sqlite3.connect(GQ_DB)
    df = pd.read_sql_query(
        """SELECT home, away, score_home AS home_score, score_away AS away_score, kickoff
           FROM match_outcomes
           WHERE score_home IS NOT NULL AND score_away IS NOT NULL
             AND home IS NOT NULL AND away IS NOT NULL""",
        conn,
    )
    conn.close()
    df["date"] = pd.to_datetime(df["kickoff"], errors="coerce")
    df["home_team"] = df["home"]
    df["away_team"] = df["away"]
    df = df.dropna(subset=["date"])
    return df


def compare_with_interwetten_dc():
    """
    对比: GQ-DC vs Interwetten-DC 在 GQ 比赛上的表现
    目标: 验证 GQ-native 是否避免 "反信号" 问题
    """
    log.info("\n" + "=" * 60)
    log.info("GQ-native DC vs Interwetten-DC 对比")
    log.info("=" * 60)

    # 加载数据
    gq_scores = load_gq_scores()
    log.info(f"GQ scores: {len(gq_scores)} matches, "
             f"{gq_scores['home_team'].nunique()} unique teams")

    # 训练 GQ-DC (降低 min_matches 以覆盖更多球队)
    gq_dc = GQDCFairPricing(min_matches=3, train_years=2.0, ridge=0.1)
    ok = gq_dc.fit(gq_scores)

    if not ok:
        log.error("GQ-DC 拟合失败")
        return None

    # 内联训练 Interwetten-DC 做公平对比
    log.info("加载 Interwetten 数据并训练对比模型...")
    try:
        from pricing_template_engine import DCFairPricing, load_interwetten
        iw_df = load_interwetten()
        iw_dc = DCFairPricing(xi=0.0015, ridge=0.01)
        iw_dc.fit(iw_df)
        log.info(f"Interwetten-DC 训练完成: {iw_dc.model['n_teams']} 队")
    except Exception as e:
        log.warning(f"Interwetten-DC 训练失败: {e}")
        iw_dc = None

    # 在 GQ 比赛上评估两个模型
    eval_df = gq_scores.copy()
    eval_df["date"] = pd.to_datetime(eval_df["kickoff"], errors="coerce")

    gq_preds = []
    iw_preds = []
    actuals = []

    for _, r in eval_df.iterrows():
        actual = int(r["home_score"]) - int(r["away_score"])
        actual_label = "home" if actual > 0 else "draw" if actual == 0 else "away"
        actuals.append(actual_label)

        # GQ-DC
        gq_p = gq_dc.fair_probabilities(r["home_team"], r["away_team"])
        gq_preds.append(gq_p)

        # Interwetten-DC (如果可用)
        if iw_dc:
            try:
                iw_p = iw_dc.fair_probabilities(r["home_team"], r["away_team"])
                iw_preds.append(iw_p)
            except Exception:
                iw_preds.append(None)

    # 计算相关系数
    log.info(f"\nGQ-DC 评估 ({len(gq_preds)} 场):")
    gq_p_h = np.array([p["p_h"] for p in gq_preds])
    gq_p_d = np.array([p["p_d"] for p in gq_preds])
    gq_p_a = np.array([p["p_a"] for p in gq_preds])

    # 实际胜率 (按概率分箱)
    is_home = np.array([1 if a == "home" else 0 for a in actuals])
    is_draw = np.array([1 if a == "draw" else 0 for a in actuals])
    is_away = np.array([1 if a == "away" else 0 for a in actuals])

    results = {
        "gq_dc": {
            "n_teams": gq_dc.model["n_teams"],
            "n_train": gq_dc.model["n_train"],
            "rho": gq_dc.model["rho"],
            "home_adv": gq_dc.model["home_adv"],
            "intercept": gq_dc.model["intercept"],
        },
        "comparison": {},
    }

    # GQ-DC 的偏差方向 vs 实际
    for name, pred_arr, actual_arr in [
        ("H", gq_p_h, is_home),
        ("D", gq_p_d, is_draw),
        ("A", gq_p_a, is_away),
    ]:
        # 分箱
        bins = np.quantile(pred_arr, [0, 0.2, 0.4, 0.6, 0.8, 1.0])
        binned = np.digitize(pred_arr, bins[1:-1])
        corr = np.corrcoef(pred_arr, actual_arr)[0, 1]
        results["comparison"][f"gq_dc_corr_{name}"] = round(float(corr), 4)
        log.info(f"  GQ-DC {name}: corr(pred, actual)={corr:+.4f}")

    if iw_dc and iw_preds and any(p is not None for p in iw_preds):
        iw_p_h = np.array([p["p_h"] if p else np.nan for p in iw_preds])
        iw_p_d = np.array([p["p_d"] if p else np.nan for p in iw_preds])
        iw_p_a = np.array([p["p_a"] if p else np.nan for p in iw_preds])

        for name, pred_arr, actual_arr in [
            ("H", iw_p_h, is_home),
            ("D", iw_p_d, is_draw),
            ("A", iw_p_a, is_away),
        ]:
            mask = ~np.isnan(pred_arr)
            if mask.sum() > 10:
                corr = np.corrcoef(pred_arr[mask], actual_arr[mask])[0, 1]
                results["comparison"][f"iw_dc_corr_{name}"] = round(float(corr), 4)
                log.info(f"  IW-DC {name}: corr(pred, actual)={corr:+.4f} (n={mask.sum()})")

    # 保存 GQ-DC 模型
    try:
        import joblib
        model_out = os.path.join(OUT_DIR, "gq_dc_model.joblib")
        joblib.dump(gq_dc, model_out)
        log.info(f"GQ-DC 模型已保存: {model_out}")
    except ImportError:
        log.warning("joblib 不可用, 跳过模型保存")

    # 保存结果
    result_path = os.path.join(OUT_DIR, "gq_dc_comparison.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log.info(f"对比结果: {result_path}")

    return results


if __name__ == "__main__":
    compare_with_interwetten_dc()
