"""
哨响AI - 市场特征提取器 (T14)
================================
从赔率数据中提取市场级特征，包括隐含概率、赔率变动率、市场信心指标、
模型-市场分歧度等，集成到特征管道 (match_features + SequenceBundle)。

核心设计:
  1. 隐含概率体系 — 1X2 隐含概率 + 去抽水公平概率
  2. 赔率变动特征 — 开盘→收盘漂移、变动速率、方向一致性
  3. 市场信心指标 — 返还率/抽水率、赔率紧密度、博彩公司分歧
  4. 模型-市场分歧 — 隐含概率 vs 模型预测偏离度
  5. 市场异常信号 — 热门方偏重、赔率突变、套利窗口
  6. 赔率价值评估 — 凯利价值、期望值、价值缺口

输出特征:
  - 直接写入 match_features 表 (mkt_implied_home_prob, mkt_odds_drift 等)
  - 可追加到 SequenceBundle.static_features 供 DL 模型使用

数据来源:
  - odds 表: 1X2 赔率 + 返还率 (retrospective_elo / default)
  - odds_history 表: 赔率时间序列 (开盘/收盘区分)
  - The Odds API: 多博彩公司赔率对比 (需配置 API Key)
  - match_features 表: 已有 sigma_trap / p_implied 等

用法:
    from optimize.market_features import MarketFeatureExtractor
    mfx = MarketFeatureExtractor()
    features = mfx.compute_match_features(match_id=123)
    # features → dict of market features

    # 批量生成并写入
    df = mfx.generate_features_df()
    mfx.write_to_match_features(df)
"""

import sqlite3
import logging
import warnings
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
from datetime import datetime

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
# 赔率 API 抽象接口
# ════════════════════════════════════════════════════════════════

@dataclass
class OddsSnapshot:
    """单时间点赔率快照"""
    home_odds: float
    draw_odds: float
    away_odds: float
    return_rate: float = 0.95      # 返还率 (1 - 庄家抽水)
    asian_handicap: Optional[float] = None
    over_under: Optional[float] = None
    over_odds: Optional[float] = None
    under_odds: Optional[float] = None
    provider: str = ''
    timestamp: Optional[str] = None

@dataclass
class MultiBookmakerOdds:
    """多博彩公司赔率聚合"""
    match_id: int
    bookmakers: Dict[str, OddsSnapshot] = field(default_factory=dict)
    # 聚合统计 (延迟计算)
    _aggregated: Optional[Dict] = field(default=None, init=False, repr=False)

    def add_bookmaker(self, name: str, snapshot: OddsSnapshot):
        self.bookmakers[name] = snapshot
        self._aggregated = None  # 清缓存

    def aggregate(self) -> Dict:
        """计算多博彩公司聚合统计"""
        if self._aggregated is not None:
            return self._aggregated

        if not self.bookmakers:
            self._aggregated = {}
            return self._aggregated

        homes = [s.home_odds for s in self.bookmakers.values() if s.home_odds and s.home_odds > 1.0]
        draws = [s.draw_odds for s in self.bookmakers.values() if s.draw_odds and s.draw_odds > 1.0]
        aways = [s.away_odds for s in self.bookmakers.values() if s.away_odds and s.away_odds > 1.0]

        result = {
            'n_bookmakers': len(self.bookmakers),
            'home_odds_mean': float(np.mean(homes)) if homes else None,
            'home_odds_std': float(np.std(homes)) if len(homes) > 1 else 0.0,
            'home_odds_min': float(np.min(homes)) if homes else None,
            'home_odds_max': float(np.max(homes)) if homes else None,
            'draw_odds_mean': float(np.mean(draws)) if draws else None,
            'draw_odds_std': float(np.std(draws)) if len(draws) > 1 else 0.0,
            'away_odds_mean': float(np.mean(aways)) if aways else None,
            'away_odds_std': float(np.std(aways)) if len(aways) > 1 else 0.0,
            'away_odds_min': float(np.min(aways)) if aways else None,
            'away_odds_max': float(np.max(aways)) if aways else None,
        }

        # 博彩公司分歧度 (std/mean 变异系数)
        if result['home_odds_mean'] and result['home_odds_mean'] > 0:
            result['home_odds_cv'] = result['home_odds_std'] / result['home_odds_mean']
        else:
            result['home_odds_cv'] = 0.0
        if result['away_odds_mean'] and result['away_odds_mean'] > 0:
            result['away_odds_cv'] = result['away_odds_std'] / result['away_odds_mean']
        else:
            result['away_odds_cv'] = 0.0

        # 最优赔率隐含概率 (用各家最高赔率 → 最小抽水)
        if homes and draws and aways:
            best_home = max(homes)
            best_draw = max(draws)
            best_away = max(aways)
            raw_probs = [1.0/best_home, 1.0/best_draw, 1.0/best_away]
            total = sum(raw_probs)
            result['best_implied_home'] = raw_probs[0] / total
            result['best_implied_draw'] = raw_probs[1] / total
            result['best_implied_away'] = raw_probs[2] / total
            result['overround_best'] = total - 1.0  # 最优赔率下仍有抽水
        else:
            result['best_implied_home'] = None
            result['best_implied_draw'] = None
            result['best_implied_away'] = None
            result['overround_best'] = None

        self._aggregated = result
        return result

@dataclass
class OddsMovement:
    """赔率变动特征"""
    opening_home: Optional[float] = None
    opening_draw: Optional[float] = None
    opening_away: Optional[float] = None
    closing_home: Optional[float] = None
    closing_draw: Optional[float] = None
    closing_away: Optional[float] = None
    # 变动量 (closing - opening)
    drift_home: Optional[float] = None
    drift_draw: Optional[float] = None
    drift_away: Optional[float] = None
    # 变动速率 (每时间单位变动量)
    drift_rate_home: Optional[float] = None
    # 变动方向一致性 (3个赔率方向是否一致)
    direction_consistency: float = 0.0
    # 变动波动率 (时间序列标准差)
    volatility_home: float = 0.0
    volatility_draw: float = 0.0
    volatility_away: float = 0.0
    # 突变检测 (最大单步变动)
    max_jump_home: float = 0.0
    max_jump_draw: float = 0.0
    max_jump_away: float = 0.0

# ════════════════════════════════════════════════════════════════
# 赔率 API 接口 (为 The Odds API 预留)
# ════════════════════════════════════════════════════════════════

class OddsAPIInterface:
    """
    赔率 API 抽象接口 — 支持多数据源切换
    当前实现: DB (从数据库读取)
    预留: The Odds API, API-Football
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or _get_db_path()
        self._the_odds_client = None  # 延迟初始化

    @property
    def the_odds_available(self) -> bool:
        """The Odds API 是否可用"""
        try:
            import os
            return bool(os.environ.get('THE_ODDS_API_KEY', ''))
        except (Exception, requests.exceptions.RequestException):
            return False

    def _get_the_odds_client(self):
        """延迟加载 The Odds API 客户端"""
        if self._the_odds_client is None and self.the_odds_available:
            from data_collector.the_odds_client import TheOddsCollector
            self._the_odds_client = TheOddsCollector()
        return self._the_odds_client

    def get_odds_snapshot(self, match_id: int) -> Optional[OddsSnapshot]:
        """从数据库获取赔率快照"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute('''
                SELECT home_odds, draw_odds, away_odds, return_rate,
                       asian_handicap, over_under, over_odds, under_odds, provider
                FROM odds
                WHERE match_id = ? AND home_odds IS NOT NULL AND home_odds > 1.0
                ORDER BY
                    CASE provider WHEN 'retrospective_elo' THEN 0 ELSE 1 END,
                    odds_id DESC
                LIMIT 1
            ''', (match_id,)).fetchone()

            if not row:
                return None

            return OddsSnapshot(
                home_odds=row['home_odds'],
                draw_odds=row['draw_odds'],
                away_odds=row['away_odds'],
                return_rate=row['return_rate'] if row['return_rate'] and row['return_rate'] > 0.5 else 0.95,
                asian_handicap=row['asian_handicap'],
                over_under=row['over_under'],
                over_odds=row['over_odds'],
                under_odds=row['under_odds'],
                provider=row['provider'] or '',
            )

    def get_odds_history(self, match_id: int) -> List[OddsSnapshot]:
        """从数据库获取赔率时间序列"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT home_odds, draw_odds, away_odds, asian_handicap,
                       odds_timestamp, provider
                FROM odds_history
                WHERE match_id = ?
                ORDER BY odds_timestamp ASC
            ''', (match_id,)).fetchall()

        snapshots = []
        for r in rows:
            if r['home_odds'] and r['home_odds'] > 1.0:
                snapshots.append(OddsSnapshot(
                    home_odds=r['home_odds'],
                    draw_odds=r['draw_odds'],
                    away_odds=r['away_odds'],
                    return_rate=0.95,  # 历史记录默认
                    asian_handicap=r['asian_handicap'],
                    timestamp=r['odds_timestamp'],
                    provider=r['provider'] or '',
                ))
        return snapshots

    def get_multi_bookmaker_odds(self, match_id: int) -> MultiBookmakerOdds:
        """
        获取多博彩公司赔率
        优先使用 The Odds API (若可用)，否则从数据库按 provider 分组
        """
        multi = MultiBookmakerOdds(match_id=match_id)

        # 尝试 The Odds API
        client = self._get_the_odds_client()
        if client and client.is_configured:
            try:
                live_odds = client.get_live_odds('soccer_epl')
                # TODO: 解析并填充 MultiBookmakerOdds
            except (Exception) as e:
                logger.debug(f"The Odds API 调用失败 match_id={match_id}: {e}")

        # 回退: 从数据库获取
        if not multi.bookmakers:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute('''
                    SELECT home_odds, draw_odds, away_odds, return_rate,
                           asian_handicap, over_under, over_odds, under_odds,
                           provider, odds_timestamp
                    FROM odds
                    WHERE match_id = ? AND home_odds IS NOT NULL AND home_odds > 1.0
                ''', (match_id,)).fetchall()

            for r in rows:
                provider_key = r['provider'] or 'unknown'
                multi.add_bookmaker(provider_key, OddsSnapshot(
                    home_odds=r['home_odds'],
                    draw_odds=r['draw_odds'],
                    away_odds=r['away_odds'],
                    return_rate=r['return_rate'] if r['return_rate'] and r['return_rate'] > 0.5 else 0.95,
                    asian_handicap=r['asian_handicap'],
                    over_under=r['over_under'],
                    over_odds=r['over_odds'],
                    under_odds=r['under_odds'],
                    provider=provider_key,
                    timestamp=r['odds_timestamp'],
                ))

        return multi

# ════════════════════════════════════════════════════════════════
# 核心计算函数
# ════════════════════════════════════════════════════════════════

def implied_prob(odds: float, return_rate: float = 0.95) -> float:
    """
    从赔率计算隐含概率 (含返还率修正)
    implied = (1/odds) * return_rate
    """
    if odds <= 1.0 or return_rate <= 0:
        return 0.0
    return float((1.0 / odds) * return_rate)

def fair_prob(home_odds: float, draw_odds: float, away_odds: float) -> Tuple[float, float, float]:
    """
    去抽水公平概率 — 将3个隐含概率归一化
    fair = raw / (raw_h + raw_d + raw_a)
    """
    raw_h = 1.0 / home_odds if home_odds > 1.0 else 0.0
    raw_d = 1.0 / draw_odds if draw_odds > 1.0 else 0.0
    raw_a = 1.0 / away_odds if away_odds > 1.0 else 0.0
    total = raw_h + raw_d + raw_a
    if total <= 0:
        return (0.0, 0.0, 0.0)
    return (raw_h / total, raw_d / total, raw_a / total)

def overround(home_odds: float, draw_odds: float, away_odds: float) -> float:
    """
    抽水率 (overround) = Σ(1/odds) - 1
    正值 = 庄家利润; 接近0 = 公平市场; 负值 = 套利可能
    """
    total = 0.0
    if home_odds > 1.0:
        total += 1.0 / home_odds
    if draw_odds > 1.0:
        total += 1.0 / draw_odds
    if away_odds > 1.0:
        total += 1.0 / away_odds
    return total - 1.0

def kelly_value(model_prob: float, odds: float, return_rate: float = 0.95) -> float:
    """
    凯利价值: f* = (bp - q) / b
    b = odds - 1 (净赔率), p = model_prob, q = 1 - p
    返回: 正值=有价值投注, 负值=无价值
    """
    if odds <= 1.0 or model_prob <= 0:
        return 0.0
    b = odds - 1.0
    p = model_prob
    q = 1.0 - p
    return float((b * p - q) / b)

def expected_value(model_prob: float, odds: float) -> float:
    """
    期望值: EV = p * (odds - 1) - (1 - p)
    """
    if odds <= 1.0:
        return 0.0
    return float(model_prob * (odds - 1.0) - (1.0 - model_prob))

def compute_odds_movement(history: List[OddsSnapshot]) -> OddsMovement:
    """
    从赔率时间序列计算变动特征

    若只有1条记录: drift=0, volatility=0
    若有2+条: opening=第1条, closing=最后1条
    """
    mov = OddsMovement()

    if not history:
        return mov

    # 开盘/收盘
    mov.opening_home = history[0].home_odds
    mov.opening_draw = history[0].draw_odds
    mov.opening_away = history[0].away_odds

    mov.closing_home = history[-1].home_odds
    mov.closing_draw = history[-1].draw_odds
    mov.closing_away = history[-1].away_odds

    # 漂移量
    if len(history) > 1:
        mov.drift_home = mov.closing_home - mov.opening_home
        mov.drift_draw = mov.closing_draw - mov.opening_draw
        mov.drift_away = mov.closing_away - mov.opening_away

        # 变动速率 (按时间跨度归一化)
        n_points = len(history)
        if n_points > 1:
            mov.drift_rate_home = mov.drift_home / (n_points - 1)
            # 方向一致性: home和away变动方向相反 → 高一致性
            # (home odds下降 = 主胜更被看好 = away odds上升)
            directions = [np.sign(mov.drift_home), np.sign(mov.drift_draw), np.sign(mov.drift_away)]
            # 理想: home下降(-1) + away上升(+1) = -2 → 一致性高
            # 或: home上升(+1) + away下降(-1) = +2 → 一致性高
            consistency = abs(directions[0] - directions[2]) / 2.0  # 0或1
            if directions[1] == 0:  # draw 不变 → 增加一致性
                consistency = max(consistency, 0.5)
            mov.direction_consistency = consistency

        # 波动率 (对数收益率标准差)
        homes = [s.home_odds for s in history if s.home_odds and s.home_odds > 1.0]
        draws = [s.draw_odds for s in history if s.draw_odds and s.draw_odds > 1.0]
        aways = [s.away_odds for s in history if s.away_odds and s.away_odds > 1.0]

        if len(homes) > 1:
            log_ret = [np.log(homes[i] / homes[i-1]) for i in range(1, len(homes))]
            mov.volatility_home = float(np.std(log_ret))
            mov.max_jump_home = float(max(abs(r) for r in log_ret))
        if len(draws) > 1:
            log_ret = [np.log(draws[i] / draws[i-1]) for i in range(1, len(draws))]
            mov.volatility_draw = float(np.std(log_ret))
            mov.max_jump_draw = float(max(abs(r) for r in log_ret))
        if len(aways) > 1:
            log_ret = [np.log(aways[i] / aways[i-1]) for i in range(1, len(aways))]
            mov.volatility_away = float(np.std(log_ret))
            mov.max_jump_away = float(max(abs(r) for r in log_ret))

    return mov

# ════════════════════════════════════════════════════════════════
# 市场特征提取器
# ════════════════════════════════════════════════════════════════

class MarketFeatureExtractor:
    """
    市场特征提取器 — 从赔率数据提取深层市场信号

    特征维度:
      1. 隐含概率体系 (5 features)
      2. 赔率变动特征 (8 features)
      3. 市场信心指标 (4 features)
      4. 模型-市场分歧 (3 features)
      5. 市场异常信号 (4 features)
      6. 赔率价值评估 (3 features)
      总计: 27 个特征 (含 home/away 对称特征)
    """

    # 特征名前缀
    PREFIX = 'mkt_'

    def __init__(self, db_path: str = None):
        self.db_path = db_path or _get_db_path()
        self.api = OddsAPIInterface(self.db_path)

    # ─── 核心计算 ───

    def compute_match_features(self, match_id: int,
                                 model_probs: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """
        计算单场比赛的全部市场特征

        Args:
            match_id: 比赛ID
            model_probs: 模型预测概率 {'home': 0.5, 'draw': 0.25, 'away': 0.25}
                         若为 None, 分歧特征置 0

        Returns:
            dict of market feature name → float
        """
        features = {}

        # 1. 获取赔率快照
        snapshot = self.api.get_odds_snapshot(match_id)

        if snapshot is None:
            # 无赔率数据 — 用默认值填充
            return self._default_features()

        # 2. 获取赔率历史 (用于变动特征)
        history = self.api.get_odds_history(match_id)

        # 3. 获取多博彩公司数据
        multi = self.api.get_multi_bookmaker_odds(match_id)

        # ─── 模块1: 隐含概率体系 ───
        ip_home = implied_prob(snapshot.home_odds, snapshot.return_rate)
        ip_draw = implied_prob(snapshot.draw_odds, snapshot.return_rate)
        ip_away = implied_prob(snapshot.away_odds, snapshot.return_rate)

        fp_home, fp_draw, fp_away = fair_prob(snapshot.home_odds, snapshot.draw_odds, snapshot.away_odds)

        ov = overround(snapshot.home_odds, snapshot.draw_odds, snapshot.away_odds)

        features.update({
            'mkt_implied_home': round(ip_home, 4),
            'mkt_implied_draw': round(ip_draw, 4),
            'mkt_implied_away': round(ip_away, 4),
            'mkt_fair_home': round(fp_home, 4),
            'mkt_fair_draw': round(fp_draw, 4),
            'mkt_fair_away': round(fp_away, 4),
            'mkt_overround': round(ov, 4),
            'mkt_home_advantage': round(fp_home - fp_away, 4),  # 正值=市场看好主队
        })

        # ─── 模块2: 赔率变动特征 ───
        movement = compute_odds_movement(history)

        # 从 odds 表也有可能区分 opening/closing (provider=default vs retrospective_elo)
        if movement.drift_home is None:
            # 尝试从多 provider 推断变动
            movement = self._infer_movement_from_multi(multi, snapshot)

        features.update({
            'mkt_odds_drift_home': round(movement.drift_home or 0.0, 4),
            'mkt_odds_drift_draw': round(movement.drift_draw or 0.0, 4),
            'mkt_odds_drift_away': round(movement.drift_away or 0.0, 4),
            'mkt_drift_direction': round(movement.direction_consistency, 4),
            'mkt_volatility': round(max(movement.volatility_home, movement.volatility_away), 4),
            'mkt_max_jump': round(max(movement.max_jump_home, movement.max_jump_draw, movement.max_jump_away), 4),
            'mkt_drift_magnitude': round(
                np.sqrt((movement.drift_home or 0.0)**2 +
                        (movement.drift_draw or 0.0)**2 +
                        (movement.drift_away or 0.0)**2), 4),
        })

        # ─── 模块3: 市场信心指标 ───
        agg = multi.aggregate()
        n_bookmakers = agg.get('n_bookmakers', 1)
        home_cv = agg.get('home_odds_cv', 0.0)
        away_cv = agg.get('away_odds_cv', 0.0)

        # 赔率紧密度: 1 - mean(CV) → 越高表示博彩公司越一致
        tightness = max(0.0, 1.0 - (home_cv + away_cv) / 2.0)

        features.update({
            'mkt_bookmaker_count': float(n_bookmakers),
            'mkt_tightness': round(tightness, 4),       # 博彩公司一致性
            'mkt_home_cv': round(home_cv, 4),            # 主胜赔率变异系数
            'mkt_away_cv': round(away_cv, 4),             # 客胜赔率变异系数
        })

        # ─── 模块4: 模型-市场分歧 ───
        if model_probs:
            mp_home = model_probs.get('home', 0.33)
            mp_draw = model_probs.get('draw', 0.33)
            mp_away = model_probs.get('away', 0.33)

            divergence_home = mp_home - fp_home
            divergence_away = mp_away - fp_away
            # 总分歧度 (KL散度近似)
            kl_approx = 0.0
            for mp, fp in [(mp_home, fp_home), (mp_draw, fp_draw), (mp_away, fp_away)]:
                if mp > 0 and fp > 0:
                    kl_approx += mp * np.log(mp / fp)

            features.update({
                'mkt_divergence_home': round(divergence_home, 4),   # 正=模型更看好主队
                'mkt_divergence_away': round(divergence_away, 4),
                'mkt_kl_divergence': round(kl_approx, 4),
            })
        else:
            features.update({
                'mkt_divergence_home': 0.0,
                'mkt_divergence_away': 0.0,
                'mkt_kl_divergence': 0.0,
            })

        # ─── 模块5: 市场异常信号 ───
        # 热门方偏重: 隐含概率最低方(赔率最低)的偏离程度
        min_odds = min(snapshot.home_odds, snapshot.away_odds)
        fav_prob = 1.0 / min_odds if min_odds > 1.0 else 0.5
        fav_heaviness = max(0.0, fav_prob - 0.5)  # 热门方超过50%的部分

        # 赔率对称性: |home_odds - away_odds| / mean → 越高越不对称
        odds_symmetry = 0.0
        if snapshot.home_odds > 1.0 and snapshot.away_odds > 1.0:
            odds_mean = (snapshot.home_odds + snapshot.away_odds) / 2.0
            odds_symmetry = abs(snapshot.home_odds - snapshot.away_odds) / odds_mean

        # 平局偏离: 平局赔率偏离期望值的程度
        draw_deviation = 0.0
        if fp_draw > 0:
            draw_deviation = abs(fp_draw - 0.26) / 0.26  # 经验值: ~26%

        features.update({
            'mkt_fav_heaviness': round(fav_heaviness, 4),      # 热门方偏重
            'mkt_odds_asymmetry': round(odds_symmetry, 4),       # 赔率不对称度
            'mkt_draw_deviation': round(draw_deviation, 4),      # 平局偏离
            'mkt_value_signal': round(fav_heaviness * odds_symmetry, 4),  # 价值信号
        })

        # ─── 模块6: 赔率价值评估 ───
        if model_probs:
            kv_home = kelly_value(model_probs.get('home', 0.33), snapshot.home_odds, snapshot.return_rate)
            kv_away = kelly_value(model_probs.get('away', 0.33), snapshot.away_odds, snapshot.return_rate)
            ev_home = expected_value(model_probs.get('home', 0.33), snapshot.home_odds)
        else:
            kv_home = 0.0
            kv_away = 0.0
            ev_home = 0.0

        features.update({
            'mkt_kelly_home': round(kv_home, 4),
            'mkt_kelly_away': round(kv_away, 4),
            'mkt_ev_home': round(ev_home, 4),
        })

        return features

    def _infer_movement_from_multi(self, multi: MultiBookmakerOdds,
                                    current: OddsSnapshot) -> OddsMovement:
        """
        从多 provider 赔率推断变动
        策略: default provider (较早) vs retrospective_elo (较新) 作为 opening/closing
        """
        mov = OddsMovement()

        if 'default' in multi.bookmakers and 'retrospective_elo' in multi.bookmakers:
            opening = multi.bookmakers['default']
            closing = multi.bookmakers['retrospective_elo']

            mov.opening_home = opening.home_odds
            mov.opening_draw = opening.draw_odds
            mov.opening_away = opening.away_odds
            mov.closing_home = closing.home_odds
            mov.closing_draw = closing.draw_odds
            mov.closing_away = closing.away_odds

            mov.drift_home = (closing.home_odds or 0) - (opening.home_odds or 0)
            mov.drift_draw = (closing.draw_odds or 0) - (opening.draw_odds or 0)
            mov.drift_away = (closing.away_odds or 0) - (opening.away_odds or 0)

            dirs = [np.sign(mov.drift_home or 0), np.sign(mov.drift_draw or 0), np.sign(mov.drift_away or 0)]
            mov.direction_consistency = abs(dirs[0] - dirs[2]) / 2.0

        elif len(multi.bookmakers) >= 1:
            # 只有1个 provider — 无变动信息
            pass

        return mov

    def _default_features(self) -> Dict[str, float]:
        """无赔率数据时的默认值"""
        return {
            'mkt_implied_home': 0.0,
            'mkt_implied_draw': 0.0,
            'mkt_implied_away': 0.0,
            'mkt_fair_home': 0.0,
            'mkt_fair_draw': 0.0,
            'mkt_fair_away': 0.0,
            'mkt_overround': 0.0,
            'mkt_home_advantage': 0.0,
            'mkt_odds_drift_home': 0.0,
            'mkt_odds_drift_draw': 0.0,
            'mkt_odds_drift_away': 0.0,
            'mkt_drift_direction': 0.0,
            'mkt_volatility': 0.0,
            'mkt_max_jump': 0.0,
            'mkt_drift_magnitude': 0.0,
            'mkt_bookmaker_count': 0.0,
            'mkt_tightness': 0.0,
            'mkt_home_cv': 0.0,
            'mkt_away_cv': 0.0,
            'mkt_divergence_home': 0.0,
            'mkt_divergence_away': 0.0,
            'mkt_kl_divergence': 0.0,
            'mkt_fav_heaviness': 0.0,
            'mkt_odds_asymmetry': 0.0,
            'mkt_draw_deviation': 0.0,
            'mkt_value_signal': 0.0,
            'mkt_kelly_home': 0.0,
            'mkt_kelly_away': 0.0,
            'mkt_ev_home': 0.0,
        }

    # ─── 批量生成 ───

    def generate_features_df(self, match_ids: Optional[List[int]] = None,
                               model_probs_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        """
        批量生成市场特征 DataFrame

        Args:
            match_ids: 要计算的比赛ID列表 (None=全部)
            model_probs_df: 模型预测概率 DataFrame, 需含 match_id, home_prob, draw_prob, away_prob

        Returns:
            DataFrame with match_id + market features
        """
        with sqlite3.connect(self.db_path) as conn:
            if match_ids is not None:
                placeholders = ','.join(['?'] * len(match_ids))
                df_matches = pd.read_sql(
                    f'SELECT match_id FROM matches WHERE match_id IN ({placeholders})',
                    conn, params=match_ids
                )
            else:
                df_matches = pd.read_sql('SELECT match_id FROM matches', conn)

        all_match_ids = df_matches['match_id'].tolist()

        # 合并模型概率
        probs_map = {}
        if model_probs_df is not None:
            for _, row in model_probs_df.iterrows():
                probs_map[row['match_id']] = {
                    'home': row.get('home_prob', 0.33),
                    'draw': row.get('draw_prob', 0.33),
                    'away': row.get('away_prob', 0.33),
                }

        # 也尝试从 predictions 表获取模型概率
        if not probs_map:
            with sqlite3.connect(self.db_path) as conn:
                try:
                    df_preds = pd.read_sql(
                        'SELECT match_id, home_prob, draw_prob, away_prob FROM predictions',
                        conn
                    )
                    for _, row in df_preds.iterrows():
                        if row['home_prob'] and row['draw_prob'] and row['away_prob']:
                            probs_map[row['match_id']] = {
                                'home': row['home_prob'],
                                'draw': row['draw_prob'],
                                'away': row['away_prob'],
                            }
                    logger.info(f"从 predictions 表获取 {len(probs_map)} 场模型概率")
                except (Exception, KeyError, IndexError):
                    pass

        # 批量计算
        results = []
        total = len(all_match_ids)
        for i, mid in enumerate(all_match_ids):
            model_p = probs_map.get(mid)
            feat = self.compute_match_features(mid, model_probs=model_p)
            feat['match_id'] = mid
            results.append(feat)
            if (i + 1) % 2000 == 0:
                logger.info(f"市场特征进度: {i+1}/{total}")

        df = pd.DataFrame(results)

        # 确保列顺序
        cols = ['match_id'] + [c for c in df.columns if c != 'match_id']
        df = df[cols]

        logger.info(f"市场特征生成完成: {len(df)} 场, {len(df.columns)-1} 特征")
        return df

    # ─── 特征管道集成 ───

    def write_to_match_features(self, df: pd.DataFrame):
        """
        将市场特征写入 match_features 表
        - 动态 ALTER TABLE ADD COLUMN 新增列
        - 按 match_id UPDATE 已有行
        """
        if df.empty:
            logger.warning("市场特征 DataFrame 为空，跳过写入")
            return

        feature_cols = [c for c in df.columns if c != 'match_id']
        with sqlite3.connect(self.db_path) as conn:
            # 获取已有列
            existing = {r[1] for r in conn.execute('PRAGMA table_info(match_features)').fetchall()}

            # 动态迁移
            for col in feature_cols:
                if col not in existing:
                    conn.execute(f'ALTER TABLE match_features ADD COLUMN {col} REAL DEFAULT 0.0')
                    logger.info(f"  + 列 {col}")

            # 批量 UPDATE
            updated = 0
            for _, row in df.iterrows():
                mid = int(row['match_id'])
                sets = ', '.join(f'{col} = ?' for col in feature_cols)
                vals = [float(row[col]) for col in feature_cols]
                conn.execute(
                    f'UPDATE match_features SET {sets} WHERE match_id = ?',
                    vals + [mid]
                )
                updated += 1

            conn.commit()
            logger.info(f"市场特征写入完成: {updated} 场, {len(feature_cols)} 列")

    def augment_bundle(self, bundle, df: pd.DataFrame, mode: str = 'diff'):
        """
        将市场特征追加到 SequenceBundle.static_features

        Args:
            bundle: SequenceBundle 实例
            df: 市场特征 DataFrame (含 match_id)
            mode: 'diff' (仅对比特征), 'full' (含 home/away 对称)
        """
        if df.empty:
            return

        feature_cols = [c for c in df.columns if c != 'match_id']

        # 选择模式
        if mode == 'diff':
            # 仅保留对比/汇总特征 (减少 DL 模型维度)
            selected = [c for c in feature_cols if not c.startswith('mkt_implied_h')
                        and not c.startswith('mkt_fair_h')
                        and c not in ('mkt_implied_draw', 'mkt_fair_draw', 'mkt_implied_away', 'mkt_fair_away',
                                      'mkt_home_cv', 'mkt_away_cv')]
            # 总是保留 home/away 概率差
            for must_have in ['mkt_implied_home', 'mkt_implied_away', 'mkt_fair_home', 'mkt_fair_away',
                              'mkt_home_advantage', 'mkt_overround', 'mkt_divergence_home', 'mkt_divergence_away']:
                if must_have in feature_cols and must_have not in selected:
                    selected.append(must_have)
        else:
            selected = feature_cols

        # 索引
        df_indexed = df.set_index('match_id')

        # 获取 match_ids
        if hasattr(bundle, 'match_ids'):
            match_ids = bundle.match_ids
        elif hasattr(bundle, 'static_features') and hasattr(bundle, 'static_feature_names'):
            # 从 match_features 表反查
            with sqlite3.connect(self.db_path) as conn:
                mids = conn.execute('SELECT match_id FROM match_features ORDER BY feature_id').fetchall()
                match_ids = [r[0] for r in mids]
        else:
            logger.warning("无法获取 bundle 的 match_ids，跳过 augment")
            return

        # 构建特征矩阵
        new_features = []
        valid_count = 0
        for mid in match_ids:
            if mid in df_indexed.index:
                row = df_indexed.loc[mid]
                new_features.append([float(row.get(c, 0.0)) for c in selected])
                valid_count += 1
            else:
                new_features.append([0.0] * len(selected))

        if valid_count == 0:
            logger.warning("无有效 match_id 匹配，跳过 augment")
            return

        new_arr = np.array(new_features, dtype=np.float32)

        # 拼接到 static_features
        if hasattr(bundle, 'static_features') and bundle.static_features is not None:
            if len(bundle.static_features.shape) == 1:
                # 单样本: (D,) → (1, D+new)
                old = bundle.static_features.reshape(1, -1)
                new_arr_single = new_arr[:1] if new_arr.shape[0] == 1 else new_arr
                bundle.static_features = np.hstack([old, new_arr_single]).flatten()
            else:
                # 多样本: (N, D) → (N, D+new)
                bundle.static_features = np.hstack([bundle.static_features, new_arr])

        # 更新特征名
        if hasattr(bundle, 'static_feature_names'):
            prefix = 'mkt_'
            bundle.static_feature_names.extend([f'{prefix}{c.replace("mkt_", "")}' for c in selected])

        logger.info(f"市场特征 augment 完成: +{len(selected)} 维 ({valid_count}/{len(match_ids)} 有效)")

    # ─── 特征相关性分析 ───

    def correlation_analysis(self, df: pd.DataFrame = None,
                              target_col: str = None) -> pd.DataFrame:
        """
        市场特征相关性分析

        Args:
            df: 市场特征 DataFrame (None=自动生成)
            target_col: 目标列名 (如 'home_win')

        Returns:
            相关性矩阵 DataFrame
        """
        if df is None:
            df = self.generate_features_df()

        feature_cols = [c for c in df.columns if c != 'match_id' and c != target_col]

        # 内部相关性
        corr_matrix = df[feature_cols].corr()

        # 与目标的相关性
        if target_col and target_col in df.columns:
            target_corr = df[feature_cols + [target_col]].corr()[target_col].drop(target_col)
            corr_matrix['target_corr'] = target_corr

            # 排序
            sorted_features = target_corr.abs().sort_values(ascending=False).index.tolist()
            logger.info("市场特征与目标相关性 (Top 10):")
            for f in sorted_features[:10]:
                logger.info(f"  {f}: {target_corr[f]:.4f}")

        # 内部高相关性对 (|r| > 0.8)
        high_corr_pairs = []
        for i, c1 in enumerate(feature_cols):
            for c2 in feature_cols[i+1:]:
                r = corr_matrix.loc[c1, c2]
                if abs(r) > 0.8:
                    high_corr_pairs.append((c1, c2, round(r, 3)))

        if high_corr_pairs:
            logger.info(f"高相关性对 (|r|>0.8): {len(high_corr_pairs)}")
            for c1, c2, r in high_corr_pairs[:5]:
                logger.info(f"  {c1} ↔ {c2}: r={r}")

        return corr_matrix

    def feature_importance_proxy(self, df: pd.DataFrame = None) -> pd.Series:
        """
        特征重要性代理 (基于方差和信息熵)
        高方差 + 低缺失率 = 更重要的特征
        """
        if df is None:
            df = self.generate_features_df()

        feature_cols = [c for c in df.columns if c != 'match_id']
        stats = pd.DataFrame({
            'std': df[feature_cols].std(),
            'nonzero_pct': (df[feature_cols] != 0).mean(),
            'mean_abs': df[feature_cols].abs().mean(),
        })

        # 归一化并加权
        stats['std_norm'] = stats['std'] / stats['std'].max()
        stats['nonzero_norm'] = stats['nonzero_pct'] / stats['nonzero_pct'].max()
        stats['importance'] = 0.5 * stats['std_norm'] + 0.5 * stats['nonzero_norm']

        importance = stats['importance'].sort_values(ascending=False)

        logger.info("市场特征重要性代理 (Top 10):")
        for f, v in importance.head(10).items():
            logger.info(f"  {f}: {v:.4f}")

        return importance

# ════════════════════════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════════════════════════

def compute_market_features(match_id: int,
                              model_probs: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """单场快捷计算"""
    mfx = MarketFeatureExtractor()
    return mfx.compute_match_features(match_id, model_probs)

def generate_market_features(match_ids: Optional[List[int]] = None,
                               model_probs_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """批量生成"""
    mfx = MarketFeatureExtractor()
    return mfx.generate_features_df(match_ids, model_probs_df)

# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    if len(sys.argv) > 1 and sys.argv[1] == 'analyze':
        print("=" * 60)
        print("T14 市场特征相关性分析")
        print("=" * 60)
        mfx = MarketFeatureExtractor()
        df = mfx.generate_features_df()
        print(f"\n数据: {len(df)} 场, {len(df.columns)-1} 特征")
        print(f"\n特征统计:")
        print(df.describe().T.to_string())

        # 相关性分析
        corr = mfx.correlation_analysis(df)

        # 重要性代理
        imp = mfx.feature_importance_proxy(df)
        print(f"\n特征重要性:")
        print(imp.to_string())

    elif len(sys.argv) > 1 and sys.argv[1] == 'write':
        mfx = MarketFeatureExtractor()
        df = mfx.generate_features_df()
        mfx.write_to_match_features(df)
        print(f"写入完成: {len(df)} 场")

    else:
        # 单场测试
        mfx = MarketFeatureExtractor()
        with sqlite3.connect(mfx.db_path) as conn:
            mid = conn.execute(
                'SELECT o.match_id FROM odds o WHERE o.home_odds > 1.0 LIMIT 1'
            ).fetchone()[0]

        feat = mfx.compute_match_features(mid)
        print(f"\n比赛 {mid} 市场特征:")
        for k, v in sorted(feat.items()):
            if v != 0.0:
                print(f"  {k}: {v}")
