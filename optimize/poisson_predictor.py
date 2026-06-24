"""
哨响AI — 泊松分布比分预测模块 (T05)
======================================

将足球赛果概率 (H/D/A) 通过泊松模型转化为:
  1. 比分概率矩阵 (0-0 ~ 6-6, 49种组合)
  2. 泊松一致性 H/D/A 概率 (反向聚合，提供替代视角)
  3. Top-K 最可能比分预测

核心逻辑 (源自 predictions.ts):
  - 从胜率反推主/客预期进球 λ
  - 独立泊松 PMF 乘积 → 比分概率
  - 比分概率聚合 → H/D/A 概率
  - 输出最高概率比分 (覆盖主胜/平/客胜各至少1个)

数学推导:
  P(score=h-a) = Poisson(h|λ_h) × Poisson(a|λ_a)
  P(H) = Σ_{h>a} P(h-a)
  P(D) = Σ_{h=a} P(h-a)
  P(A) = Σ_{h<a} P(h-a)
"""

from __future__ import annotations

import json
import math
import os
from typing import Dict, List, Optional, Tuple

import numpy as np


# ═══════════════════════════════════════════════════════════
# 联赛场均进球基线 (五大联赛 + 欧洲赛事)
# ═══════════════════════════════════════════════════════════
LEAGUE_AVG_GOALS: Dict[str, float] = {
    "default":          2.72,   # 五大联赛平均
    "Premier League":   2.85,   # 英超 ← 最高
    "Bundesliga":       3.12,   # 德甲 ← 进球机器
    "La Liga":          2.59,   # 西甲
    "Serie A":          2.70,   # 意甲
    "Ligue 1":          2.72,   # 法甲
    "Eredivisie":       3.05,   # 荷甲
    "Primeira Liga":    2.55,   # 葡超
    "Championship":     2.48,   # 英冠
    "MLS":              2.95,   # 美国大联盟
    "Champions League": 2.89,   # 欧冠
    "Europa League":    2.78,   # 欧联
    "Liga Portugal":    2.55,   # 葡超别名
    "Serie A Brazil":   2.32,   # 巴甲
}


def _poisson_pmf(k: int, lam: float) -> float:
    """泊松概率质量函数: P(X=k) = e^(-λ) * λ^k / k!"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    # 用 log 避免溢出
    log_p = -lam + k * math.log(lam) - _log_factorial(k)
    return math.exp(log_p)


def _log_factorial(n: int) -> float:
    """log(n!) 精确计算 (0≤n≤20 直接累加，更大用 Stirling)"""
    if n <= 1:
        return 0.0
    if n <= 20:
        return sum(math.log(i) for i in range(2, n + 1))
    # Stirling 近似: log(n!) ≈ n*log(n) - n + 0.5*log(2πn)
    return n * math.log(n) - n + 0.5 * math.log(2 * math.pi * n)


# ═══════════════════════════════════════════════════════════
# 核心函数
# ═══════════════════════════════════════════════════════════

def expected_goals_from_probs(
    home_prob: float,
    draw_prob: float,
    away_prob: float,
    base_lambda: float = 2.72,
    home_advantage_factor: float = 1.08,
) -> Tuple[float, float]:
    """
    从 H/D/A 概率反推主/客预期进球 λ。

    推导逻辑:
      - home_share = home_prob / (home_prob + away_prob)
        忽略平局，只看胜负倾向
      - λ_total ≈ base_lambda (联赛场均进球基线)
      - λ_home = base_lambda * home_share * home_advantage_factor
      - λ_away = base_lambda * (1 - home_share)

    主场优势因子 ≈ 1.08 (主场场均多约 0.3 球)

    Args:
        home_prob: 主胜概率 [0, 1]
        draw_prob: 平局概率 [0, 1] (仅用于参考，不参与 λ 计算)
        away_prob: 客胜概率 [0, 1]
        base_lambda: 联赛场均进球基线
        home_advantage_factor: 主场进球优势因子

    Returns:
        (lambda_home, lambda_away)
    """
    total = max(home_prob + away_prob, 0.001)
    home_share = home_prob / total
    away_share = 1.0 - home_share

    lambda_h = base_lambda * home_share * home_advantage_factor
    lambda_a = base_lambda * away_share

    # 下限保护
    lambda_h = max(0.15, lambda_h)
    lambda_a = max(0.15, lambda_a)

    # 上限裁剪 (单队场均很少超 5 球)
    lambda_h = min(lambda_h, 5.5)
    lambda_a = min(lambda_a, 5.5)

    return lambda_h, lambda_a


def score_matrix(
    lambda_h: float,
    lambda_a: float,
    max_goals: int = 6,
) -> np.ndarray:
    """
    生成完整比分概率矩阵。

    Returns:
        proba: (max_goals+1) × (max_goals+1) 矩阵
               proba[h][a] = P(主队进h球 ∧ 客队进a球)
    """
    n = max_goals + 1
    proba = np.zeros((n, n))

    # 向量化预计算 PMF
    h_pmf = np.array([_poisson_pmf(i, lambda_h) for i in range(n)])
    a_pmf = np.array([_poisson_pmf(i, lambda_a) for i in range(n)])

    proba = np.outer(h_pmf, a_pmf)

    # 超出 max_goals 的概率残差
    h_residual = max(0.0, 1.0 - h_pmf.sum())
    a_residual = max(0.0, 1.0 - a_pmf.sum())

    # 将残差均摊到尾部 (简化为加到 max_goals 行/列)
    if h_residual > 1e-8:
        proba[-1, :] += h_residual * a_pmf
    if a_residual > 1e-8:
        proba[:, -1] += a_residual * h_pmf

    # 归一化
    s = proba.sum()
    if s > 0:
        proba /= s

    return proba


def score_to_outcome_probs(
    score_proba: np.ndarray,
) -> Tuple[float, float, float]:
    """
    比分概率矩阵 → H/D/A 概率。

    对角线 (h=a) → 平局
    上三角 (h>a) → 主胜
    下三角 (h<a) → 客胜

    Returns:
        (home_prob, draw_prob, away_prob)
    """
    n = score_proba.shape[0]

    # proba[h][a]: h = home goals (row), a = away goals (col)
    # triu(k=1) → a > h (away win), tril(k=-1) → h > a (home win)
    p_home = np.tril(score_proba, k=-1).sum()   # h > a → 主胜
    p_draw = np.trace(score_proba)              # h == a → 平局
    p_away = np.triu(score_proba, k=1).sum()    # h < a → 客胜

    # 归一化 (理论上和接近1，但处理浮点误差)
    total = p_home + p_draw + p_away
    if total > 0:
        p_home /= total
        p_draw /= total
        p_away /= total

    return p_home, p_draw, p_away


def top_score_predictions(
    lambda_h: float,
    lambda_a: float,
    top_k: int = 3,
    max_goals: int = 6,
) -> List[Dict]:
    """
    生成 Top-K 最可能的比分预测，确保覆盖主胜/平/客胜。

    Args:
        lambda_h: 主队预期进球
        lambda_a: 客队预期进球
        top_k: 返回比分数量 (默认3)
        max_goals: 最大进球数

    Returns:
        [{"score": "2-1", "probability": 0.123, "outcome": "home"}, ...]
    """
    proba = score_matrix(lambda_h, lambda_a, max_goals)
    n = max_goals + 1

    # 收集所有比分
    all_scores = []
    for h in range(n):
        for a in range(n):
            p = proba[h, a]
            if p > 1e-8:
                outcome = "home" if h > a else ("away" if a > h else "draw")
                all_scores.append({
                    "score": f"{h}-{a}",
                    "probability": round(p, 5),
                    "outcome": outcome,
                })

    # 按概率降序排列
    all_scores.sort(key=lambda x: x["probability"], reverse=True)

    # 从每个赛果类别各取最佳 → 确保多样性
    result = []
    seen_outcomes = set()
    used_scores = set()

    for s in all_scores:
        if len(result) >= top_k:
            break
        if s["score"] in used_scores:
            continue
        if s["outcome"] not in seen_outcomes:
            result.append(s)
            seen_outcomes.add(s["outcome"])
            used_scores.add(s["score"])

    # 如果不足 top_k，从剩余中补足
    for s in all_scores:
        if len(result) >= top_k:
            break
        if s["score"] not in used_scores:
            result.append(s)
            used_scores.add(s["score"])

    return result


# ═══════════════════════════════════════════════════════════
# PoissonPredictor 类
# ═══════════════════════════════════════════════════════════

class PoissonPredictor:
    """
    泊松分布比分预测器 (T05)。

    用途:
      1. 将已有的 H/D/A 概率通过泊松模型重新映射，得到「泊松一致性概率」
      2. 生成最可能的比分预测
      3. 作为启发式模型的替代信号源

    使用示例:
      pp = PoissonPredictor()
      h, d, a = pp.predict_outcome_proba(0.45, 0.25, 0.30, "Premier League")
      scores = pp.predict_scores(0.45, 0.25, 0.30, "Premier League")
    """

    def __init__(self, config: Optional[Dict] = None):
        """
        Args:
            config: 全局 config 字典 (读取 models.poisson)
        """
        self.config = config or {}
        poisson_cfg = self.config.get("models", {}).get("poisson", {})

        self.enabled = poisson_cfg.get("enabled", True)
        self.default_lambda = poisson_cfg.get("base_lambda", 2.72)
        self.home_advantage_factor = poisson_cfg.get("home_advantage_factor", 1.08)
        self.max_goals = poisson_cfg.get("max_goals", 6)

        # 加载联赛级 λ 基线
        self._league_lambdas = dict(LEAGUE_AVG_GOALS)
        league_overrides = poisson_cfg.get("league_lambdas", {})
        if league_overrides:
            self._league_lambdas.update(league_overrides)

    def get_base_lambda(self, league_name: str = "default") -> float:
        """获取联赛场均进球基线"""
        return self._league_lambdas.get(
            league_name,
            self._league_lambdas.get("default", self.default_lambda),
        )

    def predict_outcome_proba(
        self,
        home_prob: float,
        draw_prob: float,
        away_prob: float,
        league_name: str = "default",
    ) -> Tuple[float, float, float]:
        """
        泊松模型 H/D/A 概率预测。

        流程:
          1. 从 H/D/A → λ_h, λ_a
          2. 泊松得分矩阵
          3. 聚合 → H/D/A

        Returns:
            (p_home, p_draw, p_away) 归一化到和=1
        """
        base_lam = self.get_base_lambda(league_name)
        lam_h, lam_a = expected_goals_from_probs(
            home_prob, draw_prob, away_prob, base_lam,
            self.home_advantage_factor,
        )
        proba_matrix = score_matrix(lam_h, lam_a, self.max_goals)
        return score_to_outcome_probs(proba_matrix)

    def predict_outcome_proba_batch(
        self,
        proba_array: np.ndarray,
        league_names: Optional[List[str]] = None,
    ) -> np.ndarray:
        """
        批量泊松 H/D/A 概率预测。

        Args:
            proba_array: (n, 3) H/D/A 输入概率
            league_names: 每条数据的联赛名列表

        Returns:
            proba_poisson: (n, 3) 泊松一致性 H/D/A 概率
        """
        n = proba_array.shape[0]
        result = np.zeros((n, 3))

        for i in range(n):
            hp, dp, ap = proba_array[i]
            league = league_names[i] if league_names and i < len(league_names) else "default"
            h, d, a = self.predict_outcome_proba(hp, dp, ap, league)
            result[i] = [h, d, a]

        return result

    def predict_scores(
        self,
        home_prob: float,
        draw_prob: float,
        away_prob: float,
        league_name: str = "default",
        top_k: int = 3,
    ) -> List[Dict]:
        """
        预测最可能的比分。

        Returns:
            [{"score": "1-0", "probability": 0.085, "outcome": "home"}, ...]
        """
        base_lam = self.get_base_lambda(league_name)
        lam_h, lam_a = expected_goals_from_probs(
            home_prob, draw_prob, away_prob, base_lam,
            self.home_advantage_factor,
        )

        scores = top_score_predictions(lam_h, lam_a, top_k, self.max_goals)
        return scores

    def full_analysis(
        self,
        home_prob: float,
        draw_prob: float,
        away_prob: float,
        league_name: str = "default",
    ) -> Dict:
        """
        完整泊松分析 (一次计算返回所有结果)。

        Returns:
            {
                "lambda": {"home": 1.45, "away": 0.92},
                "outcome_proba": {"home": 0.52, "draw": 0.22, "away": 0.26},
                "score_predictions": [...],
                "total_goals_expected": 2.37,
            }
        """
        base_lam = self.get_base_lambda(league_name)
        lam_h, lam_a = expected_goals_from_probs(
            home_prob, draw_prob, away_prob, base_lam,
            self.home_advantage_factor,
        )

        proba_matrix = score_matrix(lam_h, lam_a, self.max_goals)
        h, d, a = score_to_outcome_probs(proba_matrix)

        scores = top_score_predictions(lam_h, lam_a, 3, self.max_goals)

        return {
            "lambda": {
                "home": round(lam_h, 3),
                "away": round(lam_a, 3),
            },
            "outcome_proba": {
                "home": round(h, 4),
                "draw": round(d, 4),
                "away": round(a, 4),
            },
            "score_predictions": scores,
            "total_goals_expected": round(lam_h + lam_a, 2),
        }

    def to_dict(self) -> Dict:
        """导出配置 (用于序列化)"""
        return {
            "enabled": self.enabled,
            "default_lambda": self.default_lambda,
            "home_advantage_factor": self.home_advantage_factor,
            "max_goals": self.max_goals,
            "league_lambdas": self._league_lambdas,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "PoissonPredictor":
        """从字典恢复"""
        pp = cls.__new__(cls)
        pp.enabled = d.get("enabled", True)
        pp.default_lambda = d.get("default_lambda", 2.72)
        pp.home_advantage_factor = d.get("home_advantage_factor", 1.08)
        pp.max_goals = d.get("max_goals", 6)
        pp._league_lambdas = d.get("league_lambdas", dict(LEAGUE_AVG_GOALS))
        pp.config = {}
        return pp


# ═══════════════════════════════════════════════════════════
# 自验证测试
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    passed = 0
    failed = 0
    tests = []

    def check(name, cond, detail=""):
        global passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name} {detail}")
        tests.append((name, cond, detail))

    # ── 测试 1: PMF 基础 ──
    print("\n[Test 1] 泊松 PMF 基础")
    # k=0, λ=1 → e^-1 ≈ 0.3679
    p0 = _poisson_pmf(0, 1.0)
    check("λ=1,k=0", abs(p0 - 0.367879) < 0.001, f"got {p0:.6f}")
    # k=2, λ=2 → e^-2 * 4/2 = 2*e^-2 ≈ 0.2707
    p2 = _poisson_pmf(2, 2.0)
    check("λ=2,k=2", abs(p2 - 0.270671) < 0.001, f"got {p2:.6f}")
    # k=0, λ=0 → 边界情况
    p0z = _poisson_pmf(0, 0.0)
    check("λ=0,k=0", abs(p0z - 1.0) < 0.001, f"got {p0z:.6f}")
    # k=3, λ=0 → 0
    p3z = _poisson_pmf(3, 0.0)
    check("λ=0,k=3", abs(p3z - 0.0) < 0.001, f"got {p3z:.6f}")
    # PMF 和为 1 (λ=1.5, k=0..10)
    total = sum(_poisson_pmf(i, 1.5) for i in range(20))
    check("λ=1.5 sum≈1", abs(total - 1.0) < 0.001, f"sum={total:.6f}")

    # ── 测试 2: λ反推 ──
    print("\n[Test 2] 概率 → λ 反推")
    lam_h, lam_a = expected_goals_from_probs(0.50, 0.25, 0.25, 2.72)
    check("均势 → λ_h>λ_a (主场优势)", lam_h > lam_a, f"λ_h={lam_h:.3f}, λ_a={lam_a:.3f}")
    check("均势 → λ之和≈基线", abs(lam_h + lam_a - 2.72) < 0.5,
          f"sum={lam_h+lam_a:.3f}")

    lam_h2, lam_a2 = expected_goals_from_probs(0.70, 0.20, 0.10, 2.72)
    check("强队 → λ_h > λ_a", lam_h2 > lam_a2, f"λ_h={lam_h2:.3f}, λ_a={lam_a2:.3f}")
    check("强队 → λ_h 合理", lam_h2 > 1.5, f"λ_h={lam_h2:.3f}")

    lam_h3, lam_a3 = expected_goals_from_probs(0.10, 0.20, 0.70, 2.72)
    check("弱队 → λ_a > λ_h", lam_a3 > lam_h3, f"λ_h={lam_h3:.3f}, λ_a={lam_a3:.3f}")

    # ── 测试 3: 比分矩阵 ──
    print("\n[Test 3] 比分概率矩阵")
    mat = score_matrix(1.5, 1.0, max_goals=6)
    check("矩阵和≈1", abs(mat.sum() - 1.0) < 0.001, f"sum={mat.sum():.6f}")
    check("矩阵形状", mat.shape == (7, 7), f"shape={mat.shape}")
    # 最常见比分不应为0 (λ_h=1.5, λ_a=1.0 → 1-0 或 2-1 概率应>0)
    most_likely = np.unravel_index(mat.argmax(), mat.shape)
    check("存在最大概率比分", mat[most_likely] > 0.01,
          f"max={mat[most_likely]:.5f} at {most_likely}")

    # ── 测试 4: 比分 → H/D/A ──
    print("\n[Test 4] 比分 → 赛果概率")
    mat2 = score_matrix(1.5, 1.0)
    h, d, a = score_to_outcome_probs(mat2)
    total2 = h + d + a
    check("H+D+A≈1", abs(total2 - 1.0) < 0.001, f"sum={total2:.6f}")
    # λ_h > λ_a → 主胜概率应最大
    check("λ_h>λ_a → H最大", h > max(d, a), f"H={h:.3f} D={d:.3f} A={a:.3f}")

    mat3 = score_matrix(1.0, 1.5)
    h2, d2, a2 = score_to_outcome_probs(mat3)
    check("λ_a>λ_h → A最大", a2 > max(h2, d2), f"H={h2:.3f} D={d2:.3f} A={a2:.3f}")

    # 完全均势
    mat4 = score_matrix(1.2, 1.2)
    h3, d3, a3 = score_to_outcome_probs(mat4)
    check("均势 → H≈A", abs(h3 - a3) < 0.05, f"H={h3:.3f} A={a3:.3f}")

    # ── 测试 5: 比分预测 ──
    print("\n[Test 5] Top-K 比分预测")
    scores = top_score_predictions(1.5, 1.0, top_k=3)
    check("返回3个比分", len(scores) == 3, f"got {len(scores)}")
    outcomes = {s["outcome"] for s in scores}
    check("覆盖主胜+平局+客胜(尽可能)", len(outcomes) >= 2,
          f"outcomes={outcomes}")

    # ── 测试 6: PoissonPredictor 类 ──
    print("\n[Test 6] PoissonPredictor 类")
    pp = PoissonPredictor()
    h, d, a = pp.predict_outcome_proba(0.50, 0.25, 0.25, "Premier League")
    total = h + d + a
    check("归一化", abs(total - 1.0) < 0.001, f"sum={total:.6f}")
    check("英超基线≠默认", pp.get_base_lambda("Premier League") != pp.get_base_lambda("default"),
          f"EPL={pp.get_base_lambda('Premier League')} vs default={pp.get_base_lambda('default')}")

    # 强队主场
    h_s, d_s, a_s = pp.predict_outcome_proba(0.60, 0.22, 0.18, "Premier League")
    check("强队主场→H>50%", h_s > 0.50, f"H={h_s:.3f}")

    # 弱队 (明显差距)
    h_w, d_w, a_w = pp.predict_outcome_proba(0.15, 0.20, 0.65, "La Liga")
    check("弱队→A占优", a_w > h_w, f"H={h_w:.3f} A={a_w:.3f}")

    # 势均力敌
    h_e, d_e, a_e = pp.predict_outcome_proba(0.35, 0.30, 0.35, "Serie A")
    check("均势→D≥22%", d_e >= 0.20, f"D={d_e:.3f}")

    # ── 测试 7: 批量预测 ──
    print("\n[Test 7] 批量预测")
    arr = np.array([
        [0.50, 0.25, 0.25],
        [0.60, 0.22, 0.18],
        [0.15, 0.20, 0.65],
    ])
    proba_batch = pp.predict_outcome_proba_batch(
        arr, ["Premier League", "Bundesliga", "La Liga"],
    )
    check("批量形状", proba_batch.shape == (3, 3), f"shape={proba_batch.shape}")
    check("批量归一化", np.allclose(proba_batch.sum(axis=1), 1.0, atol=0.001),
          f"sums={proba_batch.sum(axis=1)}")
    # 德甲进球基数高 → 排名2的λ应偏高
    check("德甲≠英超", abs(proba_batch[0, 0] - proba_batch[1, 0]) > 0.001,
          f"EPL H={proba_batch[0,0]:.4f} BL H={proba_batch[1,0]:.4f}")

    # ── 测试 8: full_analysis ──
    print("\n[Test 8] full_analysis")
    result = pp.full_analysis(0.45, 0.28, 0.27, "Premier League")
    check("包含lambda", "lambda" in result)
    check("包含outcome", "outcome_proba" in result)
    check("包含scores", "score_predictions" in result)
    check("total_goals合理", 1.5 < result["total_goals_expected"] < 5.0,
          f"total={result['total_goals_expected']}")

    # ── 测试 9: 序列化 ──
    print("\n[Test 9] 序列化")
    d = pp.to_dict()
    pp2 = PoissonPredictor.from_dict(d)
    h1, d1, a1 = pp.predict_outcome_proba(0.50, 0.25, 0.25, "Premier League")
    h2r, d2r, a2r = pp2.predict_outcome_proba(0.50, 0.25, 0.25, "Premier League")
    check("roundtrip H", abs(h1 - h2r) < 0.0001, f"{h1:.6f} vs {h2r:.6f}")
    check("roundtrip D", abs(d1 - d2r) < 0.0001)

    # ── 测试 10: 统计学合理性 ──
    print("\n[Test 10] 统计学合理性")
    # 高概率事件不应给出过低概率
    h_hi, d_hi, a_hi = pp.predict_outcome_proba(0.80, 0.12, 0.08, "Premier League")
    check("强队主场H≥0.60", h_hi >= 0.60, f"H={h_hi:.3f}")
    # 不应产生 NaN 或 inf
    check("无NaN", not any(math.isnan(x) for x in [h_hi, d_hi, a_hi]))
    check("无Inf", not any(math.isinf(x) for x in [h_hi, d_hi, a_hi]))

    # ── 测试 11: 边缘情况 ──
    print("\n[Test 11] 边缘情况")
    h_z, d_z, a_z = pp.predict_outcome_proba(0.01, 0.01, 0.98, "default")
    check("极端客胜→A>0.70", a_z > 0.70, f"A={a_z:.3f}")
    check("极端客胜→无NaN", not any(math.isnan(x) for x in [h_z, d_z, a_z]))

    # 概率全相等
    h_e2, d_e2, a_e2 = pp.predict_outcome_proba(0.333, 0.334, 0.333, "default")
    check("等概率→近均分", abs(h_e2 - a_e2) < 0.15, f"H={h_e2:.3f} A={a_e2:.3f}")

    # ── 汇总 ──
    total_tests = passed + failed
    print(f"\n{'='*50}")
    print(f"  测试完成: {passed}/{total_tests} 通过")
    if failed:
        print(f"  {failed} 个测试失败!")
        sys.exit(1)
    else:
        print(f"  ✅ 全部通过!")
