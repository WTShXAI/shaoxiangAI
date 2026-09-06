"""
哨响AI — 时间衰减 Dixon-Coles 波胆模型 (v1, 开源借脑落地)
========================================================
来源/学习对象 (均已克隆至 GitHub/oss-football, MIT/可借鉴):
  - rrclaw/worldcup-predictor  skill/model/dixon_coles.py  (DC + 指数时间衰减 + ridge + 强度中心化)
  - wc26-predict  score_matrix_fusion.py  _calibrate_matrix_to_outcomes  (波胆HDA服从1X2锚)
  - JetQiao/football-prediction-skill  tilt_matrix  (市场矩阵融合+对齐1X2)
  - Torvaney/mezzala  (ρ 作为标准拟合参数, 默认开启)
  - imarranz/modelling-football-scores, RyanSCodes/Dixon-Coles-Football-Predictor (衰减/ρ 佐证)

为什么做这个 (对照我们 score_model.py 的不足):
  - 我们现有波胆 = OIP 独立 Poisson (从1X2去水反解λ, 再独立Poisson矩阵).
    独立 Poisson 假设主客进球独立 -> 低估 0-0/1-1 等低比分联合概率, 且无球队实力结构.
  - 之前尝试 DC(ρ修正) 因 106场WC小样本过拟合而默认关闭(rho=0).
    现在 Interwetten 14万场 -> 大样本 MLE 可同时拟合 decay + ρ + 强度, 不再过拟合.
  - OIP 的波胆分布与我们的 1X2 锚(deoverround)在数值上一致, 但 DC 提供"球队实力结构+低分依赖",
    二者做市场锚定集成(各向同性tilt到同一1X2)可取长补短.

核心方法:
  1. fit(): 时间衰减加权 MLE (xi 日衰减, ridge 过拟合护栏) 拟合 attack/defence/home_adv/rho/intercept.
     仅用 as_of 之前的比赛 (look-ahead free). 强度中心化避免 λ 静默减半(rrclaw 踩过的坑).
  2. scoreline_matrix(): 完整比分矩阵 + DC τ 低分修正(rho 拟合, 边界[-0.2,0.2]).
  3. tilt_to_outcomes(): 把波胆 HDA 边缘乘性缩放到目标1X2(市场去水概率) -> 与盘口锚统一.
  4. predict_score_dc(): DC 比分矩阵 tilt 到市场1X2, 再与 OIP 比分矩阵做市场锚定集成 -> top 波胆.

依赖: numpy/scipy/pandas (与项目一致). 纯离线, 不碰 SSoT(score_model.py 不变).
"""
from __future__ import annotations
import os
import sqlite3
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from typing import Dict, Any, Optional, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "football_data.db")

# ---- 默认超参 (walk-forward 调参空间在 verify_dc_score.py 里网格搜索) ----
XI_DEFAULT = 0.0015      # 日衰减 (rrclaw 用 0.0010; 我们全局联赛更密, 略大)
RIDGE_DEFAULT = 0.01     # 过拟合护栏
MIN_MATCHES = 12         # 入选球队最少场次
TRAIN_YEARS = 3.0        # 训练窗口(年)


def _tau(h, a, lam, mu, rho):
    """DC 低分依赖修正 (rrclaw 实现, MIT)."""
    t = np.ones_like(lam, dtype=float)
    t = np.where((h == 0) & (a == 0), 1.0 - lam * mu * rho, t)
    t = np.where((h == 0) & (a == 1), 1.0 + lam * rho, t)
    t = np.where((h == 1) & (a == 0), 1.0 + mu * rho, t)
    t = np.where((h == 1) & (a == 1), 1.0 - rho, t)
    return np.clip(t, 1e-9, None)


def load_iw() -> pd.DataFrame:
    """加载 interwetten_odds 为 DC 拟合用 DataFrame."""
    c = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """SELECT home_team_norm AS home_team, away_team_norm AS away_team,
                  home_score, away_score, match_date AS date,
                  close_home_odds AS ch, close_draw_odds AS cd, close_away_odds AS ca,
                  open_home_odds AS oh, open_draw_odds AS od, open_away_odds AS oa,
                  final_result
           FROM interwetten_odds
           WHERE home_score IS NOT NULL AND away_score IS NOT NULL
             AND close_home_odds IS NOT NULL AND final_result IN ('H','D','A')""",
        c,
    )
    c.close()
    df["date"] = pd.to_datetime(df["date"])
    return df


def fit(df: pd.DataFrame, as_of: pd.Timestamp, xi: float = XI_DEFAULT, ridge: float = RIDGE_DEFAULT,
        min_matches: int = MIN_MATCHES, train_years: float = TRAIN_YEARS) -> Dict[str, Any]:
    """时间衰减加权 DC MLE. 仅用 date < as_of 的比赛. 返回模型字典."""
    sub = df[df["date"] < as_of].copy()
    if train_years:
        sub = sub[sub["date"] >= as_of - pd.Timedelta(days=int(365.25 * train_years))]
    if sub.empty:
        raise ValueError("no training data before as_of")
    counts = pd.concat([sub["home_team"], sub["away_team"]]).value_counts()
    teams = sorted(counts[counts >= min_matches].index.tolist())
    if len(teams) < 2:
        raise ValueError("not enough teams")
    sub = sub[sub["home_team"].isin(teams) & sub["away_team"].isin(teams)].copy()
    tidx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    hi = sub["home_team"].map(tidx).to_numpy()
    ai = sub["away_team"].map(tidx).to_numpy()
    hs = sub["home_score"].to_numpy(dtype=int)
    aw = sub["away_score"].to_numpy(dtype=int)
    days = (as_of - sub["date"]).dt.days.to_numpy(dtype=float)
    w = np.exp(-xi * days)

    def unpack(p):
        intercept = p[0]; hadv = p[1]
        atk = np.append(p[2:2 + (n - 1)], 0.0)
        dfc = np.append(p[2 + (n - 1):2 + 2 * (n - 1)], 0.0)
        rho = p[-1]
        return intercept, hadv, atk, dfc, rho

    def negll(p):
        intercept, hadv, atk, dfc, rho = unpack(p)
        lam = np.exp(intercept + hadv + atk[hi] - dfc[ai])
        mu = np.exp(intercept + atk[ai] - dfc[hi])
        lam = np.clip(lam, 1e-6, 25); mu = np.clip(mu, 1e-6, 25)
        ll_h = hs * np.log(lam) - lam
        ll_a = aw * np.log(mu) - mu
        ll_tau = np.log(_tau(hs, aw, lam, mu, rho))
        pen = ridge * (np.sum(atk**2) + np.sum(dfc**2))
        return -(w * (ll_h + ll_a + ll_tau)).sum() + pen

    x0 = np.zeros(2 + 2 * (n - 1) + 1)
    x0[0] = 0.0; x0[1] = 0.25; x0[-1] = -0.05
    bounds = [(-2, 2), (-1, 1)] + [(-3, 3)] * (2 * (n - 1)) + [(-0.2, 0.2)]
    res = minimize(negll, x0, method="L-BFGS-B", bounds=bounds,
                   options={"maxiter": 300, "maxfun": 30000})
    intercept, hadv, atk, dfc, rho = unpack(res.x)
    # 强度中心化(把均值折回 intercept, 避免 λ 静默减半)
    intercept = intercept + atk.mean() - dfc.mean()
    atk = atk - atk.mean(); dfc = dfc - dfc.mean()
    return {
        "teams": teams, "attack": {t: float(atk[i]) for t, i in tidx.items()},
        "defence": {t: float(dfc[i]) for t, i in tidx.items()},
        "home_adv": float(hadv), "rho": float(rho), "xi": xi,
        "intercept": float(intercept), "n_train": int(len(sub)),
    }


def _lambdas(m: Dict[str, Any], home: str, away: str) -> Tuple[float, float]:
    ah = m["attack"].get(home, 0.0); aa = m["attack"].get(away, 0.0)
    dh = m["defence"].get(home, 0.0); da = m["defence"].get(away, 0.0)
    lam = np.exp(m["intercept"] + m["home_adv"] + ah - da)
    mu = np.exp(m["intercept"] + aa - dh)
    return float(lam), float(mu)


def scoreline_matrix(lam: float, mu: float, rho: float, maxg: int = 8) -> np.ndarray:
    from scipy.stats import poisson
    h = poisson.pmf(np.arange(maxg + 1), lam)
    a = poisson.pmf(np.arange(maxg + 1), mu)
    m = np.outer(h, a)
    m[0, 0] *= 1.0 - lam * mu * rho
    m[0, 1] *= 1.0 + lam * rho
    m[1, 0] *= 1.0 + mu * rho
    m[1, 1] *= 1.0 - rho
    m = np.clip(m, 0, None)
    return m / m.sum()


def deoverround(oh, od, oa):
    inv = 1.0 / oh + 1.0 / od + 1.0 / oa
    return np.array([(1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv])


def tilt_to_outcomes(M: np.ndarray, target: np.ndarray) -> np.ndarray:
    """把比分矩阵 HDA 边缘乘性缩放到 target(市场1X2). 保持类内形状, 不引入负数.
    对应 wc26 _calibrate_matrix_to_outcomes / JetQiao tilt_matrix 的思想."""
    maxg = M.shape[0] - 1
    pd_ = np.tril(M, -1).sum(); pp = np.trace(M); pa = np.triu(M, 1).sum()
    s = M.copy()
    s = np.where(np.tril(np.ones_like(M), -1) == 1, M * target[0] / pd_, s)
    s = np.where(np.eye(M.shape[0]) == 1, M * target[1] / pp, s)
    s = np.where(np.triu(np.ones_like(M), 1) == 1, M * target[2] / pa, s)
    return s / s.sum()


def predict_score_dc(m: Dict[str, Any], home: str, away: str,
                      close_odds: Tuple[float, float, float],
                      oip_matrix: Optional[np.ndarray] = None,
                      w_oip: float = 0.5, maxg: int = 8, goal_scale: float = 1.0) -> Dict[str, Any]:
    """DC 波胆预测: tilt 到市场1X2, 可选与 OIP 比分矩阵集成.
    goal_scale: 对 DC λ 的全局缩放(修正 DC 对总进球的系统性偏差, 类似 score_model 的 goal_scale).
    """
    lam, mu = _lambdas(m, home, away)
    lam, mu = lam * goal_scale, mu * goal_scale
    target = deoverround(*close_odds)           # 市场1X2锚
    M_dc = scoreline_matrix(lam, mu, m["rho"], maxg)
    M_dc = tilt_to_outcomes(M_dc, target)        # 服从市场1X2
    if oip_matrix is not None:
        M_oip = tilt_to_outcomes(oip_matrix / oip_matrix.sum(), target)
        M = w_oip * M_oip + (1.0 - w_oip) * M_dc
    else:
        M = M_dc
    M = M / M.sum()
    idx = np.arange(maxg + 1)
    p_h = float(np.tril(M, -1).sum()); p_d = float(np.trace(M)); p_a = float(np.triu(M, 1).sum())
    flat = M.flatten()
    order = np.argsort(-flat)[:5]
    top = [(int(np.unravel_index(k, M.shape)[0]), int(np.unravel_index(k, M.shape)[1]),
            round(float(flat[k]), 4)) for k in order]
    return dict(home=home, away=away, lh=round(lam, 3), la=round(mu, 3),
                rho=round(m["rho"], 4), p_h=round(p_h, 4), p_d=round(p_d, 4), p_a=round(p_a, 4),
                top_scores=top, matrix=M)


if __name__ == "__main__":
    print("加载 IW ...")
    df = load_iw()
    asof = pd.Timestamp("2023-01-01")
    print(f"拟合 DC (as_of={asof}, 训练样本≈{int((df['date']<asof).sum())}) ...")
    model = fit(df, asof)
    print(f"  n_teams={len(model['teams'])}, rho={model['rho']:.4f}, home_adv={model['home_adv']:.3f}, intercept={model['intercept']:.3f}")
    # 演示: 用一场有赔率的比赛
    row = df[df["date"] >= asof].iloc[0]
    res = predict_score_dc(model, row["home_team"], row["away_team"],
                            (row["ch"], row["cd"], row["ca"]), w_oip=0.5)
    print(f"  示例 {row['home_team']} vs {row['away_team']}: H/D/A={res['p_h']}/{res['p_d']}/{res['p_a']} top={res['top_scores']}")
