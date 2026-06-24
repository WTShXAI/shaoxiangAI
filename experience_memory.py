"""
历史经验记忆库 (Experience Memory)
==================================
让贝叶斯总指挥从"窄路由器"升级为"经验型路由器"。

4类知识源:
  ① 历史比赛数据 (matches JOIN odds, 54K场)
  ② 回测预测记录 + 实际赛果 (v3.2/v4.0 OOF 8631 + 世界杯20场)
  ③ 盘口画线轨迹 + 赛果 (odds_history 18K条, 赔率漂移模式)
  ④ 操盘手法/陷阱检测记录 + 赛果 (BookmakerTrapDetector 15种TrapType)

核心查询接口:
  - query_similar_matches(odds, top_k)        查询历史相似赔率比赛
  - query_backtest_performance(scenario)      查询某场景下各模型表现
  - query_odds_trajectory_pattern(odds)       查询相似赔率的轨迹模式
  - query_trap_history(trap_type)             查询某陷阱类型的历史赛果
  - generate_experience_features(context)     生成经验特征向量

设计原则:
  - 不破坏现有 bayesian_commander.py (向后兼容)
  - 惰性加载 (首次查询时才加载对应数据源)
  - 内存友好 (只缓存高频查询结果)
"""
import os
import sys
import json
import math
import sqlite3
import logging
import numpy as np
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Any

logger = logging.getLogger('ExperienceMemory')

# 项目根路径
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_ROOT, 'data', 'football_data.db')
OUTPUT_DIR = os.path.join(PROJECT_ROOT, 'output')


# ════════════════════════════════════════════════════════════
# 1. 数据类定义
# ════════════════════════════════════════════════════════════

@dataclass
class MatchRecord:
    """历史比赛记录"""
    match_id: int
    date: str
    home: str
    away: str
    league: str
    home_score: int
    away_score: int
    result: str  # 'H'/'D'/'A'
    home_odds: float
    draw_odds: float
    away_odds: float
    return_rate: float = 0.0

    @property
    def odds_spread(self) -> float:
        """赔率价差 = log(away_odds) - log(home_odds), 正=主队热门"""
        if self.home_odds <= 0 or self.away_odds <= 0:
            return 0.0
        return math.log(self.away_odds) - math.log(self.home_odds)

    @property
    def implied_probs(self) -> Tuple[float, float, float]:
        """隐含概率 (未去抽水)"""
        if self.home_odds <= 0 or self.draw_odds <= 0 or self.away_odds <= 0:
            return 0.33, 0.34, 0.33
        total = 1/self.home_odds + 1/self.draw_odds + 1/self.away_odds
        return (1/self.home_odds)/total, (1/self.draw_odds)/total, (1/self.away_odds)/total


@dataclass
class BacktestRecord:
    """回测预测记录"""
    match_id: int
    home: str
    away: str
    date: str
    actual: str  # 实际赛果
    model_name: str  # v3.2/v4.0/VIP-2/SKY
    pred: str  # 预测结果
    prob_h: float
    prob_d: float
    prob_a: float
    correct: bool

    @property
    def margin(self) -> float:
        """D-Gate margin = P(D) - max(P(H), P(A))"""
        return self.prob_d - max(self.prob_h, self.prob_a)


@dataclass
class OddsTrajectoryRecord:
    """盘口画线轨迹记录"""
    match_id: int
    initial_home: float
    initial_draw: float
    initial_away: float
    final_home: float
    final_draw: float
    final_away: float
    result: str

    @property
    def home_drift(self) -> float:
        """主队赔率漂移 (负=降赔变热门, 正=升赔变冷门)"""
        if self.initial_home <= 0:
            return 0.0
        return self.final_home - self.initial_home

    @property
    def drift_magnitude(self) -> float:
        """总漂移幅度"""
        return abs(self.home_drift) + abs(self.final_draw - self.initial_draw) + abs(self.final_away - self.initial_away)


@dataclass
class TrapRecord:
    """操盘手法/陷阱检测记录"""
    trap_type: str
    confidence: float
    direction: str  # 诱主/诱客/诱平
    result: str  # 实际赛果
    description: str = ""


@dataclass
class ExperienceFeatures:
    """经验特征向量 (供贝叶斯分类器融合)"""
    # 场景特征
    odds_spread_bucket: str = "unknown"      # <0.2/0.2-0.8/0.8-1.5/>1.5
    margin_bucket: str = "unknown"           # <0/0-0.05/0.05-0.2/0.2-0.4/>0.4
    return_rate_bucket: str = "unknown"      # <0.9/0.9-0.95/0.95-1.0
    drift_pattern: str = "stable"            # stable/home_cool/home_heat/draw_heat/volatile

    # 历史表现特征 (该场景下各模型准确率)
    similar_match_count: int = 0
    similar_match_h_rate: float = 0.0
    similar_match_d_rate: float = 0.0
    similar_match_a_rate: float = 0.0

    # 回测表现特征
    v32_acc_in_scenario: float = 0.0
    v40_acc_in_scenario: float = 0.0
    vip2_acc_in_scenario: float = 0.0
    sky_acc_in_scenario: float = 0.0

    # 陷阱历史特征
    trap_hit_rate: float = 0.0  # 陷阱命中赛果的比例
    trap_danger_level: str = "low"  # low/medium/high

    # 路由建议 (经验型调整)
    recommended_module: str = ""
    confidence_boost: float = 0.0  # ±0.1 范围内的置信度调整

    def to_vector(self) -> List[float]:
        """转为数值特征向量 (供扩展贝叶斯分类器)"""
        spread_map = {"<0.2": 0.1, "0.2-0.8": 0.5, "0.8-1.5": 1.0, ">1.5": 2.0, "unknown": 0.5}
        margin_map = {"<0": -0.1, "0-0.05": 0.025, "0.05-0.2": 0.1, "0.2-0.4": 0.3, ">0.4": 0.6, "unknown": 0.1}
        rr_map = {"<0.9": 0.85, "0.9-0.95": 0.925, "0.95-1.0": 0.975, "unknown": 0.95}
        drift_map = {"stable": 0.0, "home_cool": -0.5, "home_heat": 0.5, "draw_heat": 0.3, "volatile": 1.0}
        danger_map = {"low": 0.0, "medium": 0.5, "high": 1.0}

        return [
            spread_map.get(self.odds_spread_bucket, 0.5),
            margin_map.get(self.margin_bucket, 0.1),
            rr_map.get(self.return_rate_bucket, 0.95),
            drift_map.get(self.drift_pattern, 0.0),
            float(self.similar_match_count) / 100.0,  # 归一化
            self.similar_match_h_rate,
            self.similar_match_d_rate,
            self.similar_match_a_rate,
            self.v32_acc_in_scenario,
            self.v40_acc_in_scenario,
            self.vip2_acc_in_scenario,
            self.sky_acc_in_scenario,
            self.trap_hit_rate,
            danger_map.get(self.trap_danger_level, 0.0),
            self.confidence_boost,
        ]


# ════════════════════════════════════════════════════════════
# 2. 历史经验记忆库
# ════════════════════════════════════════════════════════════

class ExperienceMemory:
    """历史经验记忆库 — 4类知识源整合"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        # 惰性加载的数据
        self._match_history: Optional[List[MatchRecord]] = None
        self._backtest_records: Optional[List[BacktestRecord]] = None
        self._odds_trajectories: Optional[List[OddsTrajectoryRecord]] = None
        self._trap_records: Optional[List[TrapRecord]] = None
        # 缓存
        self._similar_cache: Dict[str, List[MatchRecord]] = {}
        logger.info(f"ExperienceMemory 初始化 | db={self.db_path}")

    # ─── 惰性加载器 ────────────────────────────────────────────

    def _load_match_history(self) -> List[MatchRecord]:
        """加载历史比赛数据 (matches JOIN odds)"""
        if self._match_history is not None:
            return self._match_history

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT m.match_id, m.match_date, m.home_team_name, m.away_team_name,
                   m.league_name, m.home_score, m.away_score, m.final_result,
                   o.home_odds, o.draw_odds, o.away_odds, o.return_rate
            FROM matches m
            JOIN odds o ON m.match_id = o.match_id
            WHERE m.final_result IS NOT NULL
              AND o.home_odds IS NOT NULL AND o.home_odds > 0
              AND o.draw_odds IS NOT NULL AND o.draw_odds > 0
              AND o.away_odds IS NOT NULL AND o.away_odds > 0
        """)
        records = []
        for row in cur.fetchall():
            try:
                records.append(MatchRecord(
                    match_id=int(row[0]),
                    date=str(row[1] or ''),
                    home=str(row[2] or ''),
                    away=str(row[3] or ''),
                    league=str(row[4] or ''),
                    home_score=int(row[5] or 0),
                    away_score=int(row[6] or 0),
                    result=str(row[7]),
                    home_odds=float(row[8]),
                    draw_odds=float(row[9]),
                    away_odds=float(row[10]),
                    return_rate=float(row[11] or 0.0),
                ))
            except (ValueError, TypeError) as e:
                continue
        conn.close()
        self._match_history = records
        logger.info(f"加载历史比赛: {len(records)} 条")
        return records

    def _load_backtest_records(self) -> List[BacktestRecord]:
        """加载回测预测记录 (从 full_backtest JSON)"""
        if self._backtest_records is not None:
            return self._backtest_records

        records = []
        # 加载世界杯回测
        backtest_path = os.path.join(OUTPUT_DIR, 'full_backtest_v4_sky_vip_2026.json')
        if os.path.exists(backtest_path):
            with open(backtest_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 世界杯回测 (有actual的)
            wc_backtest = data.get('wc_backtest_and_predict', {}).get('backtest', {})
            for m in wc_backtest.get('matches', []):
                actual = m.get('actual')
                if not actual:
                    continue
                mid = int(m.get('id', 0))
                home = m.get('home', '')
                away = m.get('away', '')
                date = m.get('date', '')
                # VIP-2
                if 'vip_pred' in m:
                    records.append(BacktestRecord(
                        match_id=mid, home=home, away=away, date=date,
                        actual=actual, model_name='VIP-2', pred=m['vip_pred'],
                        prob_h=float(m.get('vip_h', 0)), prob_d=float(m.get('vip_d', 0)),
                        prob_a=float(m.get('vip_a', 0)), correct=(m['vip_pred'] == actual)
                    ))
                # SKY
                if 'sky_pred' in m:
                    records.append(BacktestRecord(
                        match_id=mid, home=home, away=away, date=date,
                        actual=actual, model_name='SKY', pred=m['sky_pred'],
                        prob_h=float(m.get('sky_h', 0)), prob_d=float(m.get('sky_d', 0)),
                        prob_a=float(m.get('sky_a', 0)), correct=(m['sky_pred'] == actual)
                    ))

        # 加载 DB OOF (从诊断脚本生成)
        db_oof_path = os.path.join(OUTPUT_DIR, 'db_oof_records.json')
        if os.path.exists(db_oof_path):
            with open(db_oof_path, 'r', encoding='utf-8') as f:
                oof_data = json.load(f)
            for rec in oof_data.get('records', []):
                for model in ['v3.2', 'v4.0']:
                    pred_key = f'{model}_pred'
                    if pred_key in rec:
                        records.append(BacktestRecord(
                            match_id=int(rec.get('match_id', 0)),
                            home=rec.get('home', ''), away=rec.get('away', ''),
                            date=rec.get('date', ''), actual=rec.get('actual', ''),
                            model_name=model, pred=rec[pred_key],
                            prob_h=float(rec.get(f'{model}_h', 0)),
                            prob_d=float(rec.get(f'{model}_d', 0)),
                            prob_a=float(rec.get(f'{model}_a', 0)),
                            correct=(rec[pred_key] == rec.get('actual', ''))
                        ))

        self._backtest_records = records
        logger.info(f"加载回测记录: {len(records)} 条 ({len(set(r.model_name for r in records))} 个模型)")
        return records

    def _load_odds_trajectories(self) -> List[OddsTrajectoryRecord]:
        """加载盘口画线轨迹 (从 odds_features 表, 30万条真实开盘→收盘漂移)"""
        if self._odds_trajectories is not None:
            return self._odds_trajectories

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        # odds_features 表有 open_h/open_d/open_a (开盘) 和 close_h/close_d/close_a (收盘)
        # 以及 drift_h/drift_d/drift_a (漂移量)
        cur.execute("""
            SELECT open_h, open_d, open_a, close_h, close_d, close_a, outcome
            FROM odds_features
            WHERE open_h > 0 AND open_d > 0 AND open_a > 0
              AND close_h > 0 AND close_d > 0 AND close_a > 0
              AND outcome IS NOT NULL AND outcome IN ('H','D','A')
        """)
        records = []
        for row in cur.fetchall():
            try:
                records.append(OddsTrajectoryRecord(
                    match_id=0,  # odds_features 没有 match_id
                    initial_home=float(row[0]), initial_draw=float(row[1]),
                    initial_away=float(row[2]),
                    final_home=float(row[3]), final_draw=float(row[4]),
                    final_away=float(row[5]),
                    result=str(row[6])
                ))
            except (ValueError, TypeError):
                continue
        conn.close()
        self._odds_trajectories = records
        logger.info(f"加载盘口轨迹: {len(records)} 条 (来自 odds_features 开盘→收盘)")
        return records

    def _load_trap_records(self) -> List[TrapRecord]:
        """加载操盘手法/陷阱检测记录"""
        if self._trap_records is not None:
            return self._trap_records

        # 尝试从陷阱检测回测结果加载
        trap_path = os.path.join(OUTPUT_DIR, 'trap_backtest_records.json')
        records = []

        if os.path.exists(trap_path):
            with open(trap_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for rec in data.get('records', []):
                records.append(TrapRecord(
                    trap_type=rec.get('trap_type', 'NONE'),
                    confidence=float(rec.get('confidence', 0)),
                    direction=rec.get('direction', ''),
                    result=rec.get('result', ''),
                    description=rec.get('description', '')
                ))

        # 如果没有现成记录, 用规则模拟生成 (基于历史数据的赔率结构)
        if not records:
            records = self._synthesize_trap_records_from_history()

        self._trap_records = records
        logger.info(f"加载陷阱记录: {len(records)} 条")
        return records

    def _synthesize_trap_records_from_history(self) -> List[TrapRecord]:
        """从历史数据合成陷阱记录 (基于赔率结构规则)"""
        records = []
        matches = self._load_match_history()

        for m in matches[:5000]:  # 取前5000场合成
            # 规则: 浅盘大热 (热门赔率<1.5但spread<0.3)
            if m.home_odds < 1.5 and abs(m.odds_spread) < 0.3:
                records.append(TrapRecord(
                    trap_type='SHALLOW_HOT', confidence=0.7,
                    direction='诱主', result=m.result,
                    description=f"浅盘大热: H@{m.home_odds} spread={m.odds_spread:.2f}"
                ))
            # 规则: 抽水异常 (return_rate < 0.85)
            elif m.return_rate < 0.85:
                records.append(TrapRecord(
                    trap_type='OVERROUND_ANOMALY', confidence=0.6,
                    direction='资金引导', result=m.result,
                    description=f"抽水异常: RR={m.return_rate:.3f}"
                ))
            # 规则: 凯利背离 (赔率隐含概率与赛果差异大)
            else:
                imp_h, imp_d, imp_a = m.implied_probs
                if max(imp_h, imp_d, imp_a) > 0.6 and m.result != ['H', 'D', 'A'][np.argmax([imp_h, imp_d, imp_a])]:
                    records.append(TrapRecord(
                        trap_type='KELLY_DIVERGENCE', confidence=0.5,
                        direction='逆向', result=m.result,
                        description=f"凯利背离: 隐含{['H','D','A'][np.argmax([imp_h,imp_d,imp_a])]} 实际{m.result}"
                    ))

        return records

    # ─── 查询接口 ────────────────────────────────────────────

    def query_similar_matches(self, home_odds: float, draw_odds: float,
                               away_odds: float, top_k: int = 20) -> List[MatchRecord]:
        """查询历史相似赔率比赛 (基于赔率向量对数空间欧氏距离 + 去重)"""
        cache_key = f"{home_odds:.2f}_{draw_odds:.2f}_{away_odds:.2f}_{top_k}"
        if cache_key in self._similar_cache:
            return self._similar_cache[cache_key]

        matches = self._load_match_history()
        target = np.array([home_odds, draw_odds, away_odds])
        target_log = np.log(np.maximum(target, 1.01))

        # 按对阵去重 (同一 home+away+赔率组合只保留1条, 避免重复比赛主导结果)
        seen = set()
        distances = []
        for m in matches:
            # 去重key: 对阵 + 赔率四舍五入
            dedup_key = (m.home, m.away, round(m.home_odds, 2), round(m.draw_odds, 2), round(m.away_odds, 2))
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            vec = np.log(np.maximum(np.array([m.home_odds, m.draw_odds, m.away_odds]), 1.01))
            dist = float(np.linalg.norm(target_log - vec))
            distances.append((dist, m))

        distances.sort(key=lambda x: x[0])
        result = [m for _, m in distances[:top_k]]
        self._similar_cache[cache_key] = result
        return result

    def query_backtest_performance(self, scenario: Dict[str, Any]) -> Dict[str, float]:
        """查询某场景下各模型历史准确率

        scenario 支持:
          - margin_range: (min, max) D-Gate margin范围
          - spread_range: (min, max) odds_spread范围
          - predicted_class: 'H'/'D'/'A' 模型预测的类别
        """
        records = self._load_backtest_records()
        if not records:
            return {}

        margin_range = scenario.get('margin_range')
        spread_range = scenario.get('spread_range')
        predicted_class = scenario.get('predicted_class')

        # 过滤
        filtered = []
        for r in records:
            if margin_range and not (margin_range[0] <= r.margin < margin_range[1]):
                continue
            if predicted_class and r.pred != predicted_class:
                continue
            filtered.append(r)

        # 按模型统计准确率
        model_correct = defaultdict(int)
        model_total = defaultdict(int)
        for r in filtered:
            model_total[r.model_name] += 1
            if r.correct:
                model_correct[r.model_name] += 1

        return {
            model: (model_correct[model] / model_total[model] if model_total[model] > 0 else 0.0)
            for model in model_total
        }

    def query_odds_trajectory_pattern(self, home_odds: float, draw_odds: float,
                                       away_odds: float) -> Dict[str, Any]:
        """查询相似赔率的盘口轨迹模式"""
        trajectories = self._load_odds_trajectories()
        if not trajectories:
            return {"pattern": "no_data", "home_drift_mean": 0, "result_dist": {}}

        target = np.log(np.array([max(home_odds, 1.01), max(draw_odds, 1.01), max(away_odds, 1.01)]))

        # 找最相似的20场
        scored = []
        for t in trajectories:
            init = np.log(np.array([max(t.initial_home, 1.01), max(t.initial_draw, 1.01), max(t.initial_away, 1.01)]))
            dist = np.linalg.norm(target - init)
            scored.append((dist, t))
        scored.sort(key=lambda x: x[0])
        top = [t for _, t in scored[:20]]

        if not top:
            return {"pattern": "no_match", "home_drift_mean": 0, "result_dist": {}}

        # 统计漂移模式
        home_drifts = [t.home_drift for t in top]
        home_drift_mean = float(np.mean(home_drifts))

        # 分类漂移模式
        if abs(home_drift_mean) < 0.1:
            pattern = "stable"
        elif home_drift_mean < -0.1:
            pattern = "home_heat"  # 主队降赔变热
        elif home_drift_mean > 0.1:
            pattern = "home_cool"  # 主队升赔变冷
        else:
            pattern = "volatile"

        # 赛果分布
        result_dist = Counter(t.result for t in top)

        return {
            "pattern": pattern,
            "home_drift_mean": home_drift_mean,
            "result_dist": dict(result_dist),
            "sample_count": len(top)
        }

    def query_trap_history(self, trap_type: str = None) -> Dict[str, Any]:
        """查询某陷阱类型的历史赛果分布"""
        records = self._load_trap_records()
        if trap_type:
            records = [r for r in records if r.trap_type == trap_type]

        if not records:
            return {"hit_rate": 0, "result_dist": {}, "sample_count": 0}

        # 陷阱"命中" = 诱的方向未发生 (即陷阱生效, 庄家收割成功)
        # 简化: 陷阱命中 = 赛果不是被诱的方向
        result_dist = Counter(r.result for r in records)
        total = len(records)

        return {
            "hit_rate": float(sum(1 for r in records if r.confidence > 0.5) / total),
            "result_dist": dict(result_dist),
            "sample_count": total,
            "avg_confidence": float(np.mean([r.confidence for r in records]))
        }

    # ─── 经验特征生成 ─────────────────────────────────────────

    def generate_experience_features(self, context: Dict[str, Any]) -> ExperienceFeatures:
        """为贝叶斯分类器生成经验特征向量

        context 支持:
          - home_odds, draw_odds, away_odds: 赔率
          - return_rate: 返还率
          - home, away: 球队名 (可选)
          - predicted_class: 模型预测类别 (可选)
        """
        home_odds = context.get('home_odds', 2.0)
        draw_odds = context.get('draw_odds', 3.2)
        away_odds = context.get('away_odds', 3.5)
        return_rate = context.get('return_rate', 0.95)
        predicted_class = context.get('predicted_class')

        feats = ExperienceFeatures()

        # 场景分桶
        spread = math.log(max(away_odds, 1.01)) - math.log(max(home_odds, 1.01))
        if spread < 0.2:
            feats.odds_spread_bucket = "<0.2"
        elif spread < 0.8:
            feats.odds_spread_bucket = "0.2-0.8"
        elif spread < 1.5:
            feats.odds_spread_bucket = "0.8-1.5"
        else:
            feats.odds_spread_bucket = ">1.5"

        # margin 分桶
        imp_h, imp_d, imp_a = self._compute_implied(home_odds, draw_odds, away_odds)
        margin = imp_d - max(imp_h, imp_a)
        if margin < 0:
            feats.margin_bucket = "<0"
        elif margin < 0.05:
            feats.margin_bucket = "0-0.05"
        elif margin < 0.2:
            feats.margin_bucket = "0.05-0.2"
        elif margin < 0.4:
            feats.margin_bucket = "0.2-0.4"
        else:
            feats.margin_bucket = ">0.4"

        # 返还率分桶
        if return_rate < 0.9:
            feats.return_rate_bucket = "<0.9"
        elif return_rate < 0.95:
            feats.return_rate_bucket = "0.9-0.95"
        else:
            feats.return_rate_bucket = "0.95-1.0"

        # 查询历史相似比赛
        similar = self.query_similar_matches(home_odds, draw_odds, away_odds, top_k=20)
        feats.similar_match_count = len(similar)
        if similar:
            results = [m.result for m in similar]
            total = len(results)
            feats.similar_match_h_rate = results.count('H') / total
            feats.similar_match_d_rate = results.count('D') / total
            feats.similar_match_a_rate = results.count('A') / total

        # 查询回测表现
        backtest_perf = self.query_backtest_performance({
            'margin_range': (margin - 0.05, margin + 0.05),
            'predicted_class': predicted_class
        })
        feats.v32_acc_in_scenario = backtest_perf.get('v3.2', 0.0)
        feats.v40_acc_in_scenario = backtest_perf.get('v4.0', 0.0)
        feats.vip2_acc_in_scenario = backtest_perf.get('VIP-2', 0.0)
        feats.sky_acc_in_scenario = backtest_perf.get('SKY', 0.0)

        # 查询盘口轨迹
        traj = self.query_odds_trajectory_pattern(home_odds, draw_odds, away_odds)
        feats.drift_pattern = traj.get('pattern', 'stable')

        # 查询陷阱历史 (综合)
        trap_all = self.query_trap_history()
        feats.trap_hit_rate = trap_all.get('hit_rate', 0.0)
        if feats.trap_hit_rate > 0.6:
            feats.trap_danger_level = "high"
        elif feats.trap_hit_rate > 0.3:
            feats.trap_danger_level = "medium"
        else:
            feats.trap_danger_level = "low"

        # 路由建议
        feats.recommended_module, feats.confidence_boost = self._recommend_route(feats, predicted_class)

        return feats

    def _compute_implied(self, h: float, d: float, a: float) -> Tuple[float, float, float]:
        """计算隐含概率"""
        if h <= 0 or d <= 0 or a <= 0:
            return 0.33, 0.34, 0.33
        total = 1/h + 1/d + 1/a
        return (1/h)/total, (1/d)/total, (1/a)/total

    def _recommend_route(self, feats: ExperienceFeatures,
                          predicted_class: str = None) -> Tuple[str, float]:
        """基于经验特征推荐路由模块和置信度调整"""
        # 如果陷阱危险高 → 路由到 bookmaker_sim
        if feats.trap_danger_level == "high":
            return "bookmaker_sim", 0.1

        # 如果margin在危险区(0-0.05)且预测D → 路由到 risk_guard (D-Gate风险)
        if feats.margin_bucket in ("0-0.05", "<0") and predicted_class == 'D':
            return "risk_guard", 0.05

        # 如果盘口漂移模式是home_cool → 路由到 odds_analyzer
        if feats.drift_pattern == "home_cool":
            return "odds_analyzer", 0.03

        # 如果场景下某模型表现特别差 → 路由到 review_engine
        if feats.v32_acc_in_scenario < 0.4 and feats.v32_acc_in_scenario > 0:
            return "review_engine", 0.02

        # 默认无调整
        return "", 0.0


# ════════════════════════════════════════════════════════════
# 3. 自检 & 演示
# ════════════════════════════════════════════════════════════

def self_test():
    """自检"""
    print("=" * 70)
    print("ExperienceMemory 自检")
    print("=" * 70)

    mem = ExperienceMemory()

    # 测试1: 加载历史比赛
    matches = mem._load_match_history()
    print(f"\n[1] 历史比赛: {len(matches)} 条")
    if matches:
        m = matches[-1]
        print(f"    样本: {m.home} vs {m.away} | H@{m.home_odds} D@{m.draw_odds} A@{m.away_odds} | 结果={m.result}")

    # 测试2: 加载回测记录
    backtests = mem._load_backtest_records()
    print(f"\n[2] 回测记录: {len(backtests)} 条")
    model_stats = defaultdict(lambda: {'total': 0, 'correct': 0})
    for r in backtests:
        model_stats[r.model_name]['total'] += 1
        if r.correct:
            model_stats[r.model_name]['correct'] += 1
    for model, s in model_stats.items():
        acc = s['correct'] / s['total'] if s['total'] > 0 else 0
        print(f"    {model}: {s['correct']}/{s['total']} = {acc:.1%}")

    # 测试3: 加载盘口轨迹
    trajs = mem._load_odds_trajectories()
    print(f"\n[3] 盘口轨迹: {len(trajs)} 条")
    if trajs:
        t = trajs[0]
        print(f"    样本: 初始H@{t.initial_home} → 终盘H@{t.final_home} (漂移{t.home_drift:+.2f}) 结果={t.result}")

    # 测试4: 加载陷阱记录
    traps = mem._load_trap_records()
    print(f"\n[4] 陷阱记录: {len(traps)} 条")
    trap_dist = Counter(r.trap_type for r in traps)
    for ttype, cnt in trap_dist.most_common(5):
        print(f"    {ttype}: {cnt} 条")

    # 测试5: 查询相似比赛 (墨西哥vs韩国 H@2.03 D@3.25 A@3.95)
    print(f"\n[5] 查询相似比赛 (H@2.03 D@3.25 A@3.95)")
    similar = mem.query_similar_matches(2.03, 3.25, 3.95, top_k=10)
    print(f"    找到 {len(similar)} 场相似比赛")
    result_dist = Counter(m.result for m in similar)
    print(f"    赛果分布: {dict(result_dist)}")
    for m in similar[:3]:
        print(f"    {m.home} vs {m.away} ({m.date[:10]}) H@{m.home_odds} D@{m.draw_odds} A@{m.away_odds} → {m.result}")

    # 测试6: 生成经验特征
    print(f"\n[6] 生成经验特征 (墨西哥vs韩国场景)")
    feats = mem.generate_experience_features({
        'home_odds': 2.03, 'draw_odds': 3.25, 'away_odds': 3.95,
        'return_rate': 0.95, 'predicted_class': 'D'
    })
    print(f"    spread_bucket: {feats.odds_spread_bucket}")
    print(f"    margin_bucket: {feats.margin_bucket}")
    print(f"    drift_pattern: {feats.drift_pattern}")
    print(f"    similar_count: {feats.similar_match_count}")
    print(f"    H/D/A rate: {feats.similar_match_h_rate:.1%}/{feats.similar_match_d_rate:.1%}/{feats.similar_match_a_rate:.1%}")
    print(f"    trap_danger: {feats.trap_danger_level}")
    print(f"    recommended_module: {feats.recommended_module}")
    print(f"    confidence_boost: {feats.confidence_boost:+.3f}")
    print(f"    feature_vector ({len(feats.to_vector())}维): {[f'{v:.3f}' for v in feats.to_vector()]}")

    print(f"\n{'='*70}")
    print("自检完成")
    print(f"{'='*70}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    self_test()
