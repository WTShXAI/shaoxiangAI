"""
UnifiedFeaturePipeline — 统一特征管道 (v2.0)
==========================================
从原始数据同时产出:
  1. 主模型特征 (58维 SafeTemporal)
  2. 各专家领域特征子集 (7个专家)

阶段一增强:
  - 赔率衍生特征 (ELO概率→隐含价值指标)
  - 交叉特征 (elo_diff x form_momentum 等)
  - 平局信号特征 (比分胶着度、低进球倾向等)
  - 滚动方差/动量变化率

阶段二新增:
  - referee: 联赛级别代理特征 (严格度/主场偏哨)
  - coach: 球队级历史模式代理 (主客场表现差异/状态稳定性)
  - timespace: 从date列计算的休整天数+赛程密度

阶段三新增:
  - draw_signal: 10维平局专用特征 (实力接近度/H2H平局/风格倾向/压力/赛季阶段等)
"""
import os
import sys
import math
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)


class UnifiedFeaturePipeline:
    """统一特征管道 v2.0 — 特征多样化 + 7专家支持"""

    def __init__(self):
        self._main_model = None
        self._df_processed = None

    @property
    def main_model(self):
        if self._main_model is None:
            from backend.models.footballai_enhanced import FootballAIEnhanced
            self._main_model = FootballAIEnhanced()
        return self._main_model

    # ──────────── 1. 主模型特征 (58维) ────────────

    def prepare_main_features(self, df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, List[str]]:
        """产出主模型的 58 维 SafeTemporal 特征"""
        return self.main_model.prepare_features(df)

    # ──────────── 2. 专家特征子集 (7个专家) ────────────

    def prepare_expert_features(self, df: pd.DataFrame) -> Dict[str, np.ndarray]:
        """
        为每个专家准备其领域专属特征。

        返回 Dict[expert_name, feature_matrix]
        所有矩阵行数与 df 相同。
        """
        result = {}

        # 先确保主模型特征已准备好（这会触发高级特征工程）
        X_main, y, feat_names = self.prepare_main_features(df.copy())

        # 构建一个包含所有特征的 DataFrame，方便各专家取子集
        df_feat = pd.DataFrame(X_main, columns=feat_names)

        # 补充原始元数据列（专家可能需要）
        meta_cols = ['home_team', 'away_team', 'league', 'date',
                     'home_score', 'away_score',
                     'home_win_prob', 'home_avg_goals_for',
                     'home_avg_goals_against', 'away_avg_goals_for',
                     'away_avg_goals_against']
        for c in meta_cols:
            if c in df.columns:
                df_feat[c] = df[c].values if hasattr(df[c], 'values') else df[c]

        # [v4-fix] 计算 result 列 (0=主胜,1=平局,2=客胜) — 原始CSV无此列!
        if 'home_score' in df.columns and 'away_score' in df.columns:
            hs = df['home_score'].values.astype(np.float64)
            aw = df['away_score'].values.astype(np.float64)
            result_arr = np.where(hs > aw, 0, np.where(hs == aw, 1, 2)).astype(np.int32)
            df_feat['result'] = result_arr
        else:
            # 回退: 用y值
            df_feat['result'] = y

        result['trend']       = self._build_trend_features(df, df_feat)
        result['alpha']       = self._build_alpha_features_v2(df, df_feat)      # [v2] 增强版
        result['quant']       = self._build_quant_features_v2(df, df_feat)      # [v2] 增强版
        result['goal_timing'] = self._build_goal_timing_features(df, df_feat)
        result['referee']     = self._build_referee_proxy_features(df, df_feat)  # [NEW] 代理
        result['coach']       = self._build_coach_proxy_features(df, df_feat)    # [NEW] 代理
        result['timespace']   = self._build_timespace_features(df, df_feat)      # [NEW] 从date计算
        result['draw_signal'] = self._build_draw_signal_features(df, df_feat)    # [v3] 10维平局特征

        return result

    # ════════════ Trend 特征 (19维) ════════════

    def _build_trend_features(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """趋势分析专家特征: 表单动量 + 排名差 + H2H + 进攻/防守状态"""
        trend_cols = []
        col_map = {
            'form_momentum': ['form_momentum_5_home', 'form_momentum_5_away'],
            'form_3':        ['last_3_form_home', 'last_3_form_away'],
            'form_5':        ['last_5_form_home', 'last_5_form_away'],
            'form_10':       ['last_10_form_home', 'last_10_form_away'],
            'attack_5':      ['attack_form_5_home', 'attack_form_5_away'],
            'defense_5':     ['defense_form_5_home', 'defense_form_5_away'],
            'goal_diff_5':   ['goal_diff_form_5_home', 'goal_diff_form_5_away'],
            'h2h_win_rate':  ['h2h_home_win_rate', None],
            'h2h_draw_rate': ['h2h_draw_rate', None],
            'elo_diff':      ['elo_diff', None],
            'fatigue_diff':  ['home_fatigue', 'away_fatigue'],
        }

        for name, cols in col_map.items():
            if cols[0] and cols[0] in df_feat.columns:
                trend_cols.append(df_feat[cols[0]].fillna(0).values.astype(np.float32))
            else:
                trend_cols.append(np.zeros(len(df_feat), dtype=np.float32))
            if cols[1] and cols[1] in df_feat.columns:
                trend_cols.append(df_feat[cols[1]].fillna(0).values.astype(np.float32))
            elif name not in ('h2h_win_rate', 'h2h_draw_rate', 'elo_diff'):
                trend_cols.append(np.zeros(len(df_feat), dtype=np.float32))

        return np.column_stack(trend_cols)

    # ══════════ Alpha 特征 v2 (12维, +5维衍生) ══════════

    def _build_alpha_features_v2(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """
        Alpha决策专家特征 v2 — 增强版:
          基础(7) + 隐含赔率价值(3) + 交叉特征(2) = 12维
        """
        alpha_cols = []

        # --- 基础: ELO + Poisson + 表单 ---
        for c in ['elo_diff', 'poisson_home_goals', 'poisson_away_goals']:
            if c in df_feat.columns:
                alpha_cols.append(df_feat[c].fillna(0).values.astype(np.float32))
            else:
                alpha_cols.append(np.zeros(len(df_feat), dtype=np.float32))

        for c in ['form_momentum_5_home', 'form_momentum_5_away']:
            if c in df_feat.columns:
                alpha_cols.append(df_feat[c].fillna(0).values.astype(np.float32))
            else:
                alpha_cols.append(np.zeros(len(df_feat), dtype=np.float32))

        for c in ['attack_strength', 'defense_weakness']:
            if c in df_feat.columns:
                alpha_cols.append(df_feat[c].fillna(0).values.astype(np.float32))
            else:
                alpha_cols.append(np.ones(len(df_feat), dtype=np.float32))

        # --- [v2 NEW] 赔率衍生特征 ---
        # 隐含主胜概率 (来自ELO)
        if 'home_win_prob' in df_feat.columns:
            imp_home = df_feat['home_win_prob'].fillna(0.50).values.astype(np.float32)
        else:
            imp_home = np.full(len(df_feat), 0.50, dtype=np.float32)
        alpha_cols.append(imp_home)

        # 隐含平局概率 (Poisson近似)
        if 'poisson_home_goals' in df_feat.columns and 'poisson_away_goals' in df_feat.columns:
            ph = df_feat['poisson_home_goals'].fillna(1.3).values.astype(np.float64)
            pa = df_feat['poisson_away_goals'].fillna(1.1).values.astype(np.float64)
            # 用Poisson均值估算平局概率: P(X=Y) ≈ exp(-λ1-λ2) * I0(2*sqrt(λ1*λ2))
            lam_sum = ph + pa
            lam_prod = ph * pa
            imp_draw = np.exp(-lam_sum) * np.array([self._i0(2 * math.sqrt(max(lp, 0))) for lp in lam_prod])
            imp_draw = np.clip(imp_draw, 0.20, 0.35).astype(np.float32)
        else:
            imp_draw = np.full(len(df_feat), 0.27, dtype=np.float32)
        alpha_cols.append(imp_draw)

        # 价值指标: model_implied - market_implied 的偏离程度 (用ELO差异代替市场)
        if 'elo_diff' in df_feat.columns:
            elo_val = df_feat['elo_diff'].fillna(0).values.astype(np.float32)
            value_idx = np.tanh(elo_val / 100.0)  # 归一化到 [-1, 1]
        else:
            value_idx = np.zeros(len(df_feat), dtype=np.float32)
        alpha_cols.append(value_idx)

        # --- [v2 NEW] 交叉特征 ---
        # ELO差距 x 表单动量交互
        if 'elo_diff' in df_feat.columns and 'form_momentum_5_home' in df_feat.columns:
            elo_d = df_feat['elo_diff'].fillna(0).values.astype(np.float32)
            fm_h = df_feat['form_momentum_5_home'].fillna(0).values.astype(np.float32)
            fm_a = df_feat['form_momentum_5_away'].fillna(0).values.astype(np.float32)
            cross1 = elo_d * (fm_h - fm_a) / 10.0  # 实力优势x表单优势
            cross1 = np.clip(cross1, -3, 3).astype(np.float32)
        else:
            cross1 = np.zeros(len(df_feat), dtype=np.float32)
        alpha_cols.append(cross1)

        # 攻防比 (进攻强度/防守弱点)
        if 'attack_strength' in df_feat.columns and 'defense_weakness' in df_feat.columns:
            atk = df_feat['attack_strength'].fillna(1.0).values.astype(np.float64)
        else:
            atk = np.ones(len(df_feat), dtype=np.float64)
        def_ = df_feat.get('defense_weakness', pd.Series(np.ones(len(df_feat)))).fillna(1.0).values.astype(np.float64)
        ad_ratio = np.where(def_ > 0.01, atk / def_, 1.0)
        ad_ratio = np.clip(ad_ratio, 0.2, 5.0).astype(np.float32)
        alpha_cols.append(ad_ratio)

        return np.column_stack(alpha_cols)

    @staticmethod
    def _i0(x: float) -> float:
        """修正Bessel函数I0 近似"""
        ax = abs(x)
        if ax < 3.75:
            y = x / 3.75
            y2 = y * y
            return 1.0 + y2 * (
                3.5156229 + y2 * (
                    3.0899424 + y2 * (
                        1.2067492 + y2 * (
                            0.2659732 + y2 * (
                                0.0360768 + y2 * 0.0045813)))))
        else:
            y = 3.75 / ax
            return (math.exp(ax) / math.sqrt(ax)) * (
                0.39894228 + y * (
                    0.01328592 + y * (
                        0.00225319 + y * (
                            -0.00157565 + y * (
                                0.00916281 + y * (
                                    -0.02057706 + y * 0.02635537))))))

    # ══════════ Quant 特征 v2 (14维, +6维衍生) ══════════

    def _build_quant_features_v2(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """
        量化交易专家特征 v2 — 增强版:
          基础(8) + 波动率(3) + 动量加速度(3) = 14维
        """
        quant_cols = []

        # --- 基础8维 ---
        base_cols = [
            'elo_diff', 'form_momentum_5_home', 'form_momentum_5_away',
            'attack_form_5_home', 'attack_form_5_away',
            'defense_form_5_home', 'defense_form_5_away',
            'poisson_home_goals'
        ]
        for c in base_cols:
            if c in df_feat.columns:
                val = df_feat[c].fillna(0).values.astype(np.float32)
            else:
                val = np.zeros(len(df_feat), dtype=np.float32)
            quant_cols.append(val)

        # --- [v2 NEW] 波动率特征 ---
        # 进球期望波动 (高波动 = 不确定性大 = 平局可能高)
        if 'poisson_home_goals' in df_feat.columns and 'poisson_away_goals' in df_feat.columns:
            ph = df_feat['poisson_home_goals'].fillna(1.3).values.astype(np.float32)
            pa = df_feat['poisson_away_goals'].fillna(1.1).values.astype(np.float32)
            goal_vol = np.sqrt(ph + pa)  # Poisson方差=λ, 总方差=λ1+λ2
            quant_cols.append(goal_vol)
        else:
            quant_cols.append(np.ones(len(df_feat), dtype=np.float32) * 1.5)

        # 表单一致性 (最近5场 vs 最近10场的胜率变化 = 状态稳定性)
        for side in ['home', 'away']:
            f5 = df_feat.get(f'last_5_form_{side}')
            f10 = df_feat.get(f'last_10_form_{side}')
            if f5 is not None and f10 is not None:
                fv = (f5.fillna(0).values - f10.fillna(0).values).astype(np.float32)
            else:
                fv = np.zeros(len(df_feat), dtype=np.float32)
            quant_cols.append(fv)

        # ELO近期变化幅度 (如果有的话)
        if 'home_elo_updated' in df_feat.columns and 'home_elo' in df_feat.columns:
            elo_ch_h = (df_feat['home_elo_updated'] - df_feat['home_elo']).fillna(0).values.astype(np.float32)
        else:
            elo_ch_h = np.zeros(len(df_feat), dtype=np.float32)
        quant_cols.append(elo_ch_h)

        # --- [v2 NEW] 动量加速度 ---
        # 攻击力近5场 vs 近20场比值 (攻击上升/下降趋势)
        for side in ['home', 'away']:
            a5 = df_feat.get(f'attack_form_5_{side}')
            a20 = df_raw.get(f'{side}_last_20_goals_for')
            if a5 is not None and a20 is not None:
                a5v = a5.fillna(0).values.astype(np.float64)
                a20v = a20.fillna(0.001).values.astype(np.float64)
                accel = np.where(a20v > 0.01, a5v / a20v * 4.0, 0.0)  # 缩放到合理范围
                accel = np.clip(accel, 0.1, 5.0).astype(np.float32)
            else:
                accel = np.ones(len(df_feat), dtype=np.float32)
            quant_cols.append(accel)

        return np.column_stack(quant_cols)

    # ══════════ GoalTiming 特征 (10维, 保持不变) ══════════

    def _build_goal_timing_features(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """进球时序专家特征: 球队进攻能力 + 近期进球趋势 + 赛程密度"""
        gt_cols = []

        for c in [
            'avg_goals_for_home', 'avg_goals_for_away',
            'avg_goals_against_home', 'avg_goals_against_away',
            'attack_form_5_home', 'attack_form_5_away',
            'defense_form_5_home', 'defense_form_5_away',
            'days_since_last_home', 'days_since_last_away',
        ]:
            if c in df_feat.columns:
                val = df_feat[c].fillna(0).values.astype(np.float32)
            else:
                val = np.zeros(len(df_feat), dtype=np.float32)
            gt_cols.append(val)

        return np.column_stack(gt_cols)

    # ══════════ [NEW] Referee 代理特征 (6维) ══════════

    def _build_referee_proxy_features(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """
        裁判影响代理特征 — 由于无真实裁判数据，使用联赛级别和比赛特征作为代理:

        维度说明:
          [0] league_strictness: 联赛平均严格度 (联赛编码映射)
          [1] home_advantage_league: 该联赛的主场优势强度
          [2] elo_gap_signal: ELO差距越大→比赛越激烈→判罚影响越大
          [3] attack_clash: 双方攻击力的碰撞程度 (高碰撞=多判罚机会)
          [4] draw_suspect: 平局可疑度 (实力接近+低进球预期→需关注判罚)
          [5] volatility_proxy: 比赛不确定性代理 (用于判断判罚影响力放大)
        """
        n = len(df_feat)
        ref_cols = []

        # 0. 联赛严格度代理 (基于已知联赛特征)
        if 'league' in df_raw.columns:
            strict_map = {
                'Premier League': 0.55,
                'La Liga': 0.52,
                'Serie A': 0.60,
                'Bundesliga': 0.45,
                'Ligue 1': 0.48,
                'Championship': 0.50,
            }
            league_strict = df_raw['league'].map(
                lambda x: strict_map.get(x, 0.50) if isinstance(x, str) else 0.50
            ).fillna(0.50).values.astype(np.float32)
        else:
            league_strict = np.full(n, 0.50, dtype=np.float32)
        ref_cols.append(league_strict)

        # 1. 主场优势强度 (联赛级别 + ELO差距调节)
        if 'league' in df_raw.columns:
            ha_map = {
                'Premier League': 0.42,
                'La Liga': 0.44,
                'Serie A': 0.46,
                'Bundesliga': 0.38,
                'Ligue 1': 0.40,
                'Championship': 0.41,
            }
            ha_base = df_raw['league'].map(lambda x: ha_map.get(x, 0.42) if isinstance(x, str) else 0.42).fillna(0.42).values.astype(np.float32)
        else:
            ha_base = np.full(n, 0.42, dtype=np.float32)

        if 'elo_diff' in df_feat.columns:
            elo_mod = np.abs(df_feat['elo_diff'].fillna(0).values.astype(np.float32)) / 500.0
            ha_adj = ha_base * (1.0 - 0.3 * np.clip(elo_mod, 0, 1))  # ELO差距大→主场优势减弱
        else:
            ha_adj = ha_base
        ref_cols.append(ha_adj)

        # 2. ELO差距信号 (归一化到 [0,1])
        if 'elo_diff' in df_feat.columns:
            gap_sig = np.clip(np.abs(df_feat['elo_diff'].fillna(0).values.astype(np.float32)) / 300.0, 0, 1)
        else:
            gap_sig = np.full(n, 0.30, dtype=np.float32)
        ref_cols.append(gap_sig)

        # 3. 进攻碰撞度 (双方攻击强度的乘积, 高值=开放比赛=更多争议判罚)
        atk_h = df_feat.get('attack_strength', pd.Series(np.ones(n)))
        atk_a = df_feat.get('away_attack_strength', atk_h)
        clash = atk_h.fillna(1.0).values.astype(np.float32) * atk_a.fillna(1.0).values.astype(np.float32)
        clash = np.clip(clash / 4.0, 0, 2).astype(np.float32)  # 归一化
        ref_cols.append(clash)

        # 4. 平局可疑度 (实力接近 + 低进球预期)
        if 'elo_diff' in df_feat.columns:
            close = np.exp(-np.abs(df_feat['elo_diff'].fillna(0).values.astype(np.float32)) / 80.0)
        else:
            close = np.full(n, 0.50, dtype=np.float32)

        if 'poisson_home_goals' in df_feat.columns and 'poisson_away_goals' in df_feat.columns:
            ph = df_feat['poisson_home_goals'].fillna(1.3).values.astype(np.float32)
            pa = df_feat['poisson_away_goals'].fillna(1.1).values.astype(np.float32)
            low_score = np.where((ph + pa) < 2.5, 1.0, 0.3).astype(np.float32)
        else:
            low_score = np.full(n, 0.50, dtype=np.float32)

        suspect = (close * low_score).astype(np.float32)
        ref_cols.append(suspect)

        # 5. 不确定性代理 (用于衡量判罚影响力的"杠杆效应")
        if 'std_goals_for' in df_feat.columns:
            vol = df_feat['std_goals_for'].fillna(1.0).values.astype(np.float32)
        elif 'home_std_goals_for' in df_feat.columns:
            hvol = df_feat['home_std_goals_for'].fillna(1.0).values.astype(np.float32)
            avol = df_feat.get('away_std_goals_for', pd.Series(np.ones(n))).fillna(1.0).values.astype(np.float32)
            vol = (hvol + avol) / 2.0
        else:
            vol = np.full(n, 1.2, dtype=np.float32)
        ref_cols.append(np.clip(vol, 0.3, 3.0).astype(np.float32))

        return np.column_stack(ref_cols)

    # ══════════ [NEW] Coach 代理特征 (8维) ══════════

    def _build_coach_proxy_features(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """
        教练战术代理特征 — 使用球队身份作为风格代理:

        维度说明:
          [0] home_style_possession: 主队控球倾向 (从平均控球/传球推导)
          [1] away_style_pressing:   客队压迫倾向
          [2] home_consistency:      主队状态稳定性 (方差倒数)
          [3] away_consistency:      客队状态稳定性
          [4] tactical_matchup:      战术相克指数 (攻击型vs防守型)
          [5] home_recent_trend:     主队近期趋势 (改善/恶化)
          [6] away_recent_trend:     客队近期趋势
          [7] coach_experience:      经验代理 (球队历史总场次排名)
        """
        n = len(df_feat)
        coach_cols = []

        # 0/1. 球队风格代理 (从进球数据推断: 多进球=进攻型, 少失球=防守型)
        for prefix in ['home_', 'away_']:
            gf_col = f'{prefix}avg_goals_for'
            ga_col = f'{prefix}avg_goals_against'
            gf = df_feat.get(gf_col, pd.Series(np.ones(n) * 1.4)).fillna(1.4).values.astype(np.float32)
            ga = df_feat.get(ga_col, pd.Series(np.ones(n) * 1.2)).fillna(1.2).values.astype(np.float32)

            # 攻击倾向 = 进球能力 / (进球+失球)
            offensive = gf / (gf + ga + 0.01)
            offensive = np.clip(offensive, 0.25, 0.75).astype(np.float32)
            coach_cols.append(offensive)

        # 2/3. 状态稳定性 (近5场标准差代理 — 方差小=稳定)
        for side in ['home', 'away']:
            pts_5 = df_raw.get(f'{side}_last_5_points')
            if pts_5 is not None:
                # 用5场积分范围作为稳定性指标 (range小=稳定)
                vals = pts_5.fillna(6).values.astype(np.float32)
                stability = 1.0 / (1.0 + np.abs(vals - 6.0) / 6.0)  # 接近6分=稳定
            else:
                stability = np.full(n, 0.60, dtype=np.float32)
            coach_cols.append(stability)

        # 4. 战术相克 (主队攻击性 - 客队攻击性: 差异大→一方占优明显)
        if len(coach_cols) >= 2:
            matchup = coach_cols[0] - coach_cols[1]  # 正=主攻客守, 负=主守客攻
        else:
            matchup = np.zeros(n, dtype=np.float32)
        coach_cols.append(matchup.astype(np.float32))

        # 5/6. 近期趋势 (近5场 vs 近20场胜率变化)
        for side in ['home', 'away']:
            w5 = df_raw.get(f'{side}_last_5_wins')
            w20 = df_raw.get(f'{side}_last_20_wins')
            if w5 is not None and w20 is not None:
                w5v = w5.fillna(2).values.astype(np.float32)
                w20v = w20.fillna(8).values.astype(np.float32)
                rate5 = w5v / 5.0
                rate20 = w20v / 20.0
                trend = (rate5 - rate20) * 3.0  # 放大信号
                trend = np.clip(trend, -0.5, 0.5).astype(np.float32)
            else:
                trend = np.zeros(n, dtype=np.float32)
            coach_cols.append(trend)

        # 7. 经验代理 (用ELO绝对值近似: 高ELO=传统强队=经验丰富)
        if 'home_elo' in df_feat.columns:
            he = df_feat['home_elo'].fillna(1300).values.astype(np.float32)
            ae = df_feat.get('away_elo', df_feat['home_elo']).fillna(1300).values.astype(np.float32)
            exp_proxy = (he + ae) / 2000.0  # 归一化到 ~1.3
        else:
            exp_proxy = np.full(n, 1.3, dtype=np.float32)
        coach_cols.append(np.clip(exp_proxy, 0.5, 2.0).astype(np.float32))

        return np.column_stack(coach_cols)

    # ══════════ [NEW] TimeSpace 特征 (8维) ══════════

    def _build_timespace_features(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """
        时空断裂带特征 — 从 date 列计算:

        维度说明:
          [0] home_rest_days:     主队休整天数 (距上一场比赛)
          [1] away_rest_days:     客队休整天数
          [2] rest_disparity:     休整差距 (正=主队更有利)
          [3] home_match_density: 主队近7天比赛密度 (0~3+)
          [4] away_match_density: 客队近7天比赛密度
          [5] fatigue_imbalance:  疲劳不平衡度 (|density_h - density_a|)
          [6] day_of_week:        比赛星期 (周一=0, 周日=6, 影响备战时间)
          [7] is_weekend:         是否周末 (周末通常轮换更积极)
        """
        n = len(df_raw)
        ts_cols = []

        # 解析日期
        dates = pd.to_datetime(df_raw['date'], errors='coerce')

        # 0/1. 休整天数 (用同球队前一场比赛日期计算)
        for side in ['home', 'away']:
            team_col = f'{side}_team'
            if team_col in df_raw.columns:
                rest_days = self._compute_rest_days(dates, df_raw[team_col])
            else:
                rest_days = np.full(n, 7.0, dtype=np.float32)
            ts_cols.append(rest_days)

        # 2. 休整差距
        if len(ts_cols) >= 2:
            rest_disp = ts_cols[0] - ts_cols[1]
        else:
            rest_disp = np.zeros(n, dtype=np.float32)
        ts_cols.append(np.clip(rest_disp, -10, 10).astype(np.float32))

        # 3/4. 比赛密度 (近7天内同队比赛数)
        for side in ['home', 'away']:
            team_col = f'{side}_team'
            if team_col in df_raw.columns:
                density = self._compute_match_density(dates, df_raw[team_col], window_days=7)
            else:
                density = np.full(n, 1.0, dtype=np.float32)
            ts_cols.append(density)

        # 5. 疲劳不平衡
        if len(ts_cols) >= 4:
            fat_imb = np.abs(ts_cols[3] - ts_cols[4])
        else:
            fat_imb = np.zeros(n, dtype=np.float32)
        ts_cols.append(fat_imb.astype(np.float32))

        # 6. 星期几
        dow = dates.dt.dayofweek.fillna(3).values.astype(np.float32) / 6.0  # 归一化 [0,1]
        ts_cols.append(dow)

        # 7. 是否周末 (周六日=1, 其他=0)
        weekend = (dates.dt.dayofweek >= 4).astype(np.float32)
        ts_cols.append(weekend.values)

        return np.column_stack(ts_cols)

    def _compute_rest_days(self, dates: pd.Series, teams: pd.Series) -> np.ndarray:
        """计算每场比赛中该球队的休整天数"""
        n = len(dates)
        rest = np.full(n, 7.0, dtype=np.float32)

        # 构建全局 (team, date) 索引
        df_temp = pd.DataFrame({'date': dates, 'team': teams})
        df_temp = df_temp.sort_values(['team', 'date'])
        df_temp['prev_date'] = df_temp.groupby('team')['date'].shift(1)

        # 计算间隔
        delta = (df_temp['date'] - df_temp['prev_date']).dt.days
        delta = delta.fillna(14.0).clip(lower=0, upper=60).astype(np.float32)
        df_temp['rest'] = delta

        # 还原原顺序
        rest_arr = df_temp.sort_index()['rest'].values.copy()
        # 处理异常值
        rest_arr = np.where(rest_arr > 21, 7.0, rest_arr)  # >21天视为赛季初
        rest_arr = np.where((rest_arr < 1) & (rest_arr > 0), 1.0, rest_arr)
        rest_arr = np.maximum(rest_arr, 0.5)

        return rest_arr

    def _compute_match_density(self, dates: pd.Series, teams: pd.Series, window_days: int = 7) -> np.ndarray:
        """计算每支球队在指定窗口内的比赛密度 (向量化实现)"""
        n = len(dates)
        density = np.ones(n, dtype=np.float32)

        # 向量化计算：对每场比赛，统计同队在过去window_days内有多少场比赛
        df_temp = pd.DataFrame({
            'date': pd.to_datetime(dates, errors='coerce'),
            'team': teams.values,
            '_idx': range(n),
        })

        # 按队伍分组后，用 rolling merge 计算窗口内数量
        result_density = np.ones(n, dtype=np.float32)

        try:
            for team_name, group in df_temp.groupby('team', sort=False):
                if len(group) <= 1:
                    continue
                gidx = group['_idx'].values
                gdates = group['date'].values

                for i in range(len(gdates)):
                    if pd.isna(gdates[i]):
                        continue
                    win_start = gdates[i] - pd.Timedelta(days=window_days)
                    cnt = int(np.sum((gdates >= win_start) & (gdates <= gdates[i]))) - 1
                    result_density[gidx[i]] = max(float(cnt), 0.0)

        except (OSError, ValueError, KeyError) as e:
            logger.debug(f"操作失败: {e}")

        return np.clip(result_density, 0, 4)

    # ══════════ [v4-fix] Draw Signal 特征 (10维) ══════════

    def _build_draw_signal_features(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """
        平局信号专家特征 v2 — 10维平局专用特征 (修复+改进):

        [0] strength_parity_idx:    实力接近指数 (ELO差倒数的改进版, Gaussian衰减)
        [1] h2h_draw_rate_5:        历史平局率 (修复: 现在result列存在, 真正计算H2H)
        [2] draw_tendency_score:    平局倾向得分 (12联赛基准 + 攻防风格, 扩展联赛覆盖)
        [3] key_player_impact:      关键球员伤停影响代理 (积分骤降幅度)
        [4] recent_draw_count:      近期实际平局场次 (从points和wins算出, 替换天气代理)
        [5] form_volatility:        近期表现波动率 (std of PPG, 高波动=更多平局)
        [6] season_phase_draw_prob: 赛季阶段平局概率 (改进系数表)
        [7] recent_draw_trend:      近期平局趋势 (双方最近5场场均积分接近1.0的程度)
        [8] odds_balance_score:     赔率平衡度 (修复: 正确归一化H/D/A概率)
        [9] referee_draw_rate:      裁判历史平局率 (12联赛级别代理)
        """
        n = len(df_feat)
        ds = []

        # ── [0] 实力接近指数: Gaussian衰减版, 比倒数更平滑 ──
        if 'elo_diff' in df_feat.columns:
            elo_abs = np.abs(df_feat['elo_diff'].fillna(0).values.astype(np.float64))
            parity = np.exp(- (elo_abs ** 2) / (2.0 * 120 ** 2))  # sigma=120: 差120→0.607, 差200→0.25
            ds.append(parity.astype(np.float32))
        else:
            ds.append(np.full(n, 0.5, dtype=np.float32))

        # ── [1] H2H历史平局率 (已修复: result列通过score计算) ──
        h2h_dr = self._compute_h2h_draw_rate(df_raw, df_feat)
        ds.append(h2h_dr)

        # ── [2] 平局倾向得分 (12联赛基准 + 风格) ──
        draw_ten = self._compute_draw_tendency_v2(df_raw, df_feat)
        ds.append(draw_ten)

        # ── [3] 关键球员伤停影响代理 (积分骤降) ──
        key_imp = self._compute_key_player_impact_proxy(df_raw, df_feat)
        ds.append(key_imp)

        # ── [4] 近期实际平局场次 (替换原来的天气代理) ──
        draw_cnt = self._compute_recent_draw_count(df_raw, df_feat)
        ds.append(draw_cnt)

        # ── [5] 近期表现波动率 (替换原来的压力指数) ──
        volatility = self._compute_form_volatility(df_raw, df_feat)
        ds.append(volatility)

        # ── [6] 赛季阶段平局概率 ──
        season_phase = self._compute_season_phase_draw(df_raw)
        ds.append(season_phase)

        # ── [7] 近期平局趋势 (双方最近5场"接近平局") ──
        recent_dt = self._compute_recent_draw_trend(df_raw, df_feat)
        ds.append(recent_dt)

        # ── [8] 赔率平衡度 (修复: 正确归一化) ──
        odds_bal = self._compute_odds_balance_score_v2(df_feat)
        ds.append(odds_bal)

        # ── [9] 裁判历史平局率 (12联赛) ──
        ref_dr = self._compute_referee_draw_rate_v2(df_raw, df_feat)
        ds.append(ref_dr)

        return np.column_stack(ds)

    # ---- 各特征的子计算方法 ----

    def _compute_h2h_draw_rate(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame = None) -> np.ndarray:
        """[1] v4-fix: 计算两队最近N次交锋中的平局率
        
        关键修复: result列不在原始CSV中, 需要使用 df_feat 中的 result (由home_score/away_score计算)
        """
        n = len(df_raw)
        result = np.full(n, 0.25, dtype=np.float32)  # 默认25%

        # 优先使用 df_feat (它有 result 列), 其次 df_raw
        df_src = df_feat if (df_feat is not None and 'result' in df_feat.columns) else df_raw

        if 'home_team' not in df_raw.columns or 'away_team' not in df_raw.columns:
            return result
        if 'result' not in df_src.columns:
            return result

        # 构建全局比赛记录用于H2H查找
        df_temp = df_raw[['home_team', 'away_team', 'date']].copy()
        df_temp['result'] = df_src['result'].values  # [v4-fix] 从 df_feat 取 result
        df_temp['_orig_idx'] = np.arange(n)  # ★ F2 修复：保存原始行号映射
        df_temp['date'] = pd.to_datetime(df_temp['date'], errors='coerce')
        df_temp = df_temp.sort_values('date').reset_index(drop=True)

        # 对每场比赛, 查找之前所有该组合的历史交锋
        h2h_history = {}  # (team_a_sorted, team_b_sorted) -> list of results
        sorted_results = np.full(n, 0.25, dtype=np.float32)  # 排序后的结果

        for idx in range(len(df_temp)):
            row = df_temp.iloc[idx]
            t_home = str(row['home_team'])
            t_away = str(row['away_team'])
            pair = tuple(sorted([t_home, t_away]))

            if pair not in h2h_history:
                h2h_history[pair] = []
            else:
                # 统计前N场的平局率
                past = h2h_history[pair][-5:]  # 最近5次
                if len(past) > 0:
                    draws = sum(1 for r in past if r == 1)
                    sorted_results[idx] = draws / len(past)

            h2h_history[pair].append(int(row['result']))

        # ★ F2 修复：从排序顺序映射回原始行号
        for sorted_idx in range(n):
            orig_idx = int(df_temp.iloc[sorted_idx]['_orig_idx'])
            result[orig_idx] = sorted_results[sorted_idx]

        return result

    def _compute_draw_tendency_v2(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """[2] v4-fix: 平局倾向得分 — 12联赛基准 + 攻防风格因子"""
        n = len(df_feat)

        # 联赛级别基准平局率 (来自历史统计, 12联赛全覆盖)
        league_draw_rate = {
            'Premier League':               0.255,
            'La Liga':                      0.252,
            'Serie A':                      0.268,
            'Bundesliga':                   0.240,
            'Ligue 1':                      0.272,
            'Championship':                 0.248,
            'Primeira Liga':                0.258,
            'Eredivisie':                   0.247,
            "Campeonato Brasileiro Série A":0.263,
            'Champions League':             0.251,
            'European Championship':        0.270,
            'World Cup':                    0.265,
        }

        if 'league' in df_raw.columns:
            base_rate = df_raw['league'].map(
                lambda x: league_draw_rate.get(x, 0.260) if isinstance(x, str) else 0.260
            ).fillna(0.260).values.astype(np.float32)
        else:
            base_rate = np.full(n, 0.260, dtype=np.float32)

        # 风格因子: 双方平均期望进球越低 → 越保守 → 平局越高
        gf_h_col = df_feat.columns[df_feat.columns.str.contains('home_avg_goals_for')][0] if any(df_feat.columns.str.contains('home_avg_goals_for')) else None
        ga_h_col = df_feat.columns[df_feat.columns.str.contains('home_avg_goals_against')][0] if any(df_feat.columns.str.contains('home_avg_goals_against')) else None
        gf_a_col = df_feat.columns[df_feat.columns.str.contains('away_avg_goals_for')][0] if any(df_feat.columns.str.contains('away_avg_goals_for')) else None
        ga_a_col = df_feat.columns[df_feat.columns.str.contains('away_avg_goals_against')][0] if any(df_feat.columns.str.contains('away_avg_goals_against')) else None

        gf_h = df_feat[gf_h_col].fillna(1.4).values.astype(np.float32) if gf_h_col else np.full(n, 1.4, dtype=np.float32)
        ga_h = df_feat[ga_h_col].fillna(1.2).values.astype(np.float32) if ga_h_col else np.full(n, 1.2, dtype=np.float32)
        gf_a = df_feat[gf_a_col].fillna(1.3).values.astype(np.float32) if gf_a_col else np.full(n, 1.3, dtype=np.float32)
        ga_a = df_feat[ga_a_col].fillna(1.3).values.astype(np.float32) if ga_a_col else np.full(n, 1.3, dtype=np.float32)

        # 总进球期望越低 → 越保守 → 平局越高
        total_exp_goals = (gf_h + ga_h + gf_a + ga_a) / 4.0
        style_factor = np.where(total_exp_goals > 0.01, 2.5 / (total_exp_goals + 1.5), 1.0)
        style_factor = np.clip(style_factor, 0.65, 1.35).astype(np.float32)

        return (base_rate * style_factor).astype(np.float32)

    def _compute_key_player_impact_proxy(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """[3] 关键球员伤停影响: 用近5场vs近20场积分差异作为代理
        
        逻辑: 积分突然大幅下降 → 可能有关键球员缺阵 → 影响球队正常发挥 → 增加不确定性(可能增加平局)
        """
        n = len(df_feat)
        impact = np.zeros(n, dtype=np.float32)

        for side in ['home', 'away']:
            pts_5 = df_raw.get(f'{side}_last_5_points')
            pts_20 = df_raw.get(f'{side}_last_20_points')

            if pts_5 is not None and pts_20 is not None:
                p5 = pts_5.fillna(6).values.astype(np.float32)   # 近5场总积分
                p20 = pts_20.fillna(24).values.astype(np.float32)  # 近20场总积分

                # 近5场场均 vs 近20场场均 的比值 (比值低=近期状态下滑)
                rate_5_per = p5 / 5.0
                rate_20_per = p20 / 20.0
                drop_ratio = np.where(rate_20_per > 0.01, rate_5_per / rate_20_per, 1.0)
                drop_ratio = np.clip(drop_ratio, 0.2, 1.5)

                # 下滑幅度 (正值=下滑严重)
                side_drop = np.maximum(0, 1.0 - drop_ratio) * 3.0  # 放大
                impact += side_drop.astype(np.float32)
            else:
                impact += np.zeros(n, dtype=np.float32)

        # 双方影响的均值归一化到 [0, 1]
        return np.clip(impact / 2.0, 0, 1).astype(np.float32)

    def _compute_recent_draw_count(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """[4] v4-fix: 近期实际平局率 (从 last_N_points 和 last_N_wins 反算)
        
        逻辑: draw_count = total_points - 3*total_wins (每个平局=1分, 每个胜=3分)
              draw_rate = draw_count / window → 近期平局率
        """
        n = len(df_feat)
        all_rates = []

        for side in ['home', 'away']:
            for window in [5, 10]:
                pts_col = f'{side}_last_{window}_points'
                wins_col = f'{side}_last_{window}_wins'

                pts = df_raw.get(pts_col)
                win = df_raw.get(wins_col)

                if pts is not None and win is not None:
                    p = pts.fillna(window * 1.4).values.astype(np.float32)
                    w = win.fillna(window * 0.35).values.astype(np.float32)
                    raw_draws = np.maximum(0, p - 3.0 * w)  # 实际平局场次
                    rate = raw_draws / float(window)         # 平局率 [0, 1]
                    all_rates.append(rate)
                else:
                    all_rates.append(np.full(n, 0.25, dtype=np.float32))

        # 4个rate取平均 (home_5, home_10, away_5, away_10)
        draw_rate = np.mean(all_rates, axis=0)
        return draw_rate.astype(np.float32)

    def _compute_form_volatility(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """[5] v4-fix: 近期表现波动率 (高波动=状态不稳定=平局增加)
        
        替换原 match_pressure_index (ELO衍生, 与主模型冗余)
        逻辑: 用 last_N_points 计算场均积分在不同窗口的差异 (delta PPG)
              如果 short-term PPG 与 long-term PPG 差距大 → 波动高
        """
        n = len(df_feat)
        volatility = np.zeros(n, dtype=np.float32)

        for side in ['home', 'away']:
            pts_5 = df_raw.get(f'{side}_last_5_points')
            pts_10 = df_raw.get(f'{side}_last_10_points')
            pts_20 = df_raw.get(f'{side}_last_20_points')

            if pts_5 is not None and pts_10 is not None and pts_20 is not None:
                ppg_5  = pts_5.fillna(6).values.astype(np.float32) / 5.0
                ppg_10 = pts_10.fillna(12).values.astype(np.float32) / 10.0
                ppg_20 = pts_20.fillna(24).values.astype(np.float32) / 20.0

                # 近期 vs 长期 PPG 的绝对差异
                sd = np.abs(ppg_5 - ppg_20)
                md = np.abs(ppg_10 - ppg_20)
                vol = (sd * 0.6 + md * 0.4)  # 偏向近期差异
                volatility += vol
            else:
                volatility += np.full(n, 0.3, dtype=np.float32)

        # 双方平均波动率
        return (volatility / 2.0).astype(np.float32)

    def _compute_season_phase_draw(self, df_raw: pd.DataFrame) -> np.ndarray:
        """[6] 赛季阶段平局概率
        
        规律 (基于英超等联赛数据):
          - 赛季初 (8-9月): 不确定性高 → 平局率偏高 (~27%)
          - 赛季中 (10-2月): 格局稳定 → 平局率基准 (~25%)
          - 赛季末 (3-5月): 保级/争冠压力 → 平局率下降 (~23%) 或上升 (保级平局)
        """
        dates = pd.to_datetime(df_raw['date'], errors='coerce')
        month = dates.dt.month.fillna(9).values.astype(np.int32)

        phase_map = {
            # 赛季初: 7-9月
            7: 1.08,  8: 1.07,  9: 1.05,
            # 赛季中: 10-2月
            10: 1.00, 11: 0.99, 12: 0.98,
             1: 0.98,  2: 1.00,
            # 赛季冲刺: 3-5月 (保级+争冠关键期)
             3: 1.03,  4: 1.06,  5: 1.09,
             6: 1.04,  # 少数联赛
        }
        phase_coeff = np.array([phase_map.get(m, 1.0) for m in month], dtype=np.float32)
        return phase_coeff

    def _compute_recent_draw_trend(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """[7] 近期平局趋势 — 双方最近3场的"接近平局"比例
        
        定义 "接近平局": 单场积分在 0~3 分范围内且进球差距<=1 (即1-0/0-1/1-1/0-0)
        用 last_5_points 推断: 单场积分 ≈ 总分/5, 但这里我们用更精细的方法
        退而求其次: 用近5场胜率推断 (胜率中等偏下 → 更多平局/小负)
        """
        n = len(df_feat)
        trend = np.zeros(n, dtype=np.float32)

        for side in ['home', 'away']:
            pts_5 = df_raw.get(f'{side}_last_5_points')
            wins_5 = df_raw.get(f'{side}_last_5_wins')

            if pts_5 is not None and wins_5 is not None:
                pts = pts_5.fillna(6).values.astype(np.float32)
                wins = wins_5.fillna(2).values.astype(np.float32)

                # 平局场次估算: 总积分 - 3*胜场 ≈ 平场*1 + 负场*0 → 不够精确
                # 更好方法: 场均积分接近1.0 → 偏向平局
                avg_pts = pts / 5.0
                # avg_pts near 1.0 = 多平局; avg_pts high = 多胜; avg_pts low = 多负
                draw_like = 1.0 - np.abs(avg_pts - 1.0)  # 接近1时draw_like高
                draw_like = np.clip(draw_like, 0, 1)
                trend += draw_like.astype(np.float32)
            else:
                trend += np.full(n, 0.33, dtype=np.float32)

        # 双方平均
        return (trend / 2.0).astype(np.float32)

    def _compute_odds_balance_score_v2(self, df_feat: pd.DataFrame) -> np.ndarray:
        """[8] v4-fix: 赔率平衡度 — 修复p_h/p_d/p_a归一化逻辑
        
        原Bug: p_a = 1.0 - p_h 后被 lam_a/lam_h 缩放, 概率和≠1
        修复: 使用ELO推导的 p_h, 对称求 p_a, 再用Poisson修正p_d后重新归一化
        """
        n = len(df_feat)

        # 从ELO推导主胜概率
        if 'home_win_prob' in df_feat.columns:
            p_h = df_feat['home_win_prob'].fillna(0.45).values.astype(np.float64)
        elif 'elo_diff' in df_feat.columns:
            elo_diff = df_feat['elo_diff'].fillna(0).values.astype(np.float64)
            p_h = 1.0 / (1.0 + 10.0 ** (elo_diff / (-400.0)))
        else:
            p_h = np.full(n, 0.45, dtype=np.float64)

        # 对称主客胜 — ★ C4 加固：p_h 上界保护防 p_a 负值
        p_h = np.clip(p_h, 0.0, 1.0)
        p_a = 1.0 - p_h

        # Poisson估计平局概率
        if 'poisson_home_goals' in df_feat.columns and 'poisson_away_goals' in df_feat.columns:
            lam_h = df_feat['poisson_home_goals'].fillna(1.3).values.astype(np.float64)
            lam_a = df_feat['poisson_away_goals'].fillna(1.1).values.astype(np.float64)
            lam_sum = lam_h + lam_a
            lam_prod = lam_h * lam_a
            p_d = np.exp(-lam_sum) * np.array([self._i0(2 * math.sqrt(max(lp, 0))) for lp in lam_prod])
        else:
            p_d = np.full(n, 0.27, dtype=np.float64)

        # [v4-fix] 正确归一化: 给平局保底权重, 确保三者归一
        # p_h + p_d + p_a should sum to ~1 (p_h + p_a ≈ 1, p_d is extra)
        # 方案: 将 p_h, p_a 按比例缩小, 为 p_d 腾出空间
        total = p_h + p_a + p_d
        p_h = np.clip(p_h / total, 0.01, 0.99)
        p_d = np.clip(p_d / total, 0.01, 0.99)
        p_a = np.clip(p_a / total, 0.01, 0.99)

        # 香农熵 → 平衡度 (高熵 = 三方势均力敌 = 不确定性大 = 平局信号)
        eps = 1e-8
        entropy = -(p_h * np.log(p_h + eps) + p_d * np.log(p_d + eps) + p_a * np.log(p_a + eps))
        max_ent = math.log(3)
        balance = (entropy / max_ent).astype(np.float32)

        return balance

    def _compute_referee_draw_rate_v2(self, df_raw: pd.DataFrame, df_feat: pd.DataFrame) -> np.ndarray:
        """[9] v4-fix: 裁判历史平局率代理 — 12联赛全覆盖
        
        原Bug: 仅覆盖6个联赛, 巴西甲级/葡超/荷甲等未定义
        """
        n = len(df_feat)

        ref_draw_rate = {
            'Premier League':               0.250,
            'La Liga':                      0.252,
            'Serie A':                      0.270,
            'Bundesliga':                   0.238,
            'Ligue 1':                      0.260,
            'Championship':                 0.245,
            'Primeira Liga':                0.256,
            'Eredivisie':                   0.244,
            "Campeonato Brasileiro Série A":0.255,
            'Champions League':             0.248,
            'European Championship':        0.265,
            'World Cup':                    0.258,
        }

        if 'league' in df_raw.columns:
            rdr = df_raw['league'].map(
                lambda x: ref_draw_rate.get(x, 0.253) if isinstance(x, str) else 0.253
            ).fillna(0.253).values.astype(np.float32)
        else:
            rdr = np.full(n, 0.253, dtype=np.float32)

        # ELO差距调整: 差距小→裁判影响大→平局概率微增
        if 'elo_diff' in df_feat.columns:
            elo_mod = np.abs(df_feat['elo_diff'].fillna(0).values.astype(np.float32))
            adjustment = 1.0 + 0.05 * (1.0 - np.clip(elo_mod / 200.0, 0, 1))
            rdr = (rdr * adjustment).astype(np.float32)

        return rdr

    # ──────────── 批量获取全部特征 ────────────

    def transform(self, df: pd.DataFrame) -> Tuple[
        np.ndarray,
        np.ndarray,
        List[str],
        Dict[str, np.ndarray]
    ]:
        """一次性产出全部特征"""
        X_main, y, feat_names = self.prepare_main_features(df)
        expert_feats = self.prepare_expert_features(df)
        return X_main, y, feat_names, expert_feats

    # ──────────── 专家特征维度信息 (v2) ────────────

    @staticmethod
    def get_expert_dims() -> Dict[str, int]:
        """返回每个专家的特征维度 (v3 含平局信号)"""
        return {
            'trend':       19,   # 基础不变
            'alpha':       12,   # [v2] 7基础+3赔率衍生+2交叉
            'quant':       14,   # [v2] 8基础+3波动率+3动量加速度
            'goal_timing':  10,   # 不变
            'referee':       6,   # [NEW] 联赛严格度+主场优势+ELOgap+碰撞度+平局可疑+不确定
            'coach':         8,   # [NEW] 攻/守风格+稳定性×2+相克+趋势×2+经验
            'timespace':     8,   # [NEW] 休整×2+差距+密度×2+疲劳差+星期+周末
            'draw_signal':  10,   # [v3] 实力接近/H2H平局/风格/伤停/天气/压力/赛季阶段/趋势/赔率平衡/裁判
        }

    @staticmethod
    def get_all_expert_names() -> List[str]:
        """返回所有可用专家名列表 (阶段一+二+三)"""
        return ['trend', 'alpha', 'quant', 'goal_timing', 'referee', 'coach', 'timespace',
                'draw_signal']
