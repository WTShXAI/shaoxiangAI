"""
哨响AI — 阶段一：数据增强与质量检查 (T14)
==========================================
基于3万场历史比赛数据，系统性地增强特征工程：
1. 数据质量全面分析
2. 多窗口滚动特征 (5/10/20 场)
3. ELO 球队评级系统
4. 泊松分布进球期望特征

使用方式:
    >>> from backend.data import load_matches_from_db, DataEnhancer
    >>> df = load_matches_from_db()
    >>> enhancer = DataEnhancer(df)
    >>> analysis = enhancer.analyze_data_quality()
    >>> enhancer.create_rolling_features([5, 10, 20])
    >>> enhancer.generate_team_rating_features()
    >>> enhancer.create_poisson_features()
    >>> enhancer.save_enhanced_data()
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import warnings
import logging

warnings.filterwarnings('ignore')
logger = logging.getLogger(__name__)


class DataEnhancer:
    """基于3万场比赛的数据增强器

    处理流程:
    1. 加载原始比赛数据 (matches_df)
    2. 分析数据质量 → analyze_data_quality()
    3. 生成滚动特征 → create_rolling_features()
    4. 计算 ELO 评级 → generate_team_rating_features()
    5. 计算泊松特征 → create_poisson_features()
    6. 保存增强数据 → save_enhanced_data()
    """

    def __init__(self, matches_df: pd.DataFrame):
        """
        Args:
            matches_df: 比赛数据，需包含以下列:
                date, home_team, away_team, home_score, away_score,
                以及由 loader 预处理生成的 team, goals_for, goals_against, is_win
        """
        self.df = matches_df.copy()
        self.enhanced_df = None
        self.match_count = len(matches_df)
        logger.info(f"加载 {self.match_count:,} 场历史比赛")
        logger.info(f"[DATA] 加载 {self.match_count:,} 场历史比赛")

        # 验证必需列
        required_cols = ['date', 'home_team', 'away_team', 'home_score', 'away_score']
        missing = [c for c in required_cols if c not in self.df.columns]
        if missing:
            raise ValueError(f"DataFrame 缺少必需列: {missing}")

    # ── 数据质量分析 ──────────────────────────────────────────

    def analyze_data_quality(self) -> Dict:
        """全面分析数据质量

        Returns:
            包含 summary / completeness / consistency / anomalies 的字典
        """
        logger.info("[QUALITY] 分析数据质量...")

        date_min = self.df['date'].min()
        date_max = self.df['date'].max()

        analysis = {
            "summary": {
                "total_matches": len(self.df),
                "date_range": {
                    "start": str(date_min),
                    "end": str(date_max),
                    "years_span": round(
                        (date_max - date_min).days / 365.25, 2
                    ) if hasattr(date_min, 'days') else "N/A",
                },
                "leagues_count": (
                    self.df['league'].nunique()
                    if 'league' in self.df.columns else 0
                ),
                "teams_count": (
                    pd.concat([
                        self.df['home_team'],
                        self.df['away_team']
                    ]).nunique()
                ),
            },
            "completeness": {},
            "consistency": {},
            "anomalies": [],
        }

        # 计算数据完整性
        for col in ['home_team', 'away_team', 'home_score', 'away_score', 'date']:
            if col not in self.df.columns:
                continue
            missing = self.df[col].isnull().sum()
            analysis['completeness'][col] = {
                'missing': int(missing),
                'missing_pct': round(missing / len(self.df) * 100, 2),
            }

        # 检测异常比分
        if 'home_score' in self.df.columns and 'away_score' in self.df.columns:
            high_scores = self.df[
                (self.df['home_score'] > 10) | (self.df['away_score'] > 10)
            ]
            if len(high_scores) > 0:
                analysis['anomalies'].append({
                    'type': 'unusual_score',
                    'count': int(len(high_scores)),
                    'sample': high_scores.head(3)[
                        ['date', 'home_team', 'away_team',
                         'home_score', 'away_score']
                    ].to_dict('records'),
                })

            # 检测负分/空分
            negative_scores = self.df[
                (self.df['home_score'] < 0) | (self.df['away_score'] < 0)
            ]
            if len(negative_scores) > 0:
                analysis['anomalies'].append({
                    'type': 'negative_score',
                    'count': int(len(negative_scores)),
                })

        # 日期一致性检查
        if 'date' in self.df.columns:
            future_dates = self.df[self.df['date'] > datetime.now()]
            if len(future_dates) > 0:
                analysis['anomalies'].append({
                    'type': 'future_dates',
                    'count': int(len(future_dates)),
                    'sample_dates': future_dates['date'].head(5).astype(str).tolist(),
                })

        # 打印摘要
        logger.info(f"  └─ 总比赛: {analysis['summary']['total_matches']:,}")
        logger.info(f"  └─ 联赛数: {analysis['summary']['leagues_count']}")
        logger.info(f"  └─ 球队数: {analysis['summary']['teams_count']:,}")
        logger.info(f"  └─ 异常发现: {len(analysis['anomalies'])} 类")
        for anomaly in analysis['anomalies']:
            logger.info(f"     [!] {anomaly['type']}: {anomaly['count']} 条")

        return analysis

    # ── 滚动窗口特征 ──────────────────────────────────────────

    def create_rolling_features(
        self, window_sizes: List[int] = None
    ) -> pd.DataFrame:
        """创建滚动窗口特征

        将每场比赛"展开"为每支球队的视角，按时间排序后计算：
        - last_{w}_wins: 近 w 场胜场数
        - last_{w}_goals_for: 近 w 场场均进球
        - last_{w}_goals_against: 近 w 场场均失球

        Args:
            window_sizes: 窗口大小列表，默认 [5, 10, 20]

        Returns:
            含滚动特征的 DataFrame
        """
        if window_sizes is None:
            window_sizes = [5, 10, 20]

        logger.info(f"[ROLLING] 为 {self.match_count:,} 场比赛创建滚动特征 (窗口: {window_sizes})...")

        # ── 将比赛数据按球队视角展开 ──
        # 每场比赛拆为两行: 主队视角 + 客队视角
        home_view = self.df.rename(columns={
            'home_team': 'team',
            'away_team': 'opponent',
            'home_score': 'goals_for',
            'away_score': 'goals_against',
        }).copy()
        home_view['is_home'] = True
        home_view['is_win'] = (
            home_view['goals_for'] > home_view['goals_against']
        ).astype(int)
        home_view['is_draw'] = (
            home_view['goals_for'] == home_view['goals_against']
        ).astype(int)
        home_view['points'] = home_view.apply(
            lambda r: 3 if r['goals_for'] > r['goals_against']
            else (1 if r['goals_for'] == r['goals_against'] else 0),
            axis=1,
        )

        away_view = self.df.rename(columns={
            'away_team': 'team',
            'home_team': 'opponent',
            'away_score': 'goals_for',
            'home_score': 'goals_against',
        }).copy()
        away_view['is_home'] = False
        away_view['is_win'] = (
            away_view['goals_for'] > away_view['goals_against']
        ).astype(int)
        away_view['is_draw'] = (
            away_view['goals_for'] == away_view['goals_against']
        ).astype(int)
        away_view['points'] = away_view.apply(
            lambda r: 3 if r['goals_for'] > r['goals_against']
            else (1 if r['goals_for'] == r['goals_against'] else 0),
            axis=1,
        )

        # 合并两个视角，按球队+日期排序
        expanded = pd.concat([home_view, away_view], ignore_index=True)
        expanded = expanded.sort_values(['team', 'date']).reset_index(drop=True)

        logger.info(f"  └─ 展开为 {len(expanded):,} 条球队-比赛记录")

        # ── 按球队分组计算滚动统计 ──
        for window in window_sizes:
            group = expanded.groupby('team')

            # 近 N 场胜场数
            expanded[f'last_{window}_wins'] = group['is_win'].transform(
                lambda x: x.shift(1).rolling(window, min_periods=3).sum()
            )
            # 近 N 场场均进球
            expanded[f'last_{window}_goals_for'] = group['goals_for'].transform(
                lambda x: x.shift(1).rolling(window, min_periods=3).mean()
            )
            # 近 N 场场均失球
            expanded[f'last_{window}_goals_against'] = group['goals_against'].transform(
                lambda x: x.shift(1).rolling(window, min_periods=3).mean()
            )
            # 近 N 场场均得分
            expanded[f'last_{window}_points'] = group['points'].transform(
                lambda x: x.shift(1).rolling(window, min_periods=3).mean()
            )

        # ── 合并回原始比赛格式 ──
        # 从展开数据中取主队和客队各自的特征
        home_features = expanded[expanded['is_home']].copy()
        away_features = expanded[~expanded['is_home']].copy()

        # 确保对齐（按原始索引）
        home_features = home_features.set_index(self.df.index)
        away_features = away_features.set_index(self.df.index)

        for window in window_sizes:
            for feat in ['wins', 'goals_for', 'goals_against', 'points']:
                col = f'last_{window}_{feat}'
                self.df[f'home_{col}'] = home_features[col]
                self.df[f'away_{col}'] = away_features[col]

        # 计算差值特征
        for window in window_sizes:
            self.df[f'diff_last_{window}_wins'] = (
                self.df[f'home_last_{window}_wins']
                - self.df[f'away_last_{window}_wins']
            )
            self.df[f'diff_last_{window}_goals_for'] = (
                self.df[f'home_last_{window}_goals_for']
                - self.df[f'away_last_{window}_goals_for']
            )

        feature_count = len(window_sizes) * 4 * 2  # home + away for each
        logger.info(f"  [+] 生成了 {feature_count} 个滚动特征")

        return self.df

    # ── ELO 球队评级 ──────────────────────────────────────────

    def generate_team_rating_features(
        self, initial_elo: float = 1500.0, k_factor: float = 20.0
    ) -> pd.DataFrame:
        """基于 ELO 原理的球队评级系统

        遍历所有历史比赛，按时间顺序更新每支球队的 ELO 评分。
        主客场优势自动计入 (主场 +100 ELO)。

        Args:
            initial_elo: 初始 ELO，默认 1500
            k_factor:   ELO 更新系数，默认 20

        Returns:
            含 home_elo / away_elo / home_elo_updated / away_elo_updated 的 DataFrame
        """
        logger.info(f"[ELO] 为 {pd.concat([self.df['home_team'], self.df['away_team']]).nunique()} 支球队计算 ELO 评级...")

        elo_ratings = defaultdict(lambda: float(initial_elo))
        home_advantage = 100.0  # 主场 ELO 加成

        elo_history: List[Dict] = []

        # 按日期排序
        sorted_df = self.df.sort_values('date').reset_index(drop=True)

        for idx, match in sorted_df.iterrows():
            home_team = match['home_team']
            away_team = match['away_team']
            home_score = int(match['home_score'])
            away_score = int(match['away_score'])

            # 当前 ELO (含主场加成)
            R_home = elo_ratings[home_team] + home_advantage
            R_away = elo_ratings[away_team]

            # 期望胜率
            E_home = 1.0 / (1.0 + 10.0 ** ((R_away - R_home) / 400.0))
            E_away = 1.0 - E_home

            # 实际结果
            if home_score > away_score:
                S_home, S_away = 1.0, 0.0
            elif home_score < away_score:
                S_home, S_away = 0.0, 1.0
            else:
                S_home, S_away = 0.5, 0.5

            # 进球差调整 K 因子 (大比分更影响 ELO)
            goal_diff = abs(home_score - away_score)
            adjusted_k = k_factor
            if goal_diff == 2:
                adjusted_k = k_factor * 1.5
            elif goal_diff == 3:
                adjusted_k = k_factor * 1.75
            elif goal_diff >= 4:
                adjusted_k = k_factor * (1.75 + (goal_diff - 3) * 0.25)

            # 更新 ELO
            elo_ratings[home_team] = R_home + adjusted_k * (S_home - E_home) - home_advantage
            elo_ratings[away_team] = R_away + adjusted_k * (S_away - E_away)

            elo_history.append({
                'match_idx': idx,
                'home_elo': round(R_home, 1),
                'away_elo': round(R_away, 1),
                'home_elo_updated': round(elo_ratings[home_team], 1),
                'away_elo_updated': round(elo_ratings[away_team], 1),
                'elo_diff': round(R_home - R_away, 1),
                'home_win_prob': round(E_home, 4),
            })

        # 合并 ELO 特征
        elo_df = pd.DataFrame(elo_history)
        sorted_df = pd.concat(
            [sorted_df.reset_index(drop=True), elo_df.reset_index(drop=True)],
            axis=1,
        )

        # 保存回主 DataFrame
        self.df = sorted_df.sort_index() if hasattr(self.df.index, 'name') else sorted_df

        # 清理重复列
        self._deduplicate_columns()

        # 确保 home_elo 是单列（处理可能的残留重复）
        home_elo_col = self._safe_col('home_elo')
        elo_min = float(home_elo_col.min())
        elo_max = float(home_elo_col.max())
        logger.info(f"  [+] ELO 范围: {elo_min:.0f} - {elo_max:.0f}")

        # 打印 TOP 球队
        top_teams = sorted(
            elo_ratings.items(), key=lambda x: x[1], reverse=True
        )[:10]
        logger.info(f"  [TOP] Top 10 ELO:")
        for team, elo in top_teams:
            logger.info(f"     {team}: {elo:.0f}")

        return self.df

    # ── 泊松特征 ──────────────────────────────────────────────

    def _deduplicate_columns(self):
        """移除 DataFrame 中的重复列，只保留每个列名的第一次出现。"""
        dup_cols = [c for c in self.df.columns if self.df.columns.tolist().count(c) > 1]
        if dup_cols:
            seen = set()
            keep_idx = []
            for i, col in enumerate(self.df.columns):
                if col not in seen:
                    seen.add(col)
                    keep_idx.append(i)
            self.df = self.df.iloc[:, keep_idx]

    def _safe_col(self, col_name: str) -> pd.Series:
        """安全获取列，处理可能的重复列名（取第一列）。"""
        col = self.df[col_name]
        if isinstance(col, pd.DataFrame):
            col = col.iloc[:, 0]
        return col

    def create_poisson_features(self) -> pd.DataFrame:
        """创建泊松分布相关特征

        计算每支球队的:
        - avg_goals_for / avg_goals_against : 场均进/失球
        - attack_strength / defense_strength  : 攻防强度 (vs 联赛平均)
        - poisson_home_goals / poisson_away_goals : 泊松期望进球

        Returns:
            含泊松特征的 DataFrame
        """
        logger.info("[POISSON] 计算泊松分布特征...")

        # ── 先清理可能存在的重复列 ──
        self._deduplicate_columns()

        # ── 展开为球队视角 ──
        home_stats = self.df[['home_team', 'home_score', 'away_score', 'league']].copy()
        home_stats.columns = ['team', 'goals_for', 'goals_against', 'league']

        away_stats = self.df[['away_team', 'away_score', 'home_score', 'league']].copy()
        away_stats.columns = ['team', 'goals_for', 'goals_against', 'league']

        all_stats = pd.concat([home_stats, away_stats], ignore_index=True)

        # 联赛平均进球
        if 'league' in self.df.columns and self.df['league'].nunique() > 1:
            league_avg = all_stats.groupby('league')['goals_for'].mean().to_dict()
            league_avg_global = all_stats['goals_for'].mean()
        else:
            league_avg_global = all_stats['goals_for'].mean()
            league_avg = {}

        # ── 球队统计 ──
        team_stats = all_stats.groupby('team').agg(
            avg_goals_for=('goals_for', 'mean'),
            avg_goals_against=('goals_against', 'mean'),
            std_goals_for=('goals_for', 'std'),
            std_goals_against=('goals_against', 'std'),
            matches_played=('goals_for', 'count'),
        ).round(3)

        # 攻防强度 (vs 联赛平均)
        if league_avg:
            # 按联赛计算强度
            team_league = all_stats.groupby('team')['league'].agg(lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else x.iloc[0])
            team_stats['league'] = team_league
            team_stats['league_avg_goals'] = team_stats['league'].map(league_avg).fillna(league_avg_global)
        else:
            team_stats['league_avg_goals'] = league_avg_global

        team_stats['attack_strength'] = (
            team_stats['avg_goals_for'] / team_stats['league_avg_goals']
        ).round(3)
        team_stats['defense_strength'] = (
            team_stats['avg_goals_against'] / team_stats['league_avg_goals']
        ).round(3)

        # ── 合并到主数据 ──
        # 主队特征
        self.df = self.df.merge(
            team_stats[['avg_goals_for', 'avg_goals_against',
                        'attack_strength', 'defense_strength',
                        'std_goals_for', 'std_goals_against']],
            left_on='home_team', right_index=True,
            suffixes=('', '_dup'),
        )
        # 重命名为主队前缀
        for col in ['avg_goals_for', 'avg_goals_against', 'attack_strength',
                     'defense_strength', 'std_goals_for', 'std_goals_against']:
            if col in self.df.columns:
                self.df.rename(columns={col: f'home_{col}'}, inplace=True)

        # 客队特征
        self.df = self.df.merge(
            team_stats[['avg_goals_for', 'avg_goals_against',
                        'attack_strength', 'defense_strength',
                        'std_goals_for', 'std_goals_against']],
            left_on='away_team', right_index=True,
            suffixes=('', '_dup2'),
        )
        for col in ['avg_goals_for', 'avg_goals_against', 'attack_strength',
                     'defense_strength', 'std_goals_for', 'std_goals_against']:
            if col in self.df.columns:
                self.df.rename(columns={col: f'away_{col}'}, inplace=True)

        # 合并后再去重一次（merge/rename 可能引入重复）
        self._deduplicate_columns()

        # ── 泊松期望进球 ──
        # 使用安全列访问避免重复列问题
        league_avg = self.df.get('league_avg_goals',
            team_stats['league_avg_goals'].iloc[0] if 'league_avg_goals' in team_stats.columns else league_avg_global)
        if isinstance(league_avg, pd.DataFrame):
            league_avg = league_avg.iloc[:, 0]

        h_attack = self._safe_col('home_attack_strength')
        a_defense = self._safe_col('away_defense_strength')
        a_attack = self._safe_col('away_attack_strength')
        h_defense = self._safe_col('home_defense_strength')

        self.df['poisson_home_goals'] = (
            h_attack * a_defense * league_avg
        ).round(3)

        self.df['poisson_away_goals'] = (
            a_attack * h_defense * league_avg
        ).round(3)

        # 泊松预测: 主胜概率 / 平局概率 / 客胜概率
        phg = self._safe_col('poisson_home_goals')
        pag = self._safe_col('poisson_away_goals')
        self.df['poisson_home_win_prob'] = (
            1.0 / (1.0 + np.exp(-(phg - pag)))
        ).round(4)

        logger.info(f"  [+] 泊松特征: 场均进球均值={team_stats['avg_goals_for'].mean():.2f}")
        logger.info(f"  [+] 攻防强度范围: {team_stats['attack_strength'].min():.2f} - {team_stats['attack_strength'].max():.2f}")

        return self.df

    # ── 保存 ──────────────────────────────────────────────────

    def save_enhanced_data(
        self, output_path: str = "data/enhanced_matches.csv"
    ) -> str:
        """保存增强后的数据

        Args:
            output_path: 输出 CSV 路径 (相对于项目根目录)

        Returns:
            实际保存的路径
        """
        import os

        # 解析项目根目录 (backend/data/ -> project root)
        _this_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(_this_dir))

        # 如果提供了相对路径，相对于项目根
        if not os.path.isabs(output_path):
            full_path = os.path.join(project_root, output_path)
        else:
            full_path = output_path
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        self.enhanced_df = self.df.copy()
        self.enhanced_df.to_csv(full_path, index=False)

        logger.info(f"[SAVE] 增强数据已保存: {full_path}")
        logger.info(f"   └─ {len(self.enhanced_df):,} 行 × {len(self.enhanced_df.columns)} 列")
        return full_path


# ═══════════════════════════════════════════════════════════════
#  CLI 接口
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse
    import os as _os
    import sys as _sys

    parser = argparse.ArgumentParser(
        description='哨响AI — 数据增强与质量检查 (T14)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  # 仅分析数据质量
  python backend/data/enhancement.py --analyze --input data/match_data.csv

  # 完整增强管道
  python backend/data/enhancement.py --enhance --input data/match_data.csv

  # 分析 + 增强
  python backend/data/enhancement.py --analyze --enhance --input data/match_data.csv
        ''',
    )
    parser.add_argument('--input', type=str, required=True,
                        help='原始比赛数据 CSV 路径')
    parser.add_argument('--analyze', action='store_true',
                        help='运行数据质量分析')
    parser.add_argument('--enhance', action='store_true',
                        help='运行完整特征增强管道')
    parser.add_argument('--output', type=str, default='data/enhanced_matches.csv',
                        help='增强数据输出路径 (默认 data/enhanced_matches.csv)')
    parser.add_argument('--windows', type=str, default='5,10,20',
                        help='滚动特征窗口大小，逗号分隔 (默认 5,10,20)')
    parser.add_argument('--elo-initial', type=float, default=1500.0,
                        help='初始 ELO (默认 1500)')
    parser.add_argument('--elo-k', type=float, default=20.0,
                        help='ELO K 因子 (默认 20)')

    args = parser.parse_args()

    # 至少选一个操作
    if not args.analyze and not args.enhance:
        logger.info("⚠️  请指定 --analyze 或 --enhance (或两者)")
        logger.info("   示例: python backend/data/enhancement.py --analyze --input data.csv")
        _sys.exit(1)

    # 路径解析
    _this_dir = _os.path.dirname(_os.path.abspath(__file__))
    project_root = _os.path.dirname(_os.path.dirname(_this_dir))

    input_path = args.input
    if not _os.path.isabs(input_path):
        input_path = _os.path.join(project_root, input_path)

    logger.info(f"\n{'=' * 60}")
    logger.info(f"  哨响AI — 数据增强与质量检查")
    logger.info(f"{'=' * 60}")
    logger.info(f"  输入: {input_path}")
    logger.info(f"{'=' * 60}\n")

    # 加载数据
    logger.info(f"📂 加载数据...")
    df = pd.read_csv(input_path, low_memory=False)
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])

    enhancer = DataEnhancer(df)

    # ── 1. 质量分析 ──
    if args.analyze:
        analysis = enhancer.analyze_data_quality()
        logger.info(f"\n📊 数据质量报告概要:")
        s = analysis['summary']
        logger.info(f"   ├─ 总比赛: {s['total_matches']:,}")
        logger.info(f"   ├─ 日期范围: {s['date_range']['start']} ~ {s['date_range']['end']}")
        logger.info(f"   ├─ 联赛数: {s['leagues_count']}")
        logger.info(f"   ├─ 球队数: {s['teams_count']:,}")
        logger.info(f"   ├─ 完整性:")
        for col, info in analysis.get('completeness', {}).items():
            logger.info(f"   │  └─ {col}: {100 - info['missing_pct']:.1f}% 完整")
        logger.info(f"   └─ 异常: {len(analysis.get('anomalies', []))} 类")
        for a in analysis.get('anomalies', []):
            logger.info(f"      [!] {a['type']}: {a['count']} 条")

    # ── 2. 特征增强 ──
    if args.enhance:
        windows = [int(w.strip()) for w in args.windows.split(',')]
        logger.info(f"\n🚀 运行特征增强管道 (窗口: {windows})...")

        enhancer.create_rolling_features(windows)
        enhancer.generate_team_rating_features(
            initial_elo=args.elo_initial,
            k_factor=args.elo_k,
        )
        enhancer.create_poisson_features()

        # 保存
        output_path = args.output
        if not _os.path.isabs(output_path):
            output_path = _os.path.join(project_root, output_path)

        saved_path = enhancer.save_enhanced_data(output_path)
        logger.info(f"\n✅ 增强完成！特征列: {len(enhancer.df.columns)}")

    logger.info(f"\n{'=' * 60}")
    logger.info(f"  完成")
    logger.info(f"{'=' * 60}\n")
