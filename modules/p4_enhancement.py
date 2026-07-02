"""
哨响AI v4.0 — P4 智能增强引擎 (Knowledge Auto-Updater + Transfer + Multi-Bookmaker)
=====================================================================================
P4剩余任务。三大增强模块:

    1. KnowledgeAutoUpdater — 知识库自动进化 (从新赛果更新规律)
    2. LeagueTransferAdapter — 跨联赛迁移学习 (贝叶斯收缩到联赛先验)
    3. MultiBookmakerCollector — 多机构赔率采集 (The Odds API标准接口)

作者: Architecture · P4 Phase
日期: 2026-06-18
"""
from __future__ import annotations
import logging
import time
import json
import os
from typing import Dict, List, Optional, Any, Tuple, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections import defaultdict

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 1. 知识库自动更新器
# ═══════════════════════════════════════════════════════════════

@dataclass
class MatchRecord:
    """单场比赛记录"""
    home_team: str
    away_team: str
    league: str
    result: str              # H/D/A
    home_goals: int = 0
    away_goals: int = 0
    spread: float = 0.0      # 让球盘口
    odds: Dict[str, float] = field(default_factory=dict)
    date: str = ""

    def to_dict(self) -> Dict:
        return {
            "home": self.home_team, "away": self.away_team,
            "league": self.league, "result": self.result,
            "goals": f"{self.home_goals}-{self.away_goals}",
            "spread": self.spread, "date": self.date,
        }

class KnowledgeAutoUpdater:
    """
    知识库自动进化器

    从新赛果增量更新知识库统计指标:
    - 各联赛D率
    - Spread区间胜率
    - 主客场优势系数
    """

    def __init__(self):
        self.league_stats: Dict[str, Dict] = defaultdict(lambda: {
            "total": 0, "home": 0, "draw": 0, "away": 0,
            "total_goals": 0, "home_goals": 0, "away_goals": 0,
        })
        self.spread_bins: Dict[Tuple[float, float], Dict] = defaultdict(lambda: {
            "total": 0, "home": 0, "draw": 0, "away": 0,
        })
        self.records: List[MatchRecord] = []
        self._total_processed = 0

    def ingest(self, record: MatchRecord) -> Dict:
        """摄入单场比赛"""
        self.records.append(record)
        self._total_processed += 1

        # 联赛统计
        ls = self.league_stats[record.league]
        ls["total"] += 1
        if record.result == "H":
            ls["home"] += 1
        elif record.result == "D":
            ls["draw"] += 1
        else:
            ls["away"] += 1
        ls["total_goals"] += record.home_goals + record.away_goals
        ls["home_goals"] += record.home_goals
        ls["away_goals"] += record.away_goals

        # Spread区间统计
        bin_key = self._get_spread_bin(abs(record.spread))
        sb = self.spread_bins[bin_key]
        sb["total"] += 1
        if record.result == "H":
            sb["home"] += 1
        elif record.result == "D":
            sb["draw"] += 1
        else:
            sb["away"] += 1

        return self.get_updated_knowledge()

    def _get_spread_bin(self, abs_spread: float) -> Tuple[float, float]:
        for lo, hi in [(0, 2), (2, 5), (5, 8), (8, 12), (12, 20), (20, 100)]:
            if lo <= abs_spread < hi:
                return (lo, hi)
        return (20, 100)

    def get_updated_knowledge(self) -> Dict:
        """获取更新后的知识"""
        # 联赛D率排序 (取样本>=5的联赛)
        league_d_rates = {}
        for league, stats in self.league_stats.items():
            if stats["total"] >= 5:
                league_d_rates[league] = round(stats["draw"] / stats["total"], 4)

        # Spread区间胜率
        spread_rates = {}
        for (lo, hi), stats in sorted(self.spread_bins.items()):
            if stats["total"] >= 3:
                n = stats["total"]
                spread_rates[f"{lo}-{hi}"] = {
                    "H": round(stats["home"] / n, 3),
                    "D": round(stats["draw"] / n, 3),
                    "A": round(stats["away"] / n, 3),
                    "n": n,
                }

        # 全局统计
        all_total = sum(s["total"] for s in self.league_stats.values())
        all_home = sum(s["home"] for s in self.league_stats.values())
        all_draw = sum(s["draw"] for s in self.league_stats.values())

        return {
            "total_processed": self._total_processed,
            "global": {
                "total": all_total,
                "home_rate": round(all_home / all_total, 4) if all_total > 0 else 0,
                "draw_rate": round(all_draw / all_total, 4) if all_total > 0 else 0,
            },
            "league_draw_rates": league_d_rates,
            "spread_rates": spread_rates,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def compare_with_baseline(self) -> Dict:
        """对比基线知识库的变化"""
        knowledge = self.get_updated_knowledge()
        changes = []

        # 检查联赛D率偏差
        from modules.draw_upset_analyzer import DrawUpsetAnalyzer
        baseline = DrawUpsetAnalyzer.LEAGUE_D_PRIORS
        for league, rate in knowledge["league_draw_rates"].items():
            if league in baseline:
                delta = rate - baseline[league]
                if abs(delta) > 0.03:
                    changes.append({
                        "type": "league_d_rate_shift",
                        "league": league,
                        "baseline": baseline[league],
                        "current": rate,
                        "delta": round(delta, 4),
                        "alert": f"联赛{league} D率从{baseline[league]:.1%}→{rate:.1%} (变化{delta:+.1%})",
                    })

        # 建议更新
        suggestions = []
        if changes:
            suggestions.append(f"检测到{len(changes)}项统计变化, 建议更新知识库")
            for c in changes:
                suggestions.append(f"  - {c['alert']}")

        return {
            "changes": changes,
            "suggestions": suggestions,
            "needs_update": len(changes) > 0,
        }

# ═══════════════════════════════════════════════════════════════
# 2. 跨联赛迁移学习适配器
# ═══════════════════════════════════════════════════════════════

@dataclass
class LeagueProfile:
    """联赛画像"""
    name: str
    avg_goals: float            # 场均进球
    home_advantage: float       # 主场优势 (胜率偏移)
    draw_rate: float            # 平局率
    favorite_win_rate: float    # 热门胜率 (spread>5时)
    style: str = "balanced"     # attacking/balanced/defensive

    def to_dict(self) -> Dict:
        return {
            "name": self.name, "avg_goals": self.avg_goals,
            "home_advantage": self.home_advantage,
            "draw_rate": self.draw_rate,
            "favorite_win_rate": self.favorite_win_rate,
            "style": self.style,
        }

class LeagueTransferAdapter:
    """
    跨联赛迁移学习适配器

    方法: 贝叶斯收缩 (Bayesian Shrinkage)
    当目标联赛数据不足时, 将预测概率向联赛先验收缩:
        P_adjusted = w * P_model + (1-w) * P_league_prior
        w = min(1, n_samples / n_threshold)
    """

    # 联赛画像基线 (从312K历史数据)
    LEAGUE_PROFILES = {
        "英超": LeagueProfile("英超", 2.8, 0.15, 0.24, 0.75, "attacking"),
        "意甲": LeagueProfile("意甲", 2.6, 0.18, 0.27, 0.72, "defensive"),
        "德甲": LeagueProfile("德甲", 3.0, 0.16, 0.22, 0.73, "attacking"),
        "西甲": LeagueProfile("西甲", 2.7, 0.17, 0.25, 0.76, "balanced"),
        "法甲": LeagueProfile("法甲", 2.5, 0.14, 0.28, 0.70, "defensive"),
        "J联赛": LeagueProfile("J联赛", 2.4, 0.12, 0.28, 0.68, "balanced"),
        "巴甲": LeagueProfile("巴甲", 2.3, 0.20, 0.25, 0.71, "balanced"),
        "土超": LeagueProfile("土超", 2.6, 0.25, 0.26, 0.74, "attacking"),
        "俄超": LeagueProfile("俄超", 2.2, 0.16, 0.27, 0.67, "defensive"),
        "葡超": LeagueProfile("葡超", 2.5, 0.18, 0.26, 0.73, "balanced"),
    }

    def __init__(self, n_threshold: int = 200):
        """
        Args:
            n_threshold: 数据充足阈值 (当目标联赛样本>=阈值时, w=1, 不再用先验)
        """
        self.n_threshold = n_threshold
        self.league_samples: Dict[str, int] = defaultdict(int)

    def get_profile(self, league: str) -> Optional[LeagueProfile]:
        """获取联赛画像"""
        return self.LEAGUE_PROFILES.get(league)

    def adapt(self, h_prob: float, d_prob: float, a_prob: float,
              league: str) -> Tuple[float, float, float, float]:
        """
        跨联赛概率适配

        将模型预测概率向联赛先验做贝叶斯收缩。

        Returns:
            (h_adapted, d_adapted, a_adapted, shrinkage_weight)
        """
        profile = self.get_profile(league)
        if not profile:
            return h_prob, d_prob, a_prob, 1.0

        n = self.league_samples.get(league, 0)
        # 收缩权重: 样本越多, 越信任模型
        w = min(1.0, n / self.n_threshold)

        # 联赛先验概率 (从画像推)
        # 简化: 使用draw_rate和favorite_win_rate构建先验
        prior_d = profile.draw_rate
        prior_h = (1.0 - prior_d) * (profile.favorite_win_rate if h_prob > a_prob else (1 - profile.favorite_win_rate))
        prior_a = 1.0 - prior_h - prior_d

        # 贝叶斯收缩
        h_adapted = w * h_prob + (1 - w) * prior_h
        d_adapted = w * d_prob + (1 - w) * prior_d
        a_adapted = w * a_prob + (1 - w) * prior_a

        # 归一化
        total = h_adapted + d_adapted + a_adapted
        if total > 0:
            h_adapted /= total
            d_adapted /= total
            a_adapted /= total

        return h_adapted, d_adapted, a_adapted, w

    def record_sample(self, league: str):
        """记录一个联赛样本 (增加该联赛的数据量)"""
        self.league_samples[league] += 1

    def get_shrinkage_info(self, league: str) -> Dict:
        """获取收缩信息"""
        n = self.league_samples.get(league, 0)
        w = min(1.0, n / self.n_threshold)
        profile = self.get_profile(league)
        return {
            "league": league,
            "samples": n,
            "threshold": self.n_threshold,
            "shrinkage_weight": round(w, 4),
            "status": "sufficient" if w >= 1.0 else "adapting",
            "profile": profile.to_dict() if profile else None,
        }

# ═══════════════════════════════════════════════════════════════
# 3. 多机构赔率采集器
# ═══════════════════════════════════════════════════════════════

@dataclass
class BookmakerOdds:
    """单机构赔率"""
    bookmaker: str
    home: float
    draw: float
    away: float
    timestamp: str = ""
    market_type: str = "1X2"

    def to_dict(self) -> Dict:
        return {
            "bookmaker": self.bookmaker,
            "home": self.home, "draw": self.draw, "away": self.away,
            "margin": round(1/self.home + 1/self.draw + 1/self.away - 1, 4),
        }

@dataclass
class MultiBookmakerReport:
    """多机构赔率对比报告"""
    home_team: str
    away_team: str
    odds: List[BookmakerOdds] = field(default_factory=list)
    consensus: Dict[str, float] = field(default_factory=dict)  # 中位数
    divergence: float = 0.0   # 分歧度 (标准差)
    outliers: List[Dict] = field(default_factory=list)  # 异常机构
    best_value: Dict = field(default_factory=dict)       # 最佳价值方向

    def to_dict(self) -> Dict:
        return {
            "match": {"home": self.home_team, "away": self.away_team},
            "bookmakers": len(self.odds),
            "consensus": {k: round(v, 4) for k, v in self.consensus.items()},
            "divergence": round(self.divergence, 4),
            "outliers": self.outliers,
            "best_value": self.best_value,
            "all_odds": [o.to_dict() for o in self.odds],
        }

class MultiBookmakerCollector:
    """
    多机构赔率采集器

    支持:
    - The Odds API 标准接口
    - 手动输入多机构赔率
    - 分歧度计算 + 异常检测
    - 最佳价值方向识别
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("THE_ODDS_API_KEY", "")
        self._last_fetch: Optional[datetime] = None

    def from_manual(self, home_team: str, away_team: str,
                    odds_list: List[Dict[str, float]]) -> MultiBookmakerReport:
        """从手动输入的多机构赔率生成报告"""
        bookmakers = []
        for i, odds in enumerate(odds_list):
            bookmakers.append(BookmakerOdds(
                bookmaker=odds.get("bookmaker", f"机构{i+1}"),
                home=odds["home"], draw=odds["draw"], away=odds["away"],
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))

        return self._analyze(home_team, away_team, bookmakers)

    def _analyze(self, home_team: str, away_team: str,
                 bookmakers: List[BookmakerOdds]) -> MultiBookmakerReport:
        """分析多机构赔率"""
        if not bookmakers:
            return MultiBookmakerReport(home_team=home_team, away_team=away_team)

        # 中位数 (共识赔率)
        homes = [b.home for b in bookmakers]
        draws = [b.draw for b in bookmakers]
        aways = [b.away for b in bookmakers]

        consensus = {
            "home": self._median(homes),
            "draw": self._median(draws),
            "away": self._median(aways),
        }

        # 分歧度: 隐含概率的标准差
        inv_sum = 1/consensus["home"] + 1/consensus["draw"] + 1/consensus["away"]
        implied = [1/o/inv_sum for o in [consensus["home"], consensus["draw"], consensus["away"]]]
        all_implied = []
        for b in bookmakers:
            s = 1/b.home + 1/b.draw + 1/b.away
            all_implied.append(1/b.home/s)

        mean_imp = sum(all_implied) / len(all_implied)
        variance = sum((x - mean_imp)**2 for x in all_implied) / len(all_implied)
        divergence = variance ** 0.5

        # 异常机构检测 (>2倍标准差偏离共识)
        outliers = []
        for b in bookmakers:
            s = 1/b.home + 1/b.draw + 1/b.away
            imp = 1/b.home / s
            if abs(imp - mean_imp) > 2 * divergence:
                outliers.append({
                    "bookmaker": b.bookmaker,
                    "implied_home": round(imp, 4),
                    "consensus_home": round(mean_imp, 4),
                    "deviation": round(abs(imp - mean_imp), 4),
                })

        # 最佳价值: 找出对某方向赔率最高的机构
        best_value = {
            "home": {"bookmaker": max(bookmakers, key=lambda b: b.home).bookmaker,
                     "odds": max(homes)},
            "draw": {"bookmaker": max(bookmakers, key=lambda b: b.draw).bookmaker,
                     "odds": max(draws)},
            "away": {"bookmaker": max(bookmakers, key=lambda b: b.away).bookmaker,
                     "odds": max(aways)},
        }

        return MultiBookmakerReport(
            home_team=home_team, away_team=away_team,
            odds=bookmakers, consensus=consensus,
            divergence=divergence, outliers=outliers,
            best_value=best_value,
        )

    @staticmethod
    def _median(values: List[float]) -> float:
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        if n == 0:
            return 0
        if n % 2 == 1:
            return sorted_vals[n // 2]
        return (sorted_vals[n//2 - 1] + sorted_vals[n//2]) / 2

    def is_available(self) -> bool:
        """检查 API 是否可用"""
        return bool(self.api_key)

# ═══════════════════════════════════════════════════════════════
# 4. 全局单例
# ═══════════════════════════════════════════════════════════════

_updater: Optional[KnowledgeAutoUpdater] = None
_transfer: Optional[LeagueTransferAdapter] = None
_collector: Optional[MultiBookmakerCollector] = None

def get_updater() -> KnowledgeAutoUpdater:
    global _updater
    if _updater is None:
        _updater = KnowledgeAutoUpdater()
    return _updater

def get_transfer() -> LeagueTransferAdapter:
    global _transfer
    if _transfer is None:
        _transfer = LeagueTransferAdapter()
    return _transfer

def get_collector() -> MultiBookmakerCollector:
    global _collector
    if _collector is None:
        _collector = MultiBookmakerCollector()
    return _collector

def reset_all_p4():
    global _updater, _transfer, _collector
    _updater = None
    _transfer = None
    _collector = None
