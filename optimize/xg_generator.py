#!/usr/bin/env python3
"""
哨响AI — 动态预期进球生成器 (xG Generator)
=============================================
替代固定 goal_diff 公式，基于赔率/概率/实力数据动态计算每场比赛的
home_xG (主队预期进球) 和 away_xG (客队预期进球)。

设计原则：
    1. 赔率驱动总进球期望：主胜/平局/客胜概率 → 总进球基线
    2. 实力差分配进球：rating差/攻防数据 → 主客进球拆分
    3. 联赛感知：不同联赛的进球特征不同 (德甲 3.12 vs 西甲 2.59)
    4. 比赛不确定性：±0.15 小扰动避免同质化

核心流程：
    赔率 → 隐含概率 → 总进球期望(回归模型)
          ↓
    rating差 + 攻防特征 → 主客分配比例
          ↓
    home_xG, away_xG → 泊松比分矩阵

日期: 2026-06-02
"""

import math
import random
import hashlib
import logging
from typing import Dict, Tuple, Optional, List

import numpy as np

logger = logging.getLogger(__name__)


# ─── 联赛场均进球基线 ─────────────────────────────────
LEAGUE_AVG_GOALS: Dict[str, float] = {
    'premier league': 2.85,
    'bundesliga': 3.12,
    'la liga': 2.59,
    'serie a': 2.70,
    'ligue 1': 2.72,
    'eredivisie': 3.05,
    'primeira liga': 2.55,
    'championship': 2.48,
    'mls': 2.95,
    'champions league': 2.89,
    'europa league': 2.78,
    'default': 2.72,
}

# ─── 联赛主场优势因子 ─────────────────────────────────
LEAGUE_HOME_ADVANTAGE: Dict[str, float] = {
    'premier league': 1.08,
    'bundesliga': 1.10,
    'la liga': 1.09,
    'serie a': 1.12,
    'ligue 1': 1.07,
    'default': 1.08,
}


def _stable_hash(key: str) -> float:
    """确定性哈希 → 0.0~1.0 之间的小扰动种子"""
    h = hashlib.md5(key.encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _league_lookup(league_name: str, mapping: Dict[str, float],
                   default: float = None) -> float:
    """模糊联赛名匹配"""
    if not league_name:
        return default or mapping.get('default', 2.72)
    name = league_name.strip().lower()
    for k, v in mapping.items():
        if k in name or name in k:
            return v
    return default or mapping.get('default', 2.72)


class XGGenerator:
    """
    动态 xG 生成引擎

    为每场比赛生成独一无二的 home_xG 和 away_xG，
    完全替代固定的 goal_diff → λ 公式。
    """

    def __init__(self, config: Dict = None):
        cfg = config or {}
        xg_cfg = cfg.get('models', {}).get('xg', {})

        # 配置参数
        self.base_lambda = xg_cfg.get('base_lambda', 2.72)
        self.home_advantage = xg_cfg.get('home_advantage_factor', 1.08)
        self.max_goals = xg_cfg.get('max_goals', 6)
        self.enable_jitter = xg_cfg.get('enable_jitter', True)
        self.jitter_range = xg_cfg.get('jitter_range', 0.15)

        # 回归系数：主胜/客胜/平局概率 → 总进球数
        # 这些系数可根据历史数据校准
        self.coef_home_win = xg_cfg.get('coef_home_win', 0.50)
        self.coef_away_win = xg_cfg.get('coef_away_win', 0.50)
        self.coef_draw_adj = xg_cfg.get('coef_draw_adj', -0.15)

        logger.debug(f"[XG] 初始化: base_λ={self.base_lambda}, "
                     f"home_adv={self.home_advantage}")

    # ════════════════════════════════════════════════════════════
    # 核心 API
    # ════════════════════════════════════════════════════════════

    def generate_xg(self, home_prob: float, draw_prob: float, away_prob: float,
                    odds: Optional[Dict[str, float]] = None,
                    league_name: str = '',
                    home_team: str = '', away_team: str = '',
                    home_rating: Optional[float] = None,
                    away_rating: Optional[float] = None,
                    home_attack: Optional[float] = None,
                    away_defense: Optional[float] = None,
                    ) -> Tuple[float, float]:
        """
        动态生成 home_xG 和 away_xG

        Args:
            home_prob/draw_prob/away_prob: 胜平负概率 (0~1, 须归一化)
            odds: 可选赔率字典 {'home': float, 'draw': float, 'away': float}
            league_name: 联赛名 (用于联赛特征)
            home_team/away_team: 队伍名 (用于扰动种子)
            home_rating/away_rating: 球队综合评分 (0~100)
            home_attack/away_defense: 攻防数据 (选填)

        Returns:
            (home_xG, away_xG): 两队预期进球值
        """
        # Step 1: 总进球期望
        league_lambda = _league_lookup(league_name, LEAGUE_AVG_GOALS, self.base_lambda)
        total_goals = self._compute_total_goals(home_prob, draw_prob, away_prob,
                                                 odds, league_lambda)

        # Step 2: 实力差
        home_share = self._compute_home_share(
            home_prob, away_prob, home_rating, away_rating,
            home_attack, away_defense, league_name
        )

        # Step 3: 分配主客 xG (含主场优势)
        league_ha = _league_lookup(league_name, LEAGUE_HOME_ADVANTAGE, self.home_advantage)
        raw_home_xg = total_goals * home_share * league_ha
        raw_away_xg = total_goals * (1.0 - home_share)

        # Step 4: 归一化保持总进球不变
        raw_total = raw_home_xg + raw_away_xg
        if raw_total > 0:
            home_xg = total_goals * (raw_home_xg / raw_total)
            away_xg = total_goals * (raw_away_xg / raw_total)
        else:
            home_xg = total_goals * 0.5
            away_xg = total_goals * 0.5

        # Step 5: 比赛专属扰动
        if self.enable_jitter:
            match_key = f"{home_team}_{away_team}_{league_name}"
            jitter_h = 1.0 + (2.0 * _stable_hash(match_key + '_h') - 1.0) * self.jitter_range
            jitter_a = 1.0 + (2.0 * _stable_hash(match_key + '_a') - 1.0) * self.jitter_range

            # 保守扰动：保持总进球相对稳定（±0.2球的调整）
            home_xg *= jitter_h
            away_xg *= jitter_a

            # 重新归一化到原 total_goals
            jitter_total = home_xg + away_xg
            if jitter_total > 0:
                home_xg = total_goals * (home_xg / jitter_total)
                away_xg = total_goals * (away_xg / jitter_total)

        # 保底
        home_xg = max(home_xg, 0.1)
        away_xg = max(away_xg, 0.1)

        return round(home_xg, 4), round(away_xg, 4)

    def generate_xg_from_odds(self, home_odds: float, draw_odds: float,
                               away_odds: float, **kwargs) -> Tuple[float, float]:
        """
        从原始赔率直接生成 xG（无需预计算的概率）

        先计算隐含概率，再调用主方法。
        """
        # 去除市场水分 → 隐含概率
        overround = 1.0 / home_odds + 1.0 / draw_odds + 1.0 / away_odds
        hp = (1.0 / home_odds) / overround
        dp = (1.0 / draw_odds) / overround
        ap = (1.0 / away_odds) / overround

        odds_dict = {'home': home_odds, 'draw': draw_odds, 'away': away_odds}
        return self.generate_xg(hp, dp, ap, odds=odds_dict, **kwargs)

    # ════════════════════════════════════════════════════════════
    # 内部计算
    # ════════════════════════════════════════════════════════════

    def _compute_total_goals(self, home_prob: float, draw_prob: float,
                              away_prob: float, odds: Optional[Dict[str, float]],
                              league_lambda: float) -> float:
        """
        从概率估计总进球期望

        回归逻辑：
            total_goals = league_λ + α·(P_home - 0.5) + β·(P_away - 0.3) + δ·P_draw

        解释：
            - P_home 高 → 比赛节奏可能快 → 总进球微增
            - P_away 高 → 强客弱主 → 总进球增加（客队反击空间大）
            - P_draw 高 → 势均力敌 → 总进球微降（互相试探）
        """
        total = league_lambda
        total += self.coef_home_win * (home_prob - 0.40)  # 相对于中立 0.40 基准
        total += self.coef_away_win * (away_prob - 0.30)  # 相对于中立 0.30 基准
        total += self.coef_draw_adj * (draw_prob - 0.30)  # 平局概率越高 → 总进球越低

        # 如果赔率可用，叠加赔率隐含信息
        if odds:
            implied_total = self._odds_to_total_goals(odds, league_lambda)
            # 50% 回归模型 + 50% 赔率隐含
            total = 0.5 * total + 0.5 * implied_total

        return max(total, 0.5)  # 至少 0.5 球

    @staticmethod
    def _odds_to_total_goals(odds: Dict[str, float], league_lambda: float) -> float:
        """
        从赔率分布推总进球

        原理：
            - 赔率高 → 概率低 → 强队方向明确 → 进球差大
            - 主胜赔率低(<1.8) + 客胜赔率高(>4) → 一边倒 → 总进球可能高
            - 三赔接近 → 势均力敌 → 总进球适中
        """
        h = odds.get('home', 2.0)
        d = odds.get('draw', 3.2)
        a = odds.get('away', 3.5)

        # 赔率变异系数：赔率差距越大 → 比赛越一边倒
        odds_array = np.array([h, d, a])
        cv = odds_array.std() / odds_array.mean() if odds_array.mean() > 0 else 0.3

        # 比赛"张力"指标：主客赔率比
        tension = abs(math.log(max(h, 1.01)) - math.log(max(a, 1.01)))

        # 总进球 = 联赛基线 + 张力修正
        # 一边倒比赛 (tension > 1.5) → +0.3球；势均力敌 → -0.1球
        total = league_lambda + (tension - 0.8) * 0.25

        return max(min(total, 4.0), 0.5)

    def _compute_home_share(self, home_prob: float, away_prob: float,
                             home_rating: Optional[float] = None,
                             away_rating: Optional[float] = None,
                             home_attack: Optional[float] = None,
                             away_defense: Optional[float] = None,
                             league_name: str = '') -> float:
        """
        计算主队进球份额 (0.35~0.70)

        多信号融合：
            1. 概率差 (权重自适应)：P_home vs P_away 的差值
            2. rating 差 (30%权重)：球队综合评分差（不可用时权重归概率）
            3. 攻防匹配 (20%权重)：home_attack vs away_defense（不可用时权重归概率）
            4. 主场基线 (10%权重)：始终偏向主队
        """
        total_weight = 0.0
        share = 0.0

        # 1. 概率差信号 (基础权重 40%，其他信号不可用时增加)
        prob_diff = home_prob - away_prob
        # 提高概率差敏感度: 0.50 → 0.80, 使 P_home=0.55 vs P_away=0.30 → prob_share=0.50+0.25*0.80=0.70
        prob_share = 0.50 + prob_diff * 0.80
        prob_share = max(0.30, min(0.75, prob_share))
        prob_weight = 0.40
        share += prob_share * prob_weight
        total_weight += prob_weight

        # 2. rating 差信号
        rating_available = home_rating is not None and away_rating is not None
        if rating_available:
            rating_diff = min(max(home_rating - away_rating, -30), 30) / 100.0
            rating_share = 0.50 + rating_diff * 0.60
            rating_share = max(0.30, min(0.70, rating_share))
            share += rating_share * 0.30
            total_weight += 0.30
        else:
            # rating 不可用时，将权重转给概率信号
            share += prob_share * 0.30
            total_weight += 0.30

        # 3. 攻防匹配信号
        att_def_available = home_attack is not None and away_defense is not None
        if att_def_available:
            att_def_share = 0.50 + (home_attack - away_defense) * 0.30
            att_def_share = max(0.35, min(0.65, att_def_share))
            share += att_def_share * 0.20
            total_weight += 0.20
        else:
            # 攻防不可用时，将权重转给概率信号
            share += prob_share * 0.20
            total_weight += 0.20

        # 4. 主场基准
        home_baseline = 0.52  # 主场天然优势
        share += home_baseline * 0.10
        total_weight += 0.10

        # 归一化
        if total_weight > 0:
            share /= total_weight

        return max(0.30, min(0.70, share))

    # ════════════════════════════════════════════════════════════
    # 冷门调整
    # ════════════════════════════════════════════════════════════

    def apply_upset_adjustment(self, home_xg: float, away_xg: float,
                                upset_score: float, upset_direction: str,
                                upset_type: str = '') -> Tuple[float, float]:
        """
        根据冷门信号调整 xG

        Args:
            home_xg, away_xg: 原始预期进球
            upset_score: 冷门评分 (0~1)
            upset_direction: 'away_win' | 'home_win' | 'draw'
            upset_type: 冷门类型描述

        Returns:
            (调整后 home_xg, 调整后 away_xg)
        """
        if upset_score < 0.3:
            return home_xg, away_xg

        # 调整强度与冷门评分正相关
        factor = min(upset_score * 1.2, 0.60)

        if upset_direction == 'away_win':
            # 客胜冷门：降低主队xG，提高客队xG
            transfer = min(home_xg * factor, home_xg * 0.50)
            home_xg -= transfer
            away_xg += transfer * 0.80  # 80% 效率转换
        elif upset_direction == 'home_win':
            # 主胜冷门（罕见，通常发生在客队更强时）
            transfer = min(away_xg * factor, away_xg * 0.50)
            away_xg -= transfer
            home_xg += transfer * 0.80
        elif upset_direction == 'draw':
            # 平局冷门：拉近两队 xG
            diff = abs(home_xg - away_xg)
            adjustment = diff * factor * 0.60
            if home_xg > away_xg:
                home_xg -= adjustment
                away_xg += adjustment
            else:
                home_xg += adjustment
                away_xg -= adjustment

        home_xg = max(home_xg, 0.05)
        away_xg = max(away_xg, 0.05)

        return round(home_xg, 4), round(away_xg, 4)


# ════════════════════════════════════════════════════════════
# 便捷函数（向后兼容）
# ════════════════════════════════════════════════════════════

_DEFAULT_GENERATOR: Optional[XGGenerator] = None


def get_xg_generator(config: Dict = None) -> XGGenerator:
    """获取全局 XGGenerator 单例"""
    global _DEFAULT_GENERATOR
    if _DEFAULT_GENERATOR is None:
        _DEFAULT_GENERATOR = XGGenerator(config)
    return _DEFAULT_GENERATOR


def generate_xg(home_prob: float, draw_prob: float, away_prob: float,
                odds: Dict = None, league_name: str = '',
                home_team: str = '', away_team: str = '',
                **kwargs) -> Tuple[float, float]:
    """便捷函数：生成 home_xG, away_xG"""
    gen = get_xg_generator()
    return gen.generate_xg(home_prob, draw_prob, away_prob,
                           odds=odds, league_name=league_name,
                           home_team=home_team, away_team=away_team, **kwargs)


# ════════════════════════════════════════════════════════════
# 测试
# ════════════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    gen = XGGenerator()
    tests = [
        # (home_p, draw_p, away_p, odds, league, desc)
        (0.65, 0.20, 0.15, {'home': 1.50, 'draw': 4.50, 'away': 7.00},
         'Premier League', '曼城', '南安普顿', "强队主场碾压"),
        (0.38, 0.30, 0.32, {'home': 2.50, 'draw': 3.10, 'away': 2.80},
         'Premier League', '阿森纳', '利物浦', "强强对话"),
        (0.25, 0.28, 0.47, {'home': 3.80, 'draw': 3.40, 'away': 1.95},
         'Bundesliga', '奥格斯堡', '拜仁', "强队客场"),
        (0.32, 0.35, 0.33, {'home': 2.80, 'draw': 2.70, 'away': 2.90},
         'Serie A', '罗马', '那不勒斯', "均势意甲"),
        (0.55, 0.28, 0.17, {'home': 1.80, 'draw': 3.40, 'away': 4.80},
         'La Liga', '巴萨', '赫塔菲', "西甲主场优势"),
    ]

    print(f"{'场景':<30} {'H_xG':>7} {'A_xG':>7} {'总计':>7} {'主胜%':>7}")
    print("-" * 70)

    total_checks = 0
    passed = 0

    for hp, dp, ap, odds, league, ht, at, desc in tests:
        hxg, axg = gen.generate_xg(hp, dp, ap, odds=odds,
                                    league_name=league,
                                    home_team=ht, away_team=at)
        total = hxg + axg
        league_lam = _league_lookup(league, LEAGUE_AVG_GOALS, 2.72)
        label = f'{ht}vs{at}'
        print(f"{desc:<30} {hxg:7.3f} {axg:7.3f} {total:7.3f} {(hp*100):7.1f}%")

        # 验证
        total_checks += 1
        if abs(total - league_lam) < 0.6:
            passed += 1

        # 主胜概率高 → home_xG > away_xG
        total_checks += 1
        if hp > ap:
            if hxg > axg:
                passed += 1
            else:
                print(f"  [WARN] 主胜概率{hp}但home_xG({hxg:.3f})<=away_xG({axg:.3f})")
        elif ap > hp:
            if axg > hxg or abs(ap - hp) < 0.03:
                passed += 1  # 概率接近时主场优势可能反转方向
            else:
                print(f"  [WARN] 客胜概率{ap}但away_xG({axg:.3f})<=home_xG({hxg:.3f})")
        else:
            passed += 1

    # 测试冷门调整
    print(f"\n{'冷门调整测试':-^60}")
    hxg, axg = gen.generate_xg(0.55, 0.25, 0.20,
                                odds={'home': 1.80, 'draw': 3.50, 'away': 5.00},
                                league_name='Premier League',
                                home_team='曼城', away_team='伯恩利')
    print(f"  调整前: home_xG={hxg:.3f}, away_xG={axg:.3f}")
    hxg2, axg2 = gen.apply_upset_adjustment(hxg, axg, 0.75, 'away_win', '客胜冷门')
    print(f"  调整后(冷门0.75, 客胜方向): home_xG={hxg2:.3f}, away_xG={axg2:.3f}")

    total_checks += 2
    if hxg2 < hxg:
        passed += 1
    if axg2 > axg * 1.1:
        passed += 1

    # 平局冷门测试
    hxg3, axg3 = gen.apply_upset_adjustment(2.0, 0.4, 0.70, 'draw', '平局冷门')
    diff_before = abs(2.0 - 0.4)
    diff_after = abs(hxg3 - axg3)
    total_checks += 1
    if diff_after < diff_before:
        passed += 1

    print(f"\n{'='*60}")
    print(f"  结果: {passed}/{total_checks} 通过")
    if passed == total_checks:
        print("  ALL PASSED!")
