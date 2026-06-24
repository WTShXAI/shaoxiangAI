"""
╔══════════════════════════════════════════════════════════════╗
║  T04 — ELO 评级系统 (Elo Rating System)                      ║
║  v1.0 — 2026-06-01                                           ║
║                                                              ║
║  功能:                                                       ║
║  1. 基础 ELO 计算 (K=32, 主场+100)                            ║
║  2. K 因子动态调整 (联赛/比分差/赛季阶段)                       ║
║  3. 评级 → 胜平负概率映射 (含平局模型)                         ║
║  4. 批量历史回测 + 序列化保存                                  ║
╚══════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("elo_ratings")


# ═══════════════════════════════════════════════
# 联赛特征 profile — 用于 K 因子 & 主场优势调整
# ═══════════════════════════════════════════════

LEAGUE_PROFILES: Dict[str, Dict[str, float]] = {
    # ── 五大联赛 ──
    "Premier League": {
        "importance": 1.0,       # 联赛重要度系数
        "home_advantage": 100,   # ELO 主场加分
        "draw_rate": 0.252,      # 历史平局率
        "avg_goals": 2.85,       # 场均进球
    },
    "La Liga": {
        "importance": 0.98,
        "home_advantage": 105,
        "draw_rate": 0.258,
        "avg_goals": 2.63,
    },
    "Serie A": {
        "importance": 0.95,
        "home_advantage": 110,
        "draw_rate": 0.263,
        "avg_goals": 2.78,
    },
    "Bundesliga": {
        "importance": 0.95,
        "home_advantage": 105,
        "draw_rate": 0.244,
        "avg_goals": 3.12,
    },
    "Ligue 1": {
        "importance": 0.90,
        "home_advantage": 95,
        "draw_rate": 0.282,
        "avg_goals": 2.72,
    },
    # ── 欧洲赛事 ──
    "UEFA Champions League": {
        "importance": 1.20,
        "home_advantage": 80,
        "draw_rate": 0.235,
        "avg_goals": 2.89,
    },
    "UEFA Europa League": {
        "importance": 1.00,
        "home_advantage": 80,
        "draw_rate": 0.248,
        "avg_goals": 2.71,
    },
    # ── 次级联赛 ──
    "Championship": {
        "importance": 0.85,
        "home_advantage": 100,
        "draw_rate": 0.272,
        "avg_goals": 2.56,
    },
    "Serie B": {
        "importance": 0.80,
        "home_advantage": 105,
        "draw_rate": 0.295,
        "avg_goals": 2.38,
    },
    "Ligue 2": {
        "importance": 0.80,
        "home_advantage": 95,
        "draw_rate": 0.302,
        "avg_goals": 2.35,
    },
    "2. Bundesliga": {
        "importance": 0.82,
        "home_advantage": 100,
        "draw_rate": 0.278,
        "avg_goals": 2.82,
    },
    "Segunda Division": {
        "importance": 0.80,
        "home_advantage": 100,
        "draw_rate": 0.290,
        "avg_goals": 2.22,
    },
    # ── 其他联赛 ──
    "Eredivisie": {
        "importance": 0.85,
        "home_advantage": 105,
        "draw_rate": 0.240,
        "avg_goals": 3.15,
    },
    "Primeira Liga": {
        "importance": 0.82,
        "home_advantage": 108,
        "draw_rate": 0.255,
        "avg_goals": 2.52,
    },
    "Brasileirão": {
        "importance": 0.88,
        "home_advantage": 115,
        "draw_rate": 0.270,
        "avg_goals": 2.42,
    },
    "MLS": {
        "importance": 0.80,
        "home_advantage": 110,
        "draw_rate": 0.258,
        "avg_goals": 2.98,
    },
}

_DEFAULT_PROFILE = {
    "importance": 0.85,
    "home_advantage": 100,
    "draw_rate": 0.265,
    "avg_goals": 2.55,
}


def _get_profile(league_name: str) -> Dict[str, float]:
    """获取联赛 profile，未知联赛返回默认值"""
    return LEAGUE_PROFILES.get(league_name, _DEFAULT_PROFILE)


# ═══════════════════════════════════════════════
# 核心类
# ═══════════════════════════════════════════════


class EloRatingSystem:
    """
    足球 ELO 评级系统。

    核心参数 (可从 config.yaml 覆盖):
    - k_base: 基础 K 因子 (默认 32)
    - home_advantage: ELO 主场加分 (默认 100)
    - init_rating: 新球队初始评分 (默认 1500)
    - scale: 评分差缩放因子 (默认 400)
    """

    def __init__(
        self,
        k_base: float = 32.0,
        home_advantage: float = 100.0,
        init_rating: float = 1500.0,
        scale: float = 400.0,
        dynamic_k: bool = True,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.k_base = k_base
        self.home_advantage = home_advantage
        self.init_rating = init_rating
        self.scale = scale
        self.dynamic_k = dynamic_k
        self.config = config or {}

        # 评级存储
        self.ratings: Dict[str, float] = defaultdict(lambda: init_rating)
        # 评级历史: {team: [(date, rating), ...]}
        self.rating_history: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        # 最近一次更新日期
        self._last_update: Optional[str] = None

        # 从 config 覆盖参数
        if config:
            elo_cfg = config.get("models", {}).get("elo", {})
            self.k_base = elo_cfg.get("k_base", self.k_base)
            self.home_advantage = elo_cfg.get("home_advantage", self.home_advantage)
            self.init_rating = elo_cfg.get("init_rating", self.init_rating)
            self.scale = elo_cfg.get("scale", self.scale)
            self.dynamic_k = elo_cfg.get("dynamic_k", self.dynamic_k)

    # ─────────────────────────────────────────────
    # 1. 基础 ELO 计算
    # ─────────────────────────────────────────────

    def expected_score(self, rating_a: float, rating_b: float) -> float:
        """
        A 对阵 B 的预期得分 (0-1)。
        E_A = 1 / (1 + 10^((R_B - R_A) / scale))
        """
        return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / self.scale))

    def expected_match_score(
        self, home_rating: float, away_rating: float, league_name: str = "default"
    ) -> Tuple[float, float]:
        """
        返回 (主队预期得分, 客队预期得分)。
        主队评分加主场优势后计算。
        """
        profile = _get_profile(league_name)
        ha = profile["home_advantage"] if league_name != "default" else self.home_advantage
        home_adj = home_rating + ha
        e_home = self.expected_score(home_adj, away_rating)
        e_away = 1.0 - e_home
        return e_home, e_away

    def update_match(
        self,
        home_rating: float,
        away_rating: float,
        home_score: float,   # 1=主胜, 0.5=平, 0=主负
        k_home: float = 32.0,
        k_away: float = 32.0,
        league_name: str = "default",
    ) -> Tuple[float, float]:
        """
        单场比赛更新两队评分 (主场优势一致的版本)。

        使用主场评分 + 主场优势 统一计算预期得分，
        保证双方更新的 E 值互为 1-E。

        Returns:
            (new_home_rating, new_away_rating)
        """
        profile = _get_profile(league_name)
        ha = profile["home_advantage"] if league_name != "default" else self.home_advantage

        # 统一预期得分: 主场评分含优势
        home_adj = home_rating + ha
        e_home = self.expected_score(home_adj, away_rating)
        e_away = 1.0 - e_home

        new_home = home_rating + k_home * (home_score - e_home)
        new_away = away_rating + k_away * ((1.0 - home_score) - e_away)

        return new_home, new_away

    # ─────────────────────────────────────────────
    # 2. K 因子动态调整
    # ─────────────────────────────────────────────

    def compute_k_factor(
        self,
        league_name: str = "default",
        goal_diff: int = 1,
        match_stage: Optional[int] = None,
        total_matchdays: int = 38,
        is_home: bool = True,
    ) -> float:
        """
        动态 K 因子 = k_base × k_league × k_GD × k_stage

        参数:
            league_name: 联赛名 (查 profile.importance)
            goal_diff: 净胜球绝对值
            match_stage: 当前比赛轮次 (None → 按中段处理)
            total_matchdays: 赛季总轮数
            is_home: 是否主队
        """
        if not self.dynamic_k:
            return self.k_base

        profile = _get_profile(league_name)

        # 联赛重要度乘数 (0.75~1.25)
        k_league = np.clip(profile["importance"], 0.75, 1.25)

        # 比分差乘数
        if goal_diff >= 4:
            k_gd = 1.75
        elif goal_diff >= 3:
            k_gd = 1.50
        elif goal_diff >= 2:
            k_gd = 1.25
        else:
            k_gd = 1.00

        # 赛季阶段乘数 (早期↑ → 更多探索, 末期↓ → 更稳定)
        if match_stage is not None and total_matchdays > 0:
            stage_ratio = match_stage / total_matchdays
            if stage_ratio < 0.25:       # 前25%：探索期
                k_stage = 1.10
            elif stage_ratio < 0.50:     # 25-50%
                k_stage = 1.05
            elif stage_ratio < 0.75:     # 50-75%：稳定期
                k_stage = 1.00
            elif stage_ratio < 0.90:     # 75-90%：关键期
                k_stage = 1.05
            else:                         # 末10%：冲刺期，高波动
                k_stage = 1.10
        else:
            k_stage = 1.00

        # 主客场微调 (客场赢球信息量更大)
        k_home_away = 0.95 if is_home else 1.05

        k = self.k_base * k_league * k_gd * k_stage * k_home_away
        # 裁剪到合理范围
        k = np.clip(k, self.k_base * 0.5, self.k_base * 2.5)

        return k

    # ─────────────────────────────────────────────
    # 3. ELO 评级 → 胜平负概率
    # ─────────────────────────────────────────────

    def rating_diff_to_win_prob(self, rating_diff: float) -> float:
        """
        评分差 → 主队胜率 (sigmoid)。

        rating_diff = home_rating - away_rating (已含主场优势)
        """
        return 1.0 / (1.0 + np.exp(-rating_diff / (self.scale * 0.95)))

    def rating_diff_to_draw_prob(
        self, rating_diff: float, league_name: str = "default"
    ) -> float:
        """
        评分差 → 平局概率 (高斯核模型)。

        差距越小 → 平局概率越高 (峰值在 rating_diff=0)。
        平局基准率从联赛 profile 读取。
        """
        profile = _get_profile(league_name)
        base_draw = profile["draw_rate"]

        # 高斯衰减: 评分差拉大 → 平局概率快速下降
        sigma = self.scale * 0.85
        draw_factor = np.exp(-(rating_diff ** 2) / (2 * sigma ** 2))

        # 范围: [base_draw*0.3, base_draw*1.15]
        draw_prob = base_draw * (0.30 + 0.85 * draw_factor)
        draw_prob = np.clip(draw_prob, 0.08, 0.38)

        return float(draw_prob)

    def win_draw_loss_proba(
        self,
        home_rating: float,
        away_rating: float,
        league_name: str = "default",
    ) -> Tuple[float, float, float]:
        """
        ELO 评级 → 胜平负概率。

        算法:
        1. 从评分差计算主胜概率 (sigmoid)
        2. 从评分差计算平局概率 (高斯衰减)
        3. 归一化: (home, draw, away) 和为 1

        Returns:
            (home_prob, draw_prob, away_prob)
        """
        profile = _get_profile(league_name)
        ha = profile["home_advantage"] if league_name != "default" else self.home_advantage

        # 含主场优势的评分差
        rating_diff = (home_rating + ha) - away_rating

        # 原始胜率
        raw_home = self.rating_diff_to_win_prob(rating_diff)
        draw_prob = self.rating_diff_to_draw_prob(rating_diff, league_name)

        # 从剩余概率中分配给主/客
        residual = 1.0 - draw_prob
        if residual < 0.02:
            residual = 0.02
            draw_prob = 0.98

        home_prob = raw_home * residual
        away_prob = (1.0 - raw_home) * residual

        # 归一化
        total = home_prob + draw_prob + away_prob
        if total > 0:
            home_prob /= total
            draw_prob /= total
            away_prob /= total
        else:
            home_prob = draw_prob = away_prob = 1.0 / 3.0

        # 硬保护
        home_prob = max(home_prob, 0.02)
        draw_prob = max(draw_prob, 0.02)
        away_prob = max(away_prob, 0.02)
        total = home_prob + draw_prob + away_prob
        return (home_prob / total, draw_prob / total, away_prob / total)

    def win_draw_loss_proba_batch(
        self,
        home_ratings_arr: np.ndarray,
        away_ratings_arr: np.ndarray,
        league_name: str = "default",
    ) -> np.ndarray:
        """
        批量计算胜平负概率。

        Args:
            home_ratings_arr: (n,) 主队评分
            away_ratings_arr: (n,) 客队评分
            league_name: 联赛名

        Returns:
            proba: (n, 3) H/D/A 概率矩阵
        """
        profile = _get_profile(league_name)
        ha = profile["home_advantage"] if league_name != "default" else self.home_advantage

        rating_diff = (home_ratings_arr + ha) - away_ratings_arr

        n = len(rating_diff)
        proba = np.zeros((n, 3))

        raw_home = 1.0 / (1.0 + np.exp(-rating_diff / (self.scale * 0.95)))

        # 平局 (向量化)
        sigma = self.scale * 0.85
        draw_factor = np.exp(-(rating_diff ** 2) / (2 * sigma ** 2))
        base_draw = profile["draw_rate"]
        draw_prob = base_draw * (0.30 + 0.85 * draw_factor)
        draw_prob = np.clip(draw_prob, 0.08, 0.38)

        residual = 1.0 - draw_prob
        residual = np.clip(residual, 0.02, None)

        proba[:, 0] = raw_home * residual
        proba[:, 1] = draw_prob
        proba[:, 2] = (1.0 - raw_home) * residual

        # 归一化
        totals = proba.sum(axis=1, keepdims=True)
        proba = proba / totals

        proba = np.clip(proba, 0.02, None)
        totals = proba.sum(axis=1, keepdims=True)
        proba = proba / totals

        return proba

    # ─────────────────────────────────────────────
    # 4. 批量历史计算
    # ─────────────────────────────────────────────

    def compute_ratings_from_history(
        self,
        matches: List[Dict[str, Any]],
        league_name: str = "default",
        reset: bool = True,
    ) -> float:
        """
        从历史比赛列表批量计算 ELO 评分。

        每次调用按 match_date 升序处理，模拟真实时间线。

        Args:
            matches: [{
                "home_team_name": str,
                "away_team_name": str,
                "home_score": int,
                "away_score": int,
                "match_date": str ("YYYY-MM-DD"),
                "matchday": int,          # 可选(赛季轮次)
                "season": str,            # 可选
            }, ...]
            league_name: 联赛名 (用于 K 因子 & 主场优势)
            reset: 是否重置现有评分

        Returns:
            _volatility: 最终评分标准差 (反映评级区分度)
        """
        if reset:
            self.ratings.clear()
            self.ratings = defaultdict(lambda: self.init_rating)
            self.rating_history.clear()

        total_matchdays = self._guess_total_matchdays(matches)

        sorted_matches = sorted(matches, key=lambda m: str(m.get("match_date", "")))

        for m in sorted_matches:
            home = m.get("home_team_name", "")
            away = m.get("away_team_name", "")
            if not home or not away:
                continue

            hs = m.get("home_score")
            aws = m.get("away_score")
            if hs is None or aws is None:
                continue

            try:
                hs = int(hs)
                aws = int(aws)
            except (ValueError, TypeError):
                continue

            matchday = m.get("matchday")
            match_date = str(m.get("match_date", ""))

            rh = self.ratings[home]
            ra = self.ratings[away]

            # 记录赛前评分
            self.rating_history[home].append((match_date, rh))
            self.rating_history[away].append((match_date, ra))

            # 实际结果
            if hs > aws:
                score_h = 1.0
            elif hs < aws:
                score_h = 0.0
            else:
                score_h = 0.5

            # 动态 K 因子
            goal_diff = abs(hs - aws)
            if self.dynamic_k:
                stage = int(matchday) if matchday else None
                k_h = self.compute_k_factor(
                    league_name, goal_diff, stage,
                    total_matchdays, is_home=True,
                )
                k_a = self.compute_k_factor(
                    league_name, goal_diff, stage,
                    total_matchdays, is_home=False,
                )
            else:
                k_h = k_a = self.k_base

            # 统一更新 (主场优势一致的 E 值)
            new_rh, new_ra = self.update_match(
                rh, ra, score_h, k_h, k_a, league_name,
            )
            self.ratings[home] = new_rh
            self.ratings[away] = new_ra

        self._last_update = sorted_matches[-1].get("match_date", "") if sorted_matches else None

        ratings_arr = np.array(list(self.ratings.values()))
        volatility = float(np.std(ratings_arr))

        logger.info(
            f"ELO 计算完成: {len(self.ratings)} 支球队, "
            f"均值={np.mean(ratings_arr):.0f}, "
            f"标准差={volatility:.1f}, "
            f"范围=[{np.min(ratings_arr):.0f}, {np.max(ratings_arr):.0f}]"
        )

        return volatility

    @staticmethod
    def _guess_total_matchdays(matches: List[Dict]) -> int:
        """从 matchday 字段估算赛季总轮数"""
        matchdays = set()
        for m in matches:
            md = m.get("matchday")
            if md:
                try:
                    matchdays.add(int(md))
                except (ValueError, TypeError):
                    pass
        if matchdays:
            return max(matchdays)
        return 38  # 默认

    # ─────────────────────────────────────────────
    # 5. 查询接口
    # ─────────────────────────────────────────────

    def get_rating(self, team_name: str) -> float:
        """获取球队当前 ELO 评分"""
        return self.ratings.get(team_name, self.init_rating)

    def get_rating_before_date(self, team_name: str, match_date: str) -> float:
        """获取球队在某日期前的最近评分 (用于防偷窥)"""
        history = self.rating_history.get(team_name, [])
        best_rating = self.init_rating
        for date, rating in history:
            if date < match_date:
                best_rating = rating
            else:
                break
        return best_rating

    def get_top_teams(self, n: int = 10) -> List[Tuple[str, float]]:
        """获取评分最高的 N 支球队"""
        sorted_teams = sorted(self.ratings.items(), key=lambda x: x[1], reverse=True)
        return sorted_teams[:n]

    def get_ratings_dict(self) -> Dict[str, float]:
        """获取所有球队评分 dict"""
        return dict(self.ratings)

    # ─────────────────────────────────────────────
    # 6. 持久化
    # ─────────────────────────────────────────────

    def save(self, filepath: str):
        """将 ELO 评分保存到 JSON 文件"""
        data = {
            "version": "1.0",
            "last_update": self._last_update,
            "k_base": self.k_base,
            "init_rating": self.init_rating,
            "scale": self.scale,
            "n_teams": len(self.ratings),
            "ratings": dict(self.ratings),
        }
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"ELO 评分已保存: {filepath} ({len(self.ratings)} 队)")

    @classmethod
    def load(cls, filepath: str) -> "EloRatingSystem":
        """从 JSON 文件加载 ELO 评分"""
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        instance = cls(
            k_base=data.get("k_base", 32.0),
            init_rating=data.get("init_rating", 1500.0),
            scale=data.get("scale", 400.0),
        )
        instance.ratings = defaultdict(lambda: instance.init_rating, data.get("ratings", {}))
        instance._last_update = data.get("last_update")
        logger.info(f"ELO 评分已加载: {filepath} ({len(instance.ratings)} 队)")
        return instance


# ═══════════════════════════════════════════════
# 便捷工厂
# ═══════════════════════════════════════════════

_global_elo: Optional[EloRatingSystem] = None


def get_elo_system(config: Optional[Dict[str, Any]] = None) -> EloRatingSystem:
    """获取全局 ELO 实例 (懒加载单例)"""
    global _global_elo
    if _global_elo is None:
        _global_elo = EloRatingSystem(config=config)
    return _global_elo


def reset_global_elo():
    """重置全局 ELO 实例"""
    global _global_elo
    _global_elo = None


# ═══════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    print("=" * 60)
    print("  T04 ELO 评级系统 — 自测")
    print("=" * 60)

    elo = EloRatingSystem(k_base=32, home_advantage=100)
    league = "Premier League"

    # ── 测试 1: 基础胜率 ──
    print("\n[Test 1] 基础 ELO 预期得分")
    e = elo.expected_score(1600, 1500)
    print(f"  1600 vs 1500: E= {e:.4f} (expect ~0.64)")
    e2 = elo.expected_score(1500, 1600)
    print(f"  1500 vs 1600: E= {e2:.4f} (expect ~0.36)")
    assert abs(e + e2 - 1.0) < 0.001, "对称性失败!"

    # ── 测试 2: 主场优势 ──
    print("\n[Test 2] 主场优势 (ELO +100)")
    e_home, e_away = elo.expected_match_score(1500, 1500, league)
    print(f"  1500(H) vs 1500(A): E_home={e_home:.4f}, E_away={e_away:.4f}")
    assert e_home > 0.60, f"主场优势太低: {e_home:.3f}"

    # ── 测试 3: 基本更新 ──
    print("\n[Test 3] 评分更新")
    r_new_h, r_new_a = elo.update_match(1500, 1500, 1.0, 32, 32, league)
    print(f"  主胜: {1500}→{r_new_h:.1f}  {1500}→{r_new_a:.1f}")
    assert r_new_h > 1500 > r_new_a, "主胜后主队评分应上升"

    r_d_h, r_d_a = elo.update_match(1500, 1500, 0.5, 32, 32, league)
    print(f"  平局: {1500}→{r_d_h:.1f}  {1500}→{r_d_a:.1f}")
    assert r_d_h < 1500 < r_d_a, "平局后主场评分应下降(预期胜率高)"

    # ── 测试 4: 动态 K 因子 ──
    print("\n[Test 4] 动态 K 因子")
    k1 = elo.compute_k_factor("Premier League", goal_diff=1, match_stage=10, is_home=True)
    k3 = elo.compute_k_factor("Premier League", goal_diff=3, match_stage=10, is_home=True)
    k_ucl = elo.compute_k_factor("UEFA Champions League", goal_diff=1, match_stage=3, is_home=True)
    print(f"  PL GD1 mid: K={k1:.1f}")
    print(f"  PL GD3 mid: K={k3:.1f} (>{k1:.1f})")
    print(f"  UCL GD1 early: K={k_ucl:.1f} (>{k1:.1f})")
    assert k3 > k1, "大比分 K 应更大"
    assert k_ucl > k1, "欧冠 K 应大于联赛"

    # ── 测试 5: 胜平负概率 ──
    print("\n[Test 5] ELO → 胜平负概率")
    # 势均力敌 (高水平)
    h, d, a = elo.win_draw_loss_proba(1600, 1600, league)
    print(f"  1600(H) vs 1600(A): H={h:.3f} D={d:.3f} A={a:.3f}")
    assert d > 0.22, "实力接近时平局率应较高"

    # 实力悬殊
    h2, d2, a2 = elo.win_draw_loss_proba(1700, 1400, league)
    print(f"  1700(H) vs 1400(A): H={h2:.3f} D={d2:.3f} A={a2:.3f}")
    assert h2 > 0.55, "实力悬殊时主胜率应>55%"
    assert d2 < d, "实力悬殊时平局率应降低"

    # 联赛差异
    h3, d3, a3 = elo.win_draw_loss_proba(1500, 1500, "Ligue 1")
    print(f"  法甲 1500(H) vs 1500(A): H={h3:.3f} D={d3:.3f} A={a3:.3f}")
    assert d3 > d, "法甲平局率应高于英超"

    # ── 测试 6: 批量概率 ──
    print("\n[Test 6] 批量胜平负概率")
    home_r = np.array([1600, 1700, 1500, 1400])
    away_r = np.array([1600, 1400, 1500, 1700])
    batch = elo.win_draw_loss_proba_batch(home_r, away_r, league)
    assert batch.shape == (4, 3)
    assert np.allclose(batch.sum(axis=1), 1.0, atol=0.001)
    print(f"  批量: shape={batch.shape} all_sum_to_1={np.allclose(batch.sum(axis=1), 1.0, atol=0.001)}")

    # ── 测试 7: 历史回测 ──
    print("\n[Test 7] 历史回测")
    fake_matches = [
        {"home_team_name": "A", "away_team_name": "B", "home_score": 2, "away_score": 1,
         "match_date": "2025-01-01", "matchday": 1},
        {"home_team_name": "C", "away_team_name": "A", "home_score": 0, "away_score": 3,
         "match_date": "2025-01-02", "matchday": 1},
        {"home_team_name": "B", "away_team_name": "C", "home_score": 1, "away_score": 0,
         "match_date": "2025-01-08", "matchday": 2},
        {"home_team_name": "A", "away_team_name": "C", "home_score": 4, "away_score": 0,
         "match_date": "2025-01-15", "matchday": 3},
        {"home_team_name": "B", "away_team_name": "A", "home_score": 0, "away_score": 2,
         "match_date": "2025-01-22", "matchday": 4},
        {"home_team_name": "C", "away_team_name": "B", "home_score": 2, "away_score": 2,
         "match_date": "2025-01-29", "matchday": 5},
    ]
    vol = elo.compute_ratings_from_history(fake_matches, "Premier League")
    ratings = elo.get_ratings_dict()
    print(f"  最终评分: A={ratings['A']:.0f} B={ratings['B']:.0f} C={ratings['C']:.0f}")
    print(f"  区分度(σ)={vol:.1f}")
    # A 表现最好 (3胜), C 最差
    assert ratings["A"] > ratings["B"] > ratings["C"], \
        f"评级顺序错误: A({ratings['A']:.0f}) > B({ratings['B']:.0f}) > C({ratings['C']:.0f})"

    # ── 测试 8: 持久化 ──
    print("\n[Test 8] 持久化")
    tmp_path = "output/_elo_test.json"
    elo.save(tmp_path)

    elo2 = EloRatingSystem.load(tmp_path)
    assert elo2.get_rating("A") == ratings["A"]
    print(f"  保存/加载 OK: {len(elo2.ratings)} 队")
    os.remove(tmp_path)

    # ── 测试 9: top-k ──
    print("\n[Test 9] Top-k 查询")
    top = elo.get_top_teams(3)
    for name, r in top:
        print(f"  {name}: {r:.0f}")

    # ── 测试 10: 防偷窥 ──
    print("\n[Test 10] 防偷窥查询")
    r_mid = elo.get_rating_before_date("A", "2025-01-15")
    print(f"  A before 2025-01-15: {r_mid:.0f} (vs final={ratings['A']:.0f})")
    assert r_mid != ratings["A"], "防偷窥应返回中间状态评分"

    print("\n" + "=" * 60)
    print("  [PASS] T04 ELO 评级系统全部自测通过!")
    print("=" * 60)
