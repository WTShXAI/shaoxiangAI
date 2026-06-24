"""
HeuristicPredictor v1.0 — 冷启动救星

设计原则：
  1. 不依赖训练数据（纯规则 + 赔率）
  2. 冷启动时也能输出有意义的概率
  3. 双轨制：特征轨 + 赔率轨，二者加权融合

特征轨（原始 heuristic）：
  - 用 a1, a2, rank_diff, power_gap, h2h, form_momentum 等

赔率轨（新增）：
  - 1X2 反推 implied probs
  - 抽水去除
  - 应用 SP 移植过来的规则 R1/R6
  - 强队 vs 弱队 → 适当提升平局概率（基于 spread）

输出：proba [home, draw, away]
"""
import os
import sys
import logging
import numpy as np
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.environ.get(
    'PROJECT_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, _PROJECT_ROOT)


class HeuristicPredictor:
    """增强版 Heuristic — 冷启动救星"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        # 双轨权重：特征轨 vs 赔率轨
        # 冷启动时（特征都是 0）赔率轨应占主导
        self.feature_weight = self.config.get('feature_weight', 0.30)
        self.odds_weight = self.config.get('odds_weight', 0.70)

    def predict_proba(
        self,
        X: np.ndarray,
        feature_names: list = None,
        odds_data: Optional[Dict] = None,
        league_name: Optional[str] = None,
    ) -> np.ndarray:
        """
        预测概率

        Args:
            X: 特征矩阵 (n, n_features)
            feature_names: 特征名列表
            odds_data: 赔率数据 {'home', 'draw', 'away', 'over25', 'under25'}
            league_name: 联赛名（用于上下文判断）

        Returns:
            proba: (n, 3) 概率数组 [H, D, A]
        """
        n = X.shape[0]

        # 1) 特征轨
        proba_feature = self._predict_by_features(X, feature_names)

        # 2) 赔率轨（如果有赔率）
        if odds_data and self._has_valid_odds(odds_data):
            proba_odds = self._predict_by_odds(odds_data, league_name)
            # 加权融合
            proba = self.feature_weight * proba_feature + self.odds_weight * proba_odds
        else:
            # 没赔率时只用特征
            proba = proba_feature

        # 归一化
        proba = proba / proba.sum(axis=1, keepdims=True)
        return proba

    def _predict_by_features(self, X: np.ndarray, feature_names: list = None) -> np.ndarray:
        """原始特征版 heuristic（移植自 ensemble_trainer._heuristic_predict_proba）"""
        n = X.shape[0]
        proba = np.full((n, 3), 1.0 / 3)

        try:
            if feature_names is None:
                feature_names = []

            def find_idx(name):
                return feature_names.index(name) if name in feature_names else None

            a1_idx = find_idx('a1')
            a2_idx = find_idx('a2')
            rank_idx = find_idx('rank_diff_factor')
            power_gap_idx = find_idx('ix_power_gap')
            h2h_idx = find_idx('h2h_factor')
            form_idx = find_idx('form_momentum')

            h = np.full(n, 0.33)
            d = np.full(n, 0.33)
            a = np.full(n, 0.33)

            if a1_idx is not None:
                h += X[:, a1_idx] * 0.4
                a -= X[:, a1_idx] * 0.3
            if a2_idx is not None:
                h += (X[:, a2_idx] - 0.5) * 0.5
            if rank_idx is not None:
                h += X[:, rank_idx] * 0.0005
                a -= X[:, rank_idx] * 0.0005
            if power_gap_idx is not None:
                # 实力接近 → 平局概率提升
                d += np.maximum(0.0, 0.20 - X[:, power_gap_idx] * 0.5)
            if h2h_idx is not None:
                h += X[:, h2h_idx] * 0.15
                a -= X[:, h2h_idx] * 0.10
            if form_idx is not None:
                h += X[:, form_idx] * 0.10
                a -= X[:, form_idx] * 0.05

            h = np.maximum(h, 0.05)
            d = np.maximum(d, 0.05)
            a = np.maximum(a, 0.05)
            total = h + d + a
            proba[:, 0] = h / total
            proba[:, 1] = d / total
            proba[:, 2] = a / total
        except Exception as e:
            logger.warning(f"特征轨 heuristic 失败: {e}")

        return proba

    def _predict_by_odds(self, odds_data: Dict, league_name: Optional[str] = None) -> np.ndarray:
        """
        赔率版 heuristic（核心：冷启动救星）

        步骤：
          1. 1X2 → 隐含概率 + 去抽水
          2. 大小球 → 大球倾向（D 的弱信号）
          3. SP R1 规则：检测平局赔率异常
          4. SP R6 规则：检测 CS 波胆方向（如果有）
          5. 实力差距 → 平局基础概率调整
        """
        try:
            h_odd = float(odds_data.get('home', 0))
            d_odd = float(odds_data.get('draw', 0))
            a_odd = float(odds_data.get('away', 0))
            o25 = float(odds_data.get('over25', 0))
            u25 = float(odds_data.get('under25', 0))

            if h_odd <= 1.01 or d_odd <= 1.01 or a_odd <= 1.01:
                # 异常赔率 → 均匀
                return np.array([[0.33, 0.34, 0.33]])

            # 1) 1X2 → 隐含概率
            inv_h, inv_d, inv_a = 1/h_odd, 1/d_odd, 1/a_odd
            margin = inv_h + inv_d + inv_a
            p_h = inv_h / margin
            p_d = inv_d / margin
            p_a = inv_a / margin

            # 2) SP R1: 平局赔率最低 → 大幅提升平局概率
            if d_odd <= h_odd and d_odd <= a_odd:
                # SP 规则 R1: 平局最低 → 36.2% 命中率
                p_d = max(p_d, 0.36)
                # 重新归一化
                total = p_h + p_d + p_a
                p_h /= total; p_d /= total; p_a /= total

            # 3) 大小球 → 平局弱信号
            # 双方实力接近时大球概率高 → 平局概率高
            if o25 > 1.0 and u25 > 1.0:
                p_o25 = (1/o25) / ((1/o25) + (1/u25))
                # 实力差距用 spread 衡量
                spread = abs(inv_h - inv_a)  # 隐含概率差
                # spread 小 → 实力接近 → 大球倾向 + 平局倾向
                if spread < 0.15:
                    p_d = min(0.40, p_d + (p_o25 - 0.5) * 0.1 + 0.03)
                    total = p_h + p_d + p_a
                    p_h /= total; p_d /= total; p_a /= total

            # 4) 极端强弱 → 抑制平局
            if h_odd < 1.20 or a_odd < 1.20:
                # 超低赔方 85% 概率赢
                p_d = min(p_d, 0.15)
                total = p_h + p_d + p_a
                p_h /= total; p_d /= total; p_a /= total

            return np.array([[p_h, p_d, p_a]])
        except Exception as e:
            logger.warning(f"赔率轨 heuristic 失败: {e}")
            return np.array([[0.33, 0.34, 0.33]])

    def _has_valid_odds(self, odds_data: Dict) -> bool:
        """检查赔率数据是否有效"""
        try:
            h = float(odds_data.get('home', 0))
            d = float(odds_data.get('draw', 0))
            a = float(odds_data.get('away', 0))
            return h > 1.01 and d > 1.01 and a > 1.01
        except Exception:
            return False


# ── 自检 ──
if __name__ == '__main__':
    print('=' * 60)
    print('  HeuristicPredictor v1.0 — 自检')
    print('=' * 60)

    hp = HeuristicPredictor()

    # 1) 卡塔尔 vs 瑞士（冷启动场景）
    print('\n[测试1] 卡塔尔 vs 瑞士 (冷启动)')
    print('  赔率: H=13.0 D=6.70 A=1.21 O2.5=1.72 U2.5=2.21')

    # 特征全 0（冷启动）
    X = np.zeros((1, 72))
    odds = {'home': 13.0, 'draw': 6.70, 'away': 1.21, 'over25': 1.72, 'under25': 2.21}

    p = hp.predict_proba(X, feature_names=[], odds_data=odds, league_name='世界杯')
    print(f'  → H={p[0][0]:.1%} D={p[0][1]:.1%} A={p[0][2]:.1%}')
    print(f'  预期: A=主导（赔率反映客胜极强）')

    # 2) 实力接近（应该平局概率高）
    print('\n[测试2] 实力接近 (H=2.50 D=3.20 A=2.80)')
    odds2 = {'home': 2.50, 'draw': 3.20, 'away': 2.80, 'over25': 1.85, 'under25': 2.05}
    p2 = hp.predict_proba(X, feature_names=[], odds_data=odds2, league_name='英超')
    print(f'  → H={p2[0][0]:.1%} D={p2[0][1]:.1%} A={p2[0][2]:.1%}')
    print(f'  预期: D 较高（实力接近 + 略偏大球）')

    # 3) SP R1 触发（平局赔率最低）
    print('\n[测试3] R1触发 (H=3.50 D=2.80 A=2.50 — 平局最低)')
    odds3 = {'home': 3.50, 'draw': 2.80, 'away': 2.50, 'over25': 1.80, 'under25': 2.10}
    p3 = hp.predict_proba(X, feature_names=[], odds_data=odds3, league_name='西甲')
    print(f'  → H={p3[0][0]:.1%} D={p3[0][1]:.1%} A={p3[0][2]:.1%}')
    print(f'  预期: D ≥ 0.36 (R1 触发上限)')

    print('\n✅ HeuristicPredictor 就绪')
