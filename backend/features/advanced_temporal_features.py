import logging
"""
时序安全的高级特征工程

本模块确保所有时间窗口特征只使用严格历史数据（shift + rolling），
杜绝数据泄漏，适用于时序交叉验证和实盘预测。

特征类别：
  1. 势头特征 (momentum)         — last_N_form, form_momentum_N
  2. 进攻/防守状态 (off/def)      — attack_form_N, defense_form_N, goal_diff_form_N
  3. 历史交锋 (head-to-head)      — h2h_home_win_rate, h2h_draw_rate, h2h_goal_ratio
  4. 疲劳度 (fatigue)              — days_since_last_home/away, home/away_fatigue
  5. 比赛上下文 (contextual)       — season_month, is_weekend, season_progress
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Optional
import warnings
logger = logging.getLogger(__name__)
warnings.filterwarnings('ignore')


class SafeTemporalFeatureEngineer:
    """时序安全特征工程 — 所有窗口特征均只用历史数据（shift(1) before rolling）

    设计为处理 **球队级别长表**，每行代表一支球队在一场比赛中的记录。
    调用方负责将 match-level 数据 (home_*/away_* 列) 展开为 team-level 长表。

    Parameters
    ----------
    df : pd.DataFrame
        球队级别数据，必须包含: team, date, home_team, away_team,
        home_score, away_score, goals_for, goals_against, goal_diff
    team_col : str
        球队列名 (默认 'team')
    date_col : str
        日期列名 (默认 'date')
    """

    def __init__(self, df: pd.DataFrame, team_col: str = 'team', date_col: str = 'date'):
        self.df = df.copy()
        self.team_col = team_col
        self.date_col = date_col
        self.df[self.date_col] = pd.to_datetime(self.df[self.date_col], errors='coerce')
        self.df = self.df.sort_values([self.team_col, self.date_col]).reset_index(drop=True)

    # ────────────────── 1. 势头特征 ──────────────────

    def create_momentum_features(self, windows: List[int] = None):
        """创建球队势头特征 — 使用 shift(1) 排除当前行"""
        if windows is None:
            windows = [3, 5, 10, 20]
        logger.info(f"[MOMENTUM] 为 {self.df[self.team_col].nunique()} 支球队创建势头特征...")

        for window in windows:
            # shift(1) 确保不使用当前比赛信息
            self.df[f'last_{window}_form'] = self.df.groupby(self.team_col)['points'].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).mean()
            )

            # 势头变化率 (最近3场的变化趋势)
            if window >= 5:
                self.df[f'form_momentum_{window}'] = self.df.groupby(self.team_col)[
                    f'last_{window}_form'
                ].transform(lambda x: x.diff(3) / 3)

        return self.df

    # ────────────────── 2. 进攻/防守状态 ──────────────────

    def create_offensive_defensive_form(self, windows: List[int] = None):
        """创建进攻/防守状态特征"""
        if windows is None:
            windows = [5, 10]
        logger.info("[OD_STATE] 创建进攻防守状态特征...")

        for window in windows:
            self.df[f'attack_form_{window}'] = self.df.groupby(self.team_col)['goals_for'].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).mean()
            )
            self.df[f'defense_form_{window}'] = self.df.groupby(self.team_col)['goals_against'].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).mean()
            )
            self.df[f'goal_diff_form_{window}'] = self.df.groupby(self.team_col)['goal_diff'].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).mean()
            )

        return self.df

    # ────────────────── 3. 历史交锋 ──────────────────

    def create_head_to_head_features(self, max_history: int = 10):
        """创建历史交锋特征 — 严格时序安全（只看比赛日期之前的交锋）"""
        logger.info("[H2H] 创建历史交锋特征...")

        h2h_stats = []

        for idx, match in self.df.iterrows():
            home = match['home_team']
            away = match['away_team']
            date = match[self.date_col]

            past_matches = self.df[
                (
                    (self.df['home_team'] == home) & (self.df['away_team'] == away) |
                    (self.df['home_team'] == away) & (self.df['away_team'] == home)
                ) &
                (self.df[self.date_col] < date)
            ].tail(max_history)

            if len(past_matches) > 0:
                home_wins = 0
                draws = 0
                home_goals = 0
                away_goals = 0

                for _, past in past_matches.iterrows():
                    if past['home_team'] == home:
                        if past['home_score'] > past['away_score']:
                            home_wins += 1
                        elif past['home_score'] == past['away_score']:
                            draws += 1
                        home_goals += past['home_score']
                        away_goals += past['away_score']
                    else:
                        if past['away_score'] > past['home_score']:
                            home_wins += 1
                        elif past['home_score'] == past['away_score']:
                            draws += 1
                        home_goals += past['away_score']
                        away_goals += past['home_score']

                h2h_stats.append({
                    'h2h_home_win_rate': home_wins / len(past_matches),
                    'h2h_draw_rate': draws / len(past_matches),
                    'h2h_goal_ratio': home_goals / (away_goals + 1e-6),
                    'h2h_match_count': len(past_matches),
                })
            else:
                h2h_stats.append({
                    'h2h_home_win_rate': 0.4,
                    'h2h_draw_rate': 0.25,
                    'h2h_goal_ratio': 1.0,
                    'h2h_match_count': 0,
                })

        h2h_df = pd.DataFrame(h2h_stats)
        self.df = pd.concat([self.df, h2h_df], axis=1)

        return self.df

    # ────────────────── 4. 疲劳度 ──────────────────

    def create_fatigue_features(self):
        """创建疲劳度特征 — 赛程密集度"""
        logger.info("[FATIGUE] 创建疲劳度特征...")

        self.df['days_since_last_home'] = self.df.groupby('home_team')[self.date_col].diff().dt.days
        self.df['days_since_last_away'] = self.df.groupby('away_team')[self.date_col].diff().dt.days

        self.df['days_since_last_home'] = self.df['days_since_last_home'].fillna(14)
        self.df['days_since_last_away'] = self.df['days_since_last_away'].fillna(14)

        self.df['home_fatigue'] = 1 / (self.df['days_since_last_home'] + 1e-6)
        self.df['away_fatigue'] = 1 / (self.df['days_since_last_away'] + 1e-6)

        return self.df

    # ────────────────── 5. 比赛上下文 ──────────────────

    def create_contextual_features(self):
        """创建比赛上下文特征"""
        logger.info("[CONTEXT] 创建上下文特征...")

        self.df['season_month'] = self.df[self.date_col].dt.month
        self.df['is_weekend'] = self.df[self.date_col].dt.dayofweek.isin([4, 5, 6]).astype(int)

        # 赛季天数 (如果 league 列存在)
        if 'league' in self.df.columns:
            self.df['season_day'] = self.df.groupby(
                ['league', self.df[self.date_col].dt.year]
            )[self.date_col].transform(
                lambda x: (x - x.min()).dt.days
            )
            self.df['season_progress'] = self.df.groupby(
                ['league', self.df[self.date_col].dt.year]
            )['season_day'].transform(lambda x: x / x.max() if x.max() > 0 else 0)
        else:
            self.df['season_progress'] = 0.5

        return self.df

    # ────────────────── 全量运行 ──────────────────

    def run_all(self):
        """运行全部特征工程流程"""
        self.create_momentum_features()
        self.create_offensive_defensive_form()
        self.create_head_to_head_features()
        self.create_fatigue_features()
        self.create_contextual_features()
        return self.df

    # ────────────────── 特征分类 ──────────────────

    def get_safe_features(self) -> List[str]:
        """获取所有时序安全的特征名称"""
        safe_features = []

        base_features = ['home_elo', 'away_elo', 'elo_diff']
        window_features = [col for col in self.df.columns if any(
            col.startswith(prefix) for prefix in
            ['last_', 'form_', 'attack_', 'defense_', 'goal_diff_']
        )]
        h2h_features = [col for col in self.df.columns if col.startswith('h2h_')]
        fatigue_features = ['home_fatigue', 'away_fatigue',
                            'days_since_last_home', 'days_since_last_away']
        context_features = ['season_month', 'is_weekend', 'season_progress']

        all_candidates = (base_features + window_features + h2h_features +
                          fatigue_features + context_features)
        safe_features = [f for f in all_candidates if f in self.df.columns]

        logger.info(f"[OK] 识别出 {len(safe_features)} 个时序安全特征")
        return safe_features

    def get_team_level_features(self) -> List[str]:
        """获取**球队级别**特征 (每个 team 不同的特征)"""
        team_features = []
        for col in self.df.columns:
            if any(col.startswith(p) for p in [
                'last_', 'form_momentum_', 'attack_form_',
                'defense_form_', 'goal_diff_form_',
            ]):
                team_features.append(col)
        return team_features

    def get_match_level_features(self) -> List[str]:
        """获取**比赛级别**特征 (同一场比赛两边相同的特征)"""
        match_features = []
        for col in self.df.columns:
            if any(col.startswith(p) for p in [
                'h2h_', 'days_since_last_', 'home_fatigue', 'away_fatigue',
                'season_', 'is_weekend',
            ]):
                match_features.append(col)
        return match_features
