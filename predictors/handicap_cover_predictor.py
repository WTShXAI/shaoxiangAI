"""
让球覆盖预测器 (v3.0 — 欧盘版)
================================================================
统一接口: 结合 OTSM 调制 + 历史覆盖率剖面 + 1X2 欧赔推导让球线,
输出赢盘概率、置信度、价值投注信号。

核心流程:
  1. 输入 match_id + 1X2 欧赔
  2. odds_to_handicap → 理论让球线 + 分桶
  3. handicap_depth_profile 查询 → 该桶×OTSM状态的历史覆盖率
  4. OTSM 调制 → 赢盘置信度加权
  5. 对比市场赔率 → 价值信号
"""

import sqlite3
import logging
from typing import Optional, Dict, Tuple
from dataclasses import dataclass

from bookmaker_sim.odds_handicap_converter import (
    odds_to_handicap,
    odds_to_handicap_bin,
)

logger = logging.getLogger(__name__)

@dataclass
class CoverPrediction:
    """让球覆盖预测结果"""
    match_id: int
    # 欧赔 → 理论让球线
    derived_handicap: float
    handicap_bin: str
    # 历史覆盖率
    base_cover_rate: float       # 该桶该状态下的历史主队赢盘率
    sample_count: int            # 样本量
    # OTSM 状态
    otsm_state: str
    lock_confidence: float
    # OTSM 调制后输出
    cover_probability: float     # 调制后的主队赢盘概率
    cover_confidence: float      # 预测置信度 [0, 1]
    # 价值信号
    value_exists: bool           # 是否存在价值
    value_magnitude: float       # 价值幅度
    signal_strength: str         # strong / moderate / weak

class HandicapCoverPredictor:
    """
    让球覆盖预测器 (欧盘版)

    用法:
        predictor = HandicapCoverPredictor()
        predictor.initialize()
        predictor.load_profiles()

        # 预测某场比赛的让球覆盖
        result = predictor.predict(match_id, home_odds=1.50, draw_odds=4.00, away_odds=6.50)
        # 或从数据库自动读取赔率:
        result = predictor.predict(match_id)
    """

    # OTSM 调制参数
    OTSM_WEIGHTS = {
        "LOCKED": {"base": 0.50, "bonus": 0.50},
        "ACTIVE":  {"base": 0.30, "bonus": 0.30},
        "NOISE":   {"base": 0.10, "bonus": 0.20},
        "UNKNOWN": {"base": 0.20, "bonus": 0.10},
    }

    # 价值投注阈值
    VALUE_THRESHOLDS = {
        "LOCKED": 0.03,
        "ACTIVE": 0.06,
        "NOISE": 0.10,
        "UNKNOWN": 0.08,
    }

    def __init__(self, db_path: str = "data/football_data.db"):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.profiles: Dict[Tuple[str, str], Dict] = {}
        self._profiles_loaded = False

    def initialize(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        logger.info(f"HandicapCoverPredictor 初始化完成, DB: {self.db_path}")

    def load_profiles(self):
        """从 handicap_depth_profile 加载覆盖率剖面到内存"""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT handicap_bin, otsm_state, n_samples,
                   home_cover_rate, away_cover_rate, push_rate, home_cover_std
            FROM handicap_depth_profile
        """)
        rows = cur.fetchall()

        self.profiles = {}
        for r in rows:
            key = (r["handicap_bin"], r["otsm_state"])
            self.profiles[key] = {
                "n": r["n_samples"],
                "home_cover": r["home_cover_rate"],
                "away_cover": r["away_cover_rate"],
                "push": r["push_rate"],
                "std": r["home_cover_std"],
            }
        self._profiles_loaded = True
        logger.info(f"加载 {len(self.profiles)} 条覆盖率剖面")

    def _get_otsm_state(self, match_id: int) -> Tuple[str, float]:
        """从 match_features_otsm 表查询 OTSM 状态和锁置信度"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT otsm_state_LOCKED, otsm_state_ACTIVE, otsm_state_NOISE, "
                "otsm_lock_confidence FROM match_features_otsm WHERE match_id=?",
                (match_id,),
            )
            row = cur.fetchone()
            if row:
                if row["otsm_state_LOCKED"]:
                    state = "LOCKED"
                elif row["otsm_state_ACTIVE"]:
                    state = "ACTIVE"
                elif row["otsm_state_NOISE"]:
                    state = "NOISE"
                else:
                    state = "UNKNOWN"
                conf = row["otsm_lock_confidence"] or 0.0
                return state, conf
        except sqlite3.OperationalError:
            pass
        return "UNKNOWN", 0.0

    def _get_odds(self, match_id: int) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """从 odds 表查询 1X2 赔率"""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT home_odds, draw_odds, away_odds FROM odds WHERE match_id=? "
            "AND home_odds IS NOT NULL ORDER BY odds_timestamp ASC LIMIT 1",
            (match_id,),
        )
        row = cur.fetchone()
        if row:
            return row["home_odds"], row["draw_odds"], row["away_odds"]
        return None, None, None

    def predict(
        self,
        match_id: int,
        home_odds: Optional[float] = None,
        draw_odds: Optional[float] = None,
        away_odds: Optional[float] = None,
    ) -> Optional[CoverPrediction]:
        """
        预测让球覆盖结果。

        Args:
            match_id: 比赛ID
            home_odds, draw_odds, away_odds: 1X2 欧赔 (可选, 不传则从DB读取)

        Returns:
            CoverPrediction 或 None (数据不足时)
        """
        if not self._profiles_loaded:
            self.load_profiles()
            if not self._profiles_loaded:
                logger.warning("覆盖率剖面未加载, 无法预测")
                return None

        # 获取赔率
        if home_odds is None or draw_odds is None or away_odds is None:
            home_odds, draw_odds, away_odds = self._get_odds(match_id)
            if home_odds is None:
                logger.warning(f"比赛 {match_id}: 无可用赔率")
                return None

        # ★ Step 1: 欧赔 → 理论让球线
        handicap, p_home, p_away = odds_to_handicap(home_odds, draw_odds, away_odds)
        handicap_bin = odds_to_handicap_bin(handicap)

        # ★ Step 2: 查询历史覆盖率剖面
        otsm_state, lock_confidence = self._get_otsm_state(match_id)

        key = (handicap_bin, otsm_state)
        profile = self.profiles.get(key)
        if profile is None:
            # 降级: 查 UNKNOWN 状态
            fallback_key = (handicap_bin, "UNKNOWN")
            profile = self.profiles.get(fallback_key)
            if profile is None:
                logger.debug(f"无剖面: {handicap_bin} × {otsm_state}")
                return None

        base_cover_rate = profile["home_cover"]
        n_samples = profile["n"]

        # ★ Step 3: OTSM 调制
        weights = self.OTSM_WEIGHTS.get(otsm_state, self.OTSM_WEIGHTS["UNKNOWN"])
        weight = min(1.0, weights["base"] + lock_confidence * weights["bonus"])

        # 赢盘概率 = 0.5 + (历史覆盖率 - 0.5) × OTSM权重
        # LOCKED=权重高 → 历史规律被放大
        # NOISE=权重低 → 回归中立 0.5
        modulated_prob = 0.5 + (base_cover_rate - 0.5) * weight

        # 置信度 = OTSM锁置信度 × OTSM权重 × (1 - 标准差惩罚)
        sample_penalty = min(1.0, n_samples / 50)  # 样本少于50时降权
        modulated_conf = lock_confidence * weight * sample_penalty

        # ★ Step 4: 价值投注信号
        # 对比: 模型理论赢盘率 vs 此分桶历史"基准"覆盖率
        # 如果 OTSM 锁定时偏差显著 → 有价值
        threshold = self.VALUE_THRESHOLDS.get(otsm_state, 0.08)
        deviation = abs(modulated_prob - base_cover_rate)
        value_exists = deviation > threshold and otsm_state == "LOCKED"

        # 信号强度
        if deviation > threshold * 2 and value_exists:
            signal_strength = "strong"
        elif deviation > threshold:
            signal_strength = "moderate"
        else:
            signal_strength = "weak"

        return CoverPrediction(
            match_id=match_id,
            derived_handicap=handicap,
            handicap_bin=handicap_bin,
            base_cover_rate=base_cover_rate,
            sample_count=n_samples,
            otsm_state=otsm_state,
            lock_confidence=lock_confidence,
            cover_probability=round(modulated_prob, 4),
            cover_confidence=round(modulated_conf, 4),
            value_exists=value_exists,
            value_magnitude=round(deviation, 4),
            signal_strength=signal_strength,
        )

    def batch_predict(self, match_ids: list) -> Dict[int, Optional[CoverPrediction]]:
        """批量预测"""
        results = {}
        for mid in match_ids:
            results[mid] = self.predict(mid)
        return results

    def close(self):
        if self.conn:
            self.conn.close()

# ── CLI ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    predictor = HandicapCoverPredictor()
    predictor.initialize()
    predictor.load_profiles()

    # 找几场比赛测试
    cur = predictor.conn.cursor()
    cur.execute("""
        SELECT m.match_id, m.home_team_name, m.away_team_name,
               o.home_odds, o.draw_odds, o.away_odds
        FROM matches m
        JOIN odds o ON m.match_id = o.match_id
        WHERE m.status = 'finished' AND o.home_odds IS NOT NULL
        ORDER BY m.match_date DESC LIMIT 5
    """)
    rows = cur.fetchall()

    print("\n让球覆盖预测 (欧盘版) — 测试:")
    print(f"{'match_id':>8} {'对阵':>24} {'赔率(H/D/A)':>22} {'让球线':>7} {'OTSM':>8} {'赢盘率':>8} {'置信度':>7} {'价值'}")
    print("-" * 110)

    for r in rows:
        mid = r["match_id"]
        result = predictor.predict(mid, r["home_odds"], r["draw_odds"], r["away_odds"])
        if result:
            odds_str = f"{r['home_odds']:.2f}/{r['draw_odds']:.2f}/{r['away_odds']:.2f}"
            vs_str = f"{r['home_team_name'][:10]} vs {r['away_team_name'][:10]}"
            print(f"{mid:>8} {vs_str:>24} {odds_str:>22} {result.handicap_bin:>7} "
                  f"{result.otsm_state:>8} {result.cover_probability:>7.1%} "
                  f"{result.cover_confidence:>7.3f} {'✓' if result.value_exists else '-'}")

    predictor.close()
