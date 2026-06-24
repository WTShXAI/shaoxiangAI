"""
哨响AI - 全队伤病评估模块 (T13)
===================================
将 goalkeeper_model 的3因子模型扩展到全队11个位置，设计伤病影响量化指标，
并集成到特征管道 (match_features + SequenceBundle)。

核心设计:
  1. 位置重要性权重 — GK/CB/CM/ST 各位置缺阵对球队影响不同
  2. 球员质量降级 — 替补 vs 主力 的实力差距
  3. 伤病恢复因子 — 伤愈复出后的状态折扣
  4. 阵容深度指数 — 板凳厚度对抗伤病的能力
  5. 累积伤病指数 — 多人缺阵的非线性叠加效应
  6. 交锋伤病差 — 双方伤病影响的对比

输出特征:
  - 直接写入 match_features 表 (injury_index, squad_depth, injury_impact_diff 等)
  - 可追加到 SequenceBundle.static_features 供 DL 模型使用

数据来源:
  - 显式伤病数据: TeamInjuryReport (手动/API 输入)
  - 隐式代理数据: form_trends 表中出场数异常 / media_intelligence NLP
  - 球队基础数据: teams 表 (rating, attack/defense_strength)

用法:
    from optimize.injury_index import TeamInjuryModel
    model = TeamInjuryModel()
    features = model.compute_match_features(home_team, away_team, match_date)
    # features → dict of injury-related features
"""

import sqlite3
import logging
import warnings
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 数据库路径
DB_PATH = None  # 延迟初始化


def _get_db_path():
    global DB_PATH
    if DB_PATH is None:
        import os
        DB_PATH = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'football_data.db'
        )
    return DB_PATH


# ════════════════════════════════════════════════════════════════
# 位置定义与权重
# ════════════════════════════════════════════════════════════════

@dataclass
class PositionProfile:
    """位置画像 — 定义缺阵影响"""
    code: str           # 位置代码: GK, CB, FB, DM, CM, AM, W, ST
    name_cn: str        # 中文名
    importance: float   # 缺阵影响权重 (0-1, 越高越重要)
    replaceability: float  # 可替代性 (0-1, 越高越容易被替代)
    # 缺阵对攻防的影响 (0-1)
    attack_impact: float   # 对进攻的影响权重
    defense_impact: float  # 对防守的影响权重


# 标准阵型位置 (4-3-3)
POSITION_PROFILES = {
    'GK':  PositionProfile('GK',  '门将',   0.95, 0.20, 0.05, 0.95),
    'CB':  PositionProfile('CB',  '中后卫', 0.85, 0.40, 0.10, 0.80),
    'FB':  PositionProfile('FB',  '边后卫', 0.55, 0.65, 0.40, 0.30),
    'DM':  PositionProfile('DM',  '后腰',   0.75, 0.45, 0.30, 0.60),
    'CM':  PositionProfile('CM',  '中场',   0.70, 0.50, 0.50, 0.35),
    'AM':  PositionProfile('AM',  '前腰',   0.65, 0.45, 0.70, 0.10),
    'W':   PositionProfile('W',   '边锋',   0.50, 0.55, 0.65, 0.05),
    'ST':  PositionProfile('ST',  '前锋',   0.80, 0.40, 0.85, 0.05),
}

# 标准阵型 4-3-3 各位置人数
FORMATION_433 = {'GK': 1, 'CB': 2, 'FB': 2, 'DM': 1, 'CM': 2, 'W': 2, 'ST': 1}

# 伤病严重程度
INJURY_SEVERITY = {
    'doubtful': 0.3,    # 出场成疑
    'out':      0.7,    # 缺阵
    'long_term': 1.0,   # 长期伤缺
    'returning': 0.4,   # 刚复出(状态折扣)
}


# ════════════════════════════════════════════════════════════════
# 球员与阵容数据结构
# ════════════════════════════════════════════════════════════════

@dataclass
class PlayerInjuryInfo:
    """球员伤病信息"""
    name: str
    team: str
    position: str = 'CM'         # 位置代码
    is_starter: bool = True      # 是否主力
    quality_rating: float = 0.7  # 球员实力评分 (0-1, 相对于联赛)
    injury_status: str = 'fit'   # fit / doubtful / out / long_term / returning
    games_missed: int = 0        # 已缺阵场次
    expected_return: int = 0     # 预计再缺阵场次
    games_since_return: int = 0  # 复出后场次

    @property
    def severity_score(self) -> float:
        """伤病严重程度评分 (0-1)"""
        return INJURY_SEVERITY.get(self.injury_status, 0.0)

    @property
    def quality_gap(self) -> float:
        """主力→替补实力差 (0-1)"""
        if self.is_starter:
            return self.quality_rating * 0.2  # 替补约为主力的 80%
        return 0.0


@dataclass
class TeamInjuryReport:
    """球队伤病报告 — 某场比赛前某队的伤病情况"""
    team: str
    match_date: str = ''
    players: List[PlayerInjuryInfo] = field(default_factory=list)
    # 媒体情报
    media_injury_alert: bool = False
    media_injury_impact: float = 0.0  # -1 ~ +1 (正=对球队不利)
    # 阵容深度代理
    squad_rating: float = 0.7  # 球队整体评分 (0-1)
    bench_rating: float = 0.5  # 替补席评分 (0-1)

    @property
    def n_injured(self) -> int:
        """受伤球员数 (排除fit)"""
        return sum(1 for p in self.players if p.injury_status != 'fit')

    @property
    def n_starters_out(self) -> int:
        """主力缺阵数"""
        return sum(1 for p in self.players
                   if p.is_starter and p.injury_status in ('out', 'long_term'))

    @property
    def n_doubtful(self) -> int:
        """出场成疑数"""
        return sum(1 for p in self.players if p.injury_status == 'doubtful')


# ════════════════════════════════════════════════════════════════
# 全队伤病评估模型
# ════════════════════════════════════════════════════════════════

class TeamInjuryModel:
    """
    全队伤病评估模型

    将 goalkeeper_model 的3因子 (training_load, injury_recovery, pressure)
    扩展到全队11个位置，计算：

    1. 位置加权缺阵指数 (Position-Weighted Absence Index)
    2. 球员质量降级 (Quality Degradation)
    3. 伤病恢复因子 (Injury Recovery Factor) — 复出球员状态折扣
    4. 阵容深度指数 (Squad Depth Index)
    5. 累积伤病指数 (Cumulative Injury Index) — 非线性叠加
    6. 攻防分离影响 (Attack/Defense Impact Split)
    """

    # 累积伤病的非线性指数 (控制多人缺阵的叠加效应)
    CUMULATIVE_EXPONENT = 1.3
    # 复出状态恢复窗口 (N场后恢复到100%)
    RECOVERY_WINDOW = 5

    def __init__(self, db_path: str = None):
        self.db_path = db_path or _get_db_path()
        self._position_profiles = dict(POSITION_PROFILES)
        # 缓存: team_name → 最近 form_trends
        self._form_cache = {}
        # 缓存: team_name → team rating
        self._rating_cache = {}

    # ─── 核心: 计算单队伤病指数 ───────────────────────────────

    def compute_team_injury_index(self, report: TeamInjuryReport) -> Dict:
        """
        计算单队伤病指数 (0-1, 越高伤病影响越大)

        Returns:
            {
                'injury_index': float,          # 综合伤病指数 (0-1)
                'attack_impact': float,         # 进攻端影响 (0-1)
                'defense_impact': float,        # 防守端影响 (0-1)
                'squad_depth': float,           # 阵容深度 (0-1, 越高越厚)
                'quality_degradation': float,   # 实力降级 (0-1)
                'recovery_discount': float,     # 复出折扣 (0-1)
                'n_injured': int,
                'n_starters_out': int,
                'n_doubtful': int,
                'position_breakdown': dict,     # 各位置缺阵详情
            }
        """
        # 1. 位置加权缺阵指数
        pwai, attack_impact, defense_impact, pos_breakdown = \
            self._compute_position_weighted_absence(report)

        # 2. 球员质量降级
        quality_deg = self._compute_quality_degradation(report)

        # 3. 复出状态折扣
        recovery_disc = self._compute_recovery_discount(report)

        # 4. 阵容深度指数
        squad_depth = self._compute_squad_depth(report)

        # 5. 累积伤病指数 (非线性叠加)
        cumulative = self._compute_cumulative_index(pwai, report)

        # 6. 媒体情报融合
        media_adj = 0.0
        if report.media_injury_alert:
            media_adj = abs(report.media_injury_impact) * 0.15

        # 综合伤病指数
        injury_index = min(1.0, cumulative + quality_deg * 0.3 +
                           recovery_disc * 0.15 + media_adj)

        return {
            'injury_index':      round(injury_index, 4),
            'attack_impact':     round(attack_impact, 4),
            'defense_impact':    round(defense_impact, 4),
            'squad_depth':       round(squad_depth, 4),
            'quality_degradation': round(quality_deg, 4),
            'recovery_discount': round(recovery_disc, 4),
            'n_injured':         report.n_injured,
            'n_starters_out':    report.n_starters_out,
            'n_doubtful':        report.n_doubtful,
            'position_breakdown': pos_breakdown,
        }

    def _compute_position_weighted_absence(self, report: TeamInjuryReport):
        """
        位置加权缺阵指数 (PWAI)

        逻辑:
          对每个受伤球员:
            impact = position.importance × (1 - position.replaceability) × severity

          攻防分离:
            attack_impact = Σ impact × position.attack_impact
            defense_impact = Σ impact × position.defense_impact
        """
        total_impact = 0.0
        attack_impact = 0.0
        defense_impact = 0.0
        pos_breakdown = {}

        for player in report.players:
            if player.injury_status == 'fit':
                continue

            pos = self._position_profiles.get(player.position)
            if pos is None:
                pos = self._position_profiles['CM']  # 默认中场

            severity = player.severity_score
            # 缺阵影响 = 位置重要性 × 不可替代性 × 严重程度
            impact = pos.importance * (1.0 - pos.replaceability) * severity

            # 主力/替补权重
            if player.is_starter:
                impact *= 1.0
            else:
                impact *= 0.3  # 替补缺阵影响小

            total_impact += impact
            attack_impact += impact * pos.attack_impact
            defense_impact += impact * pos.defense_impact

            # 按位置统计
            if player.position not in pos_breakdown:
                pos_breakdown[player.position] = {
                    'count': 0, 'total_severity': 0.0, 'total_impact': 0.0
                }
            pos_breakdown[player.position]['count'] += 1
            pos_breakdown[player.position]['total_severity'] += severity
            pos_breakdown[player.position]['total_impact'] += impact

        # 归一化 (最多11人缺阵的极端情况)
        max_possible = 3.0  # 理论最大约3-4个主力缺阵即影响极大
        total_impact = min(1.0, total_impact / max_possible)
        attack_impact = min(1.0, attack_impact / max_possible)
        defense_impact = min(1.0, defense_impact / max_possible)

        return total_impact, attack_impact, defense_impact, pos_breakdown

    def _compute_quality_degradation(self, report: TeamInjuryReport) -> float:
        """
        球员质量降级

        主力缺阵 → 替补顶上 → 实力差 = 主力评分 - 替补评分
        替补评分 = 主力评分 × (1 - degradation_rate)
        degradation_rate 由 bench_rating / squad_rating 决定
        """
        total_gap = 0.0
        for player in report.players:
            if player.injury_status in ('out', 'long_term') and player.is_starter:
                # 替补实力 ≈ bench_rating / squad_rating × player.quality
                depth_ratio = report.bench_rating / max(report.squad_rating, 0.01)
                sub_quality = player.quality_rating * depth_ratio
                gap = player.quality_rating - sub_quality
                total_gap += gap

        # 归一化
        return min(1.0, total_gap / 1.5)  # 1.5 ≈ 3个主力降级0.5

    def _compute_recovery_discount(self, report: TeamInjuryReport) -> float:
        """
        复出状态折扣

        刚复出的球员状态通常只有 70-85%
        recovery = min(1.0, games_since_return / RECOVERY_WINDOW)
        discount = 1.0 - recovery → 状态折扣
        """
        total_discount = 0.0
        n_returning = 0
        for player in report.players:
            if player.injury_status == 'returning' and player.is_starter:
                recovery = min(1.0, player.games_since_return / self.RECOVERY_WINDOW)
                discount = 1.0 - recovery
                pos = self._position_profiles.get(player.position)
                weight = pos.importance if pos else 0.7
                total_discount += discount * weight
                n_returning += 1

        if n_returning == 0:
            return 0.0
        return min(1.0, total_discount / 2.0)  # 归一化

    def _compute_squad_depth(self, report: TeamInjuryReport) -> float:
        """
        阵容深度指数 (0-1, 越高越厚)

        基于球队评分和替补评分的差异:
          depth = 1.0 - (squad_rating - bench_rating)
          受伤病人数修正: depth *= (1 - 0.05 * n_injured)
        """
        base_depth = report.bench_rating / max(report.squad_rating, 0.01)
        base_depth = min(1.0, base_depth)
        # 伤病越多, 深度越薄
        injury_penalty = 1.0 - 0.05 * report.n_injured
        injury_penalty = max(0.3, injury_penalty)
        return base_depth * injury_penalty

    def _compute_cumulative_index(self, pwai: float,
                                   report: TeamInjuryReport) -> float:
        """
        累积伤病指数 — 非线性叠加

        当多人缺阵时, 影响不是简单的线性加总, 而是指数增长:
          cumulative = pwai ^ (1 + 0.1 * (n_injured - 1))

        示例:
          1人缺阵: pwai^1.0 = pwai
          3人缺阵: pwai^1.2 ≈ 放大20%
          5人缺阵: pwai^1.4 ≈ 放大40%
        """
        n = report.n_injured
        if n <= 1:
            return pwai
        exponent = 1.0 + 0.1 * (n - 1)
        return min(1.0, pwai ** (1.0 / exponent) if pwai > 0 else 0.0)

    # ─── 比赛: 计算双方伤病差 ──────────────────────────────────

    def compute_match_features(self,
                                home_report: TeamInjuryReport,
                                away_report: TeamInjuryReport) -> Dict:
        """
        计算比赛级别的伤病特征 (可直接写入 match_features)

        Returns:
            {
                'home_injury_index': float,    # 主队伤病指数
                'away_injury_index': float,    # 客队伤病指数
                'injury_index_diff': float,    # 伤病指数差 (正=主队更健康)
                'home_attack_impact': float,   # 主队进攻端伤病影响
                'away_attack_impact': float,
                'home_defense_impact': float,   # 主队防守端伤病影响
                'away_defense_impact': float,
                'attack_impact_diff': float,   # 进攻伤病差
                'defense_impact_diff': float,  # 防守伤病差
                'home_squad_depth': float,     # 主队阵容深度
                'away_squad_depth': float,
                'squad_depth_diff': float,     # 深度差
                'home_quality_deg': float,     # 主队实力降级
                'away_quality_deg': float,
                'quality_deg_diff': float,
                'home_recovery_disc': float,   # 主队复出折扣
                'away_recovery_disc': float,
                'recovery_disc_diff': float,
                'total_injury_asymmetry': float,  # 综合伤病不对称性
            }
        """
        home = self.compute_team_injury_index(home_report)
        away = self.compute_team_injury_index(away_report)

        hi = home['injury_index']
        ai = away['injury_index']

        features = {
            'home_injury_index':    hi,
            'away_injury_index':    ai,
            # 所有 diff: 正值 = 主队优势 (主队伤病更轻/深度更厚)
            'injury_index_diff':    round(ai - hi, 4),
            'home_attack_impact':   home['attack_impact'],
            'away_attack_impact':   away['attack_impact'],
            'home_defense_impact':  home['defense_impact'],
            'away_defense_impact':  away['defense_impact'],
            'attack_impact_diff':   round(away['attack_impact'] - home['attack_impact'], 4),
            'defense_impact_diff':  round(away['defense_impact'] - home['defense_impact'], 4),
            'home_squad_depth':     home['squad_depth'],
            'away_squad_depth':    away['squad_depth'],
            'squad_depth_diff':    round(home['squad_depth'] - away['squad_depth'], 4),
            'home_quality_deg':    home['quality_degradation'],
            'away_quality_deg':    away['quality_degradation'],
            'quality_deg_diff':    round(away['quality_degradation'] - home['quality_degradation'], 4),
            'home_recovery_disc':  home['recovery_discount'],
            'away_recovery_disc':  away['recovery_discount'],
            'recovery_disc_diff':  round(away['recovery_discount'] - home['recovery_discount'], 4),
        }

        # 综合伤病不对称性 (加权汇总)
        features['total_injury_asymmetry'] = round(
            0.35 * features['injury_index_diff'] +
            0.25 * features['attack_impact_diff'] +
            0.25 * features['defense_impact_diff'] +
            0.15 * features['squad_depth_diff'],
            4
        )

        return features

    # ─── 从数据库构建伤病报告 (代理模式) ──────────────────────

    def build_injury_report_from_db(self, team_name: str,
                                     match_date: str,
                                     media_signal: Dict = None) -> TeamInjuryReport:
        """
        从数据库构建伤病报告 (代理模式)

        由于当前数据库没有独立的伤病表, 使用以下代理策略:
          1. 球队评分 → squad_rating / bench_rating
          2. form_trends → 近期异常 (出场数骤减 = 可能伤病)
          3. media_intelligence → NLP 伤病信号
          4. 球队近N场失球变化 → 防守端可能伤病

        未来接入伤病API后可直接替换此方法。
        """
        report = TeamInjuryReport(
            team=team_name,
            match_date=match_date,
        )

        # 1. 从 teams 表获取评分
        rating_data = self._get_team_rating(team_name)
        if rating_data:
            # squad_rating: 用 team rating 归一化到 0-1
            report.squad_rating = min(1.0, rating_data.get('rating', 70) / 100.0)
            # bench_rating: 替补实力约为主力的 75-85%
            depth_factor = self._estimate_depth_factor(rating_data)
            report.bench_rating = report.squad_rating * depth_factor

        # 2. 从 form_trends 代理伤病信号
        self._infer_injuries_from_form(report, team_name, match_date)

        # 3. 媒体情报
        if media_signal:
            report.media_injury_alert = media_signal.get('injury_alert', False)
            report.media_injury_impact = media_signal.get('injury_impact', 0.0)

        return report

    def _get_team_rating(self, team_name: str) -> Optional[Dict]:
        """从 teams 表获取球队评分"""
        if team_name in self._rating_cache:
            return self._rating_cache[team_name]

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT rating, attack_strength, defense_strength FROM teams "
                "WHERE team_name = ?", (team_name,)
            )
            row = cur.fetchone()
            conn.close()

            if row:
                result = dict(row)
            else:
                result = {'rating': 70, 'attack_strength': 1.0, 'defense_strength': 1.0}

            self._rating_cache[team_name] = result
            return result
        except (Exception, KeyError, IndexError) as e:
            logger.warning(f"获取球队评分失败 {team_name}: {e}")
            return {'rating': 70, 'attack_strength': 1.0, 'defense_strength': 1.0}

    def _estimate_depth_factor(self, rating_data: Dict) -> float:
        """
        估计阵容深度因子 (0.70 - 0.90)

        顶级球队板凳更厚, 底级球队更薄
        """
        rating = rating_data.get('rating', 70)
        if rating >= 85:
            return 0.88  # 顶级球队板凳厚
        elif rating >= 75:
            return 0.82
        elif rating >= 65:
            return 0.76
        else:
            return 0.72  # 弱队板凳薄

    def _infer_injuries_from_form(self, report: TeamInjuryReport,
                                    team_name: str, match_date: str):
        """
        从 form_trends 代理推断伤病

        逻辑:
          - 近5场进球骤降 → 可能前锋/攻击手缺阵
          - 近5场失球骤增 → 可能后卫/门将缺阵
          - 最近一场未出场 → 可能受伤
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 获取该队 match_date 之前的近5场 form_trends
            cur.execute("""
                SELECT goals_for, goals_against, is_clean_sheet, result
                FROM form_trends
                WHERE team_name = ? AND match_date <= ?
                ORDER BY match_date DESC LIMIT 10
            """, (team_name, match_date))
            rows = cur.fetchall()
            conn.close()

            if len(rows) < 3:
                return

            recent5 = [dict(r) for r in rows[:5]]
            older5 = [dict(r) for r in rows[5:10]] if len(rows) >= 6 else []

            # 进球骤降 → 攻击手可能缺阵
            recent_gf = np.mean([r['goals_for'] or 0 for r in recent5])
            if older5:
                older_gf = np.mean([r['goals_for'] or 0 for r in older5])
                gf_drop = older_gf - recent_gf
                if gf_drop > 0.8:  # 进球均值下降 > 0.8
                    report.players.append(PlayerInjuryInfo(
                        name='proxy_ST', team=team_name,
                        position='ST', is_starter=True,
                        quality_rating=report.squad_rating,
                        injury_status='doubtful',
                    ))

            # 失球骤增 → 防守可能缺阵
            recent_ga = np.mean([r['goals_against'] or 0 for r in recent5])
            if older5:
                older_ga = np.mean([r['goals_against'] or 0 for r in older5])
                ga_rise = recent_ga - older_ga
                if ga_rise > 0.8:  # 失球均值上升 > 0.8
                    report.players.append(PlayerInjuryInfo(
                        name='proxy_CB', team=team_name,
                        position='CB', is_starter=True,
                        quality_rating=report.squad_rating,
                        injury_status='doubtful',
                    ))

            # 近3场零封率 → 门将状态
            recent_cs_rate = np.mean([r['is_clean_sheet'] or 0 for r in recent5[:3]])
            if recent_cs_rate < 0.1 and recent_ga > 1.5:
                report.players.append(PlayerInjuryInfo(
                    name='proxy_GK', team=team_name,
                    position='GK', is_starter=False,
                    quality_rating=report.squad_rating,
                    injury_status='doubtful',
                ))

        except (Exception, KeyError, IndexError) as e:
            logger.warning(f"推断伤病失败 {team_name}: {e}")

    # ─── 批量生成伤病特征 (从数据库) ─────────────────────────

    def generate_features_df(self, match_ids: List[int] = None) -> pd.DataFrame:
        """
        为所有比赛(或指定比赛)生成伤病特征 DataFrame

        Returns:
            DataFrame with columns: match_id, home_team, away_team, match_date,
            + 所有 compute_match_features() 输出列
        """
        conn = sqlite3.connect(self.db_path)

        if match_ids:
            placeholders = ','.join('?' * len(match_ids))
            query = f"""
                SELECT m.match_id, m.match_date, m.home_team_name, m.away_team_name
                FROM matches m
                WHERE m.match_id IN ({placeholders}) AND m.status = 'finished'
                ORDER BY m.match_date
            """
            matches_df = pd.read_sql(query, conn, params=match_ids)
        else:
            query = """
                SELECT m.match_id, m.match_date, m.home_team_name, m.away_team_name
                FROM matches m
                WHERE m.status = 'finished'
                ORDER BY m.match_date
            """
            matches_df = pd.read_sql(query, conn)
        conn.close()

        if matches_df.empty:
            logger.warning("无比赛数据")
            return pd.DataFrame()

        results = []
        for _, row in matches_df.iterrows():
            home_report = self.build_injury_report_from_db(
                row['home_team_name'], row['match_date'])
            away_report = self.build_injury_report_from_db(
                row['away_team_name'], row['match_date'])

            features = self.compute_match_features(home_report, away_report)
            features['match_id'] = row['match_id']
            features['home_team'] = row['home_team_name']
            features['away_team'] = row['away_team_name']
            features['match_date'] = row['match_date']
            results.append(features)

        df = pd.DataFrame(results)
        # 确保列顺序: match_id 在前
        cols = ['match_id', 'home_team', 'away_team', 'match_date'] + \
               [c for c in df.columns if c not in ('match_id', 'home_team', 'away_team', 'match_date')]
        return df[cols]

    # ─── 集成: 追加到 SequenceBundle ─────────────────────────

    def augment_bundle(self, bundle, features_df: pd.DataFrame = None,
                       mode: str = 'diff'):
        """
        将伤病特征追加到 SequenceBundle 的 static_features

        Args:
            bundle: SequenceBundle 实例
            features_df: 伤病特征 DataFrame (None 则自动生成)
            mode: 'diff' 只用差值列 | 'both' 用所有列 | 'home' 只用主队列
        """
        if features_df is None:
            features_df = self.generate_features_df()

        feat_indexed = features_df.set_index('match_id')

        # 选择模式
        diff_cols = [c for c in feat_indexed.columns if c.endswith('_diff') or
                     c in ('total_injury_asymmetry',)]
        home_cols = [c for c in feat_indexed.columns if c.startswith('home_')]
        away_cols = [c for c in feat_indexed.columns if c.startswith('away_')]

        if mode == 'diff':
            use_cols = diff_cols
        elif mode == 'both':
            use_cols = diff_cols + home_cols + away_cols
        elif mode == 'home':
            use_cols = home_cols + diff_cols
        else:
            use_cols = diff_cols

        # 过滤有效列
        use_cols = [c for c in use_cols if c in feat_indexed.columns]

        new_features = feat_indexed[use_cols].values

        # 拼接到 static_features
        old_static = bundle.static_features
        bundle.static_features = np.hstack([old_static, new_features])

        # 命名: 加 inj_ 前缀
        def _rename_col(c):
            if c.endswith('_diff'):
                return 'inj_d_' + c.replace('_diff', '')
            elif c.startswith('home_'):
                return 'inj_h_' + c[5:]
            elif c.startswith('away_'):
                return 'inj_a_' + c[5:]
            return 'inj_' + c

        bundle.static_feature_names = list(bundle.static_feature_names) + [
            _rename_col(c) for c in use_cols
        ]

        return bundle

    # ─── 集成: 写入 match_features 表 ─────────────────────────

    def write_to_match_features(self, features_df: pd.DataFrame = None,
                                 match_ids: List[int] = None):
        """
        将伤病特征写入 match_features 表

        新增列: injury_index_home, injury_index_away, injury_index_diff,
                attack_impact_diff, defense_impact_diff, squad_depth_diff,
                quality_deg_diff, total_injury_asymmetry
        """
        if features_df is None:
            features_df = self.generate_features_df(match_ids=match_ids)

        if features_df.empty:
            logger.warning("无伤病特征数据写入")
            return

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # 新增列 (如果不存在)
        new_columns = {
            'injury_index_home': 'REAL DEFAULT 0.0',
            'injury_index_away': 'REAL DEFAULT 0.0',
            'injury_index_diff': 'REAL DEFAULT 0.0',
            'attack_impact_diff': 'REAL DEFAULT 0.0',
            'defense_impact_diff': 'REAL DEFAULT 0.0',
            'squad_depth_diff': 'REAL DEFAULT 0.0',
            'quality_deg_diff': 'REAL DEFAULT 0.0',
            'total_injury_asymmetry': 'REAL DEFAULT 0.0',
        }

        for col_name, col_type in new_columns.items():
            try:
                cur.execute(f'ALTER TABLE match_features ADD COLUMN {col_name} {col_type}')
            except (Exception, sqlite3.Error):
                pass  # 列已存在

        # 更新数据
        col_map = {
            'injury_index_home': 'home_injury_index',
            'injury_index_away': 'away_injury_index',
            'injury_index_diff': 'injury_index_diff',
            'attack_impact_diff': 'attack_impact_diff',
            'defense_impact_diff': 'defense_impact_diff',
            'squad_depth_diff': 'squad_depth_diff',
            'quality_deg_diff': 'quality_deg_diff',
            'total_injury_asymmetry': 'total_injury_asymmetry',
        }

        updated = 0
        for _, row in features_df.iterrows():
            mid = row['match_id']
            # 检查 match_features 中是否存在该 match_id
            cur.execute(
                "SELECT feature_id FROM match_features WHERE match_id = ?", (mid,))
            exists = cur.fetchone()

            if exists:
                set_clauses = []
                values = []
                for db_col, df_col in col_map.items():
                    if df_col in row:
                        set_clauses.append(f"{db_col} = ?")
                        values.append(row[df_col])
                if set_clauses:
                    values.append(mid)
                    cur.execute(
                        f"UPDATE match_features SET {', '.join(set_clauses)} "
                        f"WHERE match_id = ?", values
                    )
                    updated += 1

        conn.commit()
        conn.close()
        logger.info(f"已更新 {updated} 场比赛的伤病特征")

    # ─── 与 goalkeeper_model 协同 ─────────────────────────────

    def integrate_keeper_risk(self, team_name: str,
                               keeper_eval: Dict,
                               injury_index: Dict) -> Dict:
        """
        将门将风险评估结果融合到全队伤病指数中

        Args:
            team_name: 球队名
            keeper_eval: KeeperRiskModel.evaluate() 的输出
            injury_index: compute_team_injury_index() 的输出

        Returns:
            融合后的伤病指数 (injury_index 会被门将风险修正)
        """
        base_index = injury_index['injury_index']
        keeper_risk = keeper_eval.get('keeper_risk', 0.75)

        # 门将高风险 → 伤病指数上升
        # keeper_risk: 0-1, 越低越危险
        keeper_penalty = 0.0
        if keeper_risk < 0.70:
            keeper_penalty = (0.70 - keeper_risk) * 0.3  # 最多 +0.09
        elif keeper_risk > 0.85:
            keeper_penalty = -(keeper_risk - 0.85) * 0.1  # 最多 -0.015

        adjusted_index = max(0.0, min(1.0, base_index + keeper_penalty))

        # 防守端影响调整
        defense_impact = injury_index['defense_impact']
        if keeper_risk < 0.70:
            defense_impact = min(1.0, defense_impact + (0.70 - keeper_risk) * 0.2)

        result = dict(injury_index)
        result['injury_index'] = round(adjusted_index, 4)
        result['defense_impact'] = round(defense_impact, 4)
        result['keeper_risk_contribution'] = round(keeper_penalty, 4)
        result['keeper_risk_level'] = keeper_eval.get('risk_level', 'medium')

        return result


# ════════════════════════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════════════════════════

def compute_injury_features(home_report: TeamInjuryReport = None,
                             away_report: TeamInjuryReport = None,
                             home_team: str = None,
                             away_team: str = None,
                             match_date: str = None,
                             db_path: str = None) -> Dict:
    """
    便捷函数: 计算单场伤病特征

    可以直接传入 TeamInjuryReport, 也可以只传球队名+日期 (自动从DB构建)
    """
    model = TeamInjuryModel(db_path=db_path)

    if home_report is None and home_team:
        home_report = model.build_injury_report_from_db(home_team, match_date)
    if away_report is None and away_team:
        away_report = model.build_injury_report_from_db(away_team, match_date)

    if home_report is None or away_report is None:
        raise ValueError("必须提供 TeamInjuryReport 或 球队名+日期")

    return model.compute_match_features(home_report, away_report)


def generate_injury_features(db_path: str = None) -> pd.DataFrame:
    """便捷函数: 为所有比赛生成伤病特征"""
    model = TeamInjuryModel(db_path=db_path)
    return model.generate_features_df()


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("=" * 60)
    print("  T13 全队伤病评估模块 - 示例")
    print("=" * 60)

    model = TeamInjuryModel()

    # 示例 1: 手动构建伤病报告
    print("\n--- 示例 1: 手动构建伤病报告 ---")
    home_report = TeamInjuryReport(
        team='利物浦',
        squad_rating=0.87,
        bench_rating=0.75,
        players=[
            PlayerInjuryInfo(name='Salah', team='利物浦', position='W',
                           is_starter=True, quality_rating=0.92,
                           injury_status='doubtful'),
            PlayerInjuryInfo(name='Van Dijk', team='利物浦', position='CB',
                           is_starter=True, quality_rating=0.88,
                           injury_status='out'),
            PlayerInjuryInfo(name='Alisson', team='利物浦', position='GK',
                           is_starter=True, quality_rating=0.90,
                           injury_status='returning',
                           games_since_return=2),
        ]
    )

    away_report = TeamInjuryReport(
        team='曼城',
        squad_rating=0.90,
        bench_rating=0.82,
        players=[
            PlayerInjuryInfo(name='De Bruyne', team='曼城', position='AM',
                           is_starter=True, quality_rating=0.93,
                           injury_status='out'),
        ]
    )

    features = model.compute_match_features(home_report, away_report)
    for k, v in features.items():
        print(f"  {k}: {v}")

    # 示例 2: 从数据库构建 (代理模式)
    print("\n--- 示例 2: 从数据库代理构建 ---")
    report = model.build_injury_report_from_db('利物浦', '2025-01-15')
    print(f"  球队: {report.team}")
    print(f"  评分: squad={report.squad_rating:.2f}, bench={report.bench_rating:.2f}")
    print(f"  伤病球员: {report.n_injured}")
    for p in report.players:
        print(f"    - {p.name} ({p.position}): {p.injury_status}")

    # 示例 3: 与门将模型协同
    print("\n--- 示例 3: 与门将模型协同 ---")
    from modules.goalkeeper_model import KeeperRiskModel
    keeper_model = KeeperRiskModel()

    home_idx = model.compute_team_injury_index(home_report)
    keeper_eval = keeper_model.evaluate("Alisson", {'games_since_injury': 2})

    fused = model.integrate_keeper_risk('利物浦', keeper_eval, home_idx)
    print(f"  原始伤病指数: {home_idx['injury_index']}")
    print(f"  融合后指数: {fused['injury_index']}")
    print(f"  门将贡献: {fused['keeper_risk_contribution']}")
    print(f"  门将风险级别: {fused['keeper_risk_level']}")
