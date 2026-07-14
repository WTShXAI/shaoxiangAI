#!/usr/bin/env python3
"""
哨响AI — 赔率时序状态机 (Odds Temporal State Machine) v1.0

核心理念:
  庄家的赔率调整不是一个单纯的定价过程，而是一套经过多层非线性变换的加密协议。
  开盘赔率(open)是庄家对赛果的初始哈希、收盘赔率(close)是经过风控算法扰动后的密文。
  开→收之间的相变轨迹，暴露了庄家风控模型的内部状态转移。

三维相空间:
  D1 — 熵漂移 (Entropy Drift):      概率分布从开到收的熵变化，量度市场信念收敛/发散
  D2 — 水位加速度 (Water-Level Acc): 庄家抽水率的变化率，量度庄家自身的确信度
  D3 — 凯利涨落 (Kelly Fluctuation): 热门方的隐含概率变化，量度市场对赛果的重估强度

四种状态:
  LOCKED  (锁定期): 庄家风控已收敛到确定性锚点 → 赔率置信度最高
  ACTIVE  (活跃期): 庄家正在调整仓位但未锁定 → 信号存在但含噪声
  NOISE   (噪声期): 赔率几乎不变，纯市场噪声 → 无可用信号
  DECOY   (诱饵期): 表面有强信号但方向与基本面相悖 → 可能是庄家做局

集成:
  - 作为独立分析模块运行: python odds_temporal_sm.py --analyze
  - 被 pipeline.py 调用来增强选择性预测
  - 被 prediction_guard.py 调用来验证预测一致性

依赖: training_extended 表 (311,983条, 100%开赔覆盖)
"""

import sqlite3
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

# ── 路径 ──
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "football_data.db")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output", "temporal_sm")

# ═══════════════════════════════════════════════════════════════════════════════
# 基础数据结构
# ═══════════════════════════════════════════════════════════════════════════════

class PhaseState(str, Enum):
    """赔率相空间状态"""
    LOCKED = "LOCKED"    # 庄家确信锁定
    ACTIVE = "ACTIVE"    # 活跃调仓中
    NOISE = "NOISE"      # 纯噪声
    DECOY = "DECOY"      # 疑似诱饵

@dataclass
class PhaseVector:
    """三维相空间坐标"""
    entropy_drift: float       # D1: 熵漂移 [-1, +1], 负=收敛, 正=发散
    water_accel: float         # D2: 水位加速度 [-1, +1], 负=庄家降抽水(自信)
    kelly_fluctuation: float   # D3: 凯利涨落 [0, +∞), 大=大幅重估
    magnitude: float = 0.0     # 合成向量模

    def __post_init__(self):
        self.magnitude = np.sqrt(
            self.entropy_drift ** 2 +
            self.water_accel ** 2 +
            min(self.kelly_fluctuation, 1.0) ** 2  # cap at 1.0 for magnitude
        )

@dataclass
class OddsSnapshot:
    """赔率快照（开盘或收盘）"""
    home: float
    draw: float
    away: float

    @property
    def implied_probs(self) -> np.ndarray:
        """赔率→隐含概率，含 overround 归一化"""
        raw = np.array([1.0 / self.home, 1.0 / self.draw, 1.0 / self.away])
        return raw / raw.sum()

    @property
    def overround(self) -> float:
        """庄家抽水率"""
        raw = 1.0 / self.home + 1.0 / self.draw + 1.0 / self.away
        return raw - 1.0

    @property
    def entropy(self) -> float:
        """概率分布香农熵 (bits)，最大值 log2(3)≈1.585"""
        p = self.implied_probs
        return -np.sum(p * np.log2(p + 1e-12))

    @property
    def favorite(self) -> int:
        """热门方向: 0=H, 1=D, 2=A"""
        return int(np.argmin([self.home, self.draw, self.away]))

@dataclass
class StateMachineResult:
    """状态机推断结果"""
    match_id: Any
    match_date: str
    league: str
    home_team: str
    away_team: str

    # 原始赔率
    open_snapshot: OddsSnapshot
    close_snapshot: OddsSnapshot

    # 相空间坐标
    phase_vector: PhaseVector

    # 推断
    state: PhaseState
    lock_confidence: float       # [0,1] 锁定期确信度
    state_probabilities: Dict[str, float] = field(default_factory=dict)

    # 结果 (用于回测，可选)
    actual_result: Optional[str] = None
    prediction_correct: Optional[bool] = None

# ═══════════════════════════════════════════════════════════════════════════════
# 核心状态机
# ═══════════════════════════════════════════════════════════════════════════════

class OddsTemporalStateMachine:
    """
    赔率时序状态机

    通过分析开盘→收盘的赔率相变轨迹，推断庄家风控模型的内部状态。
    不依赖ML拟合，基于赔率结构本身的几何特征做推断。

    使用:
        sm = OddsTemporalStateMachine()
        sm.fit_thresholds()              # 从历史数据计算分位数阈值
        results = sm.analyze_matches()   # 批量分析
        state = sm.infer_single(open_odds, close_odds)  # 单场推断
    """

    def __init__(self, db_path: str = DB_PATH, output_dir: str = OUTPUT_DIR):
        self.db_path = db_path
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        # 分位数阈值（从历史数据拟合）
        self.thresholds: Dict[str, Dict[str, float]] = {}

        # 状态先验分布
        self.state_priors: Dict[str, float] = {}

        # 状态→准确率映射 (回测填充)
        self.state_accuracy: Dict[str, float] = {}

    # ── 特征计算 ────────────────────────────────────────────────────────

    @staticmethod
    def compute_entropy_drift(open_snap: OddsSnapshot, close_snap: OddsSnapshot) -> float:
        """
        D1: 熵漂移

        开盘熵 → 收盘熵的变化。负值=市场信念收敛 (不确定性下降), 正值=发散。

        归一化: 除以最大可能熵 log2(3)≈1.585，输出范围 [-1, 1]。
        """
        max_entropy = np.log2(3)
        delta = close_snap.entropy - open_snap.entropy
        return delta / max_entropy

    @staticmethod
    def compute_water_accel(open_snap: OddsSnapshot, close_snap: OddsSnapshot) -> float:
        """
        D2: 水位加速度

        庄家抽水率(overround)的变化。负值=庄家降低抽水(对自己判断更自信),
        正值=庄家扩大抽水(对冲不确定性)。

        归一化: delta_overround / open_overround，限制到 [-1, 1]。
        """
        open_or = open_snap.overround
        close_or = close_snap.overround

        if abs(open_or) < 1e-8:
            return 0.0

        delta = close_or - open_or
        accel = delta / (abs(open_or) + 0.01)
        return float(np.clip(accel, -1.0, 1.0))

    @staticmethod
    def compute_kelly_fluctuation(open_snap: OddsSnapshot, close_snap: OddsSnapshot) -> float:
        """
        D3: 凯利涨落

        热门方的隐含概率从开到收的变化幅度。反映市场对赛果的重估强度。
        大值=市场大幅重新评估热门方胜率。

        归一化: |delta_fav| / open_fav, 无上限但有典型范围 [0, 2]。
        """
        open_probs = open_snap.implied_probs
        close_probs = close_snap.implied_probs

        # 取双方最大概率方向
        open_fav_idx = open_snap.favorite
        close_fav_idx = close_snap.favorite

        # 如果热门方向变了，波动更大 (方向切换惩罚)
        if open_fav_idx != close_fav_idx:
            base = abs(close_probs[close_fav_idx] - open_probs[open_fav_idx])
            base += 0.05  # 方向切换惩罚
        else:
            base = abs(close_probs[open_fav_idx] - open_probs[open_fav_idx])

        # 除以开盘概率做归一化
        norm = open_probs[open_fav_idx] + 0.05
        return float(base / norm)

    def compute_phase_vector(self, open_snap: OddsSnapshot, close_snap: OddsSnapshot) -> PhaseVector:
        """计算完整三维相空间坐标"""
        return PhaseVector(
            entropy_drift=self.compute_entropy_drift(open_snap, close_snap),
            water_accel=self.compute_water_accel(open_snap, close_snap),
            kelly_fluctuation=self.compute_kelly_fluctuation(open_snap, close_snap),
        )

    # ── 状态推断 ────────────────────────────────────────────────────────

    def infer_state(self, pv: PhaseVector) -> Tuple[PhaseState, float, Dict[str, float]]:
        """
        从相空间坐标推断庄家状态 (v2 — 数据驱动, 2026-06-13 校准)。

        核心发现 (50000样本回测):
          - 熵漂移是唯一强信号维度 (P80+ → 63.2% vs P0-P50 → 46.4%)
          - 水位加速和凯利涨落仅提供微弱辅助信号
          - 最佳复合: 高熵漂移 + 庄家降抽水 = 64.0%

        三态模型:
          NOISE  (噪声期): 熵漂移 < P20 → 48.7%, 低于基准, 显式低置信
          ACTIVE (活跃期): P20 ≤ 熵漂移 ≤ P80 → 50%+, 中等置信
          LOCKED (锁定期): 熵漂移 > P80 → 63.2%, 庄家已解码锚点
            - LOCKED_PREMIUM: > P80 + 降抽水 → 64.0%, 最强信号
            - LOCKED_STANDARD: > P80 (无水位确认) → 62.2%

        注意: DECOY 状态已弃用。数据显示"高熵+升抽水"也是 62.2%，
              实为市场在激烈博弈后的方向性确认，不是诱饵。

        Returns:
            (state, lock_confidence, state_probabilities)
        """
        th = self.thresholds

        # 各维度分位数
        ent_p20 = th.get("entropy_drift", {}).get("p20", 0.002)
        ent_p50 = th.get("entropy_drift", {}).get("p50", 0.010)
        ent_p80 = th.get("entropy_drift", {}).get("p80", 0.030)
        ent_p90 = th.get("entropy_drift", {}).get("p90", 0.050)
        water_p20 = th.get("water_accel", {}).get("p20", -0.05)
        water_p80 = th.get("water_accel", {}).get("p80", 0.05)

        e = abs(pv.entropy_drift)
        w = pv.water_accel       # 负=降抽水(庄家自信), 正=升抽水(对冲)

        # ── lock_confidence: 直接映射到熵漂移分位 ──
        # 线性插值到 [0, 1], P20=0, P80=0.6, P90=1.0, P95+→饱和
        if e <= ent_p20:
            lock_conf = 0.0
        elif e <= ent_p80:
            lock_conf = float((e - ent_p20) / (ent_p80 - ent_p20 + 1e-9) * 0.6)
        elif e <= ent_p90:
            lock_conf = float(0.6 + (e - ent_p80) / (ent_p90 - ent_p80 + 1e-9) * 0.3)
        else:
            lock_conf = float(min(1.0, 0.9 + (e - ent_p90) / (ent_p90 + 1e-9) * 0.1))

        # ── 水位确认加成: 降抽水 +0.05, 升抽水 -0.02 ──
        if w < water_p20:
            lock_conf = float(min(1.0, lock_conf + 0.05))
        elif w > water_p80:
            lock_conf = float(max(0.0, lock_conf - 0.02))

        # ── 状态分类 ──
        if e < ent_p20:
            # 噪声：熵几乎不变，无解码信号
            state = PhaseState.NOISE
            prob = {"NOISE": 0.65, "ACTIVE": 0.25, "LOCKED": 0.05, "DECOY": 0.05}
        elif e > ent_p80:
            # 锁定：熵大幅变化，庄家已解码
            state = PhaseState.LOCKED
            prob = {"NOISE": 0.05, "ACTIVE": 0.15, "LOCKED": 0.75, "DECOY": 0.05}
        else:
            # 活跃：中等等级熵变化
            state = PhaseState.ACTIVE
            prob = {"NOISE": 0.20, "ACTIVE": 0.55, "LOCKED": 0.20, "DECOY": 0.05}

        return state, lock_conf, prob

    # ── 阈值拟合 ────────────────────────────────────────────────────────

    def fit_thresholds(self, sample_size: int = 50000) -> Dict[str, Dict[str, float]]:
        """
        从 training_extended 历史数据拟合分位数阈值。

        遍历样本，计算所有比赛的三维相空间坐标，取各维度分位数。
        这些阈值是状态推断的基础。
        """
        print(f"[OddsTSM] 拟合阈值: 从 training_extended 采样 {sample_size} 条...")

        conn = sqlite3.connect(self.db_path)

        query = f"""
        SELECT ext_id, match_date, league_name, home_team, away_team,
               open_home, open_draw, open_away,
               odds_home, odds_draw, odds_away,
               home_score, away_score, final_result
        FROM training_extended
        WHERE open_home IS NOT NULL
          AND odds_home IS NOT NULL
          AND home_score IS NOT NULL
        ORDER BY RANDOM()
        LIMIT {sample_size}
        """

        df = pd.read_sql_query(query, conn)
        conn.close()

        entropy_drifts = []
        water_accels = []
        kelly_flucts = []

        valid = 0
        for _, row in df.iterrows():
            try:
                open_snap = OddsSnapshot(
                    home=float(row["open_home"]),
                    draw=float(row["open_draw"]),
                    away=float(row["open_away"]),
                )
                close_snap = OddsSnapshot(
                    home=float(row["odds_home"]),
                    draw=float(row["odds_draw"]),
                    away=float(row["odds_away"]),
                )
                pv = self.compute_phase_vector(open_snap, close_snap)
                entropy_drifts.append(pv.entropy_drift)  # 保留原始值用于符号分析
                water_accels.append(pv.water_accel)
                kelly_flucts.append(pv.kelly_fluctuation)
                valid += 1
            except (ValueError, ZeroDivisionError):
                continue

        print(f"[OddsTSM]  有效样本: {valid}/{len(df)}")

        # 熵漂移: 用绝对值计算分位数（正负方向都是信号）
        abs_drifts = np.abs(entropy_drifts)

        self.thresholds = {
            "entropy_drift": {
                "p10": float(np.percentile(abs_drifts, 10)),
                "p20": float(np.percentile(abs_drifts, 20)),
                "p25": float(np.percentile(abs_drifts, 25)),
                "p50": float(np.percentile(abs_drifts, 50)),
                "p75": float(np.percentile(abs_drifts, 75)),
                "p80": float(np.percentile(abs_drifts, 80)),
                "p90": float(np.percentile(abs_drifts, 90)),
            },
            "water_accel": {
                "p10": float(np.percentile(water_accels, 10)),
                "p20": float(np.percentile(water_accels, 20)),
                "p25": float(np.percentile(water_accels, 25)),
                "p50": float(np.percentile(water_accels, 50)),
                "p75": float(np.percentile(water_accels, 75)),
                "p80": float(np.percentile(water_accels, 80)),
                "p90": float(np.percentile(water_accels, 90)),
            },
            "kelly_fluctuation": {
                "p10": float(np.percentile(kelly_flucts, 10)),
                "p20": float(np.percentile(kelly_flucts, 20)),
                "p25": float(np.percentile(kelly_flucts, 25)),
                "p50": float(np.percentile(kelly_flucts, 50)),
                "p75": float(np.percentile(kelly_flucts, 75)),
                "p80": float(np.percentile(kelly_flucts, 80)),
                "p90": float(np.percentile(kelly_flucts, 90)),
            },
        }

        # 状态先验分布 (基于 v2 三态模型)
        n = valid
        locked_mask = abs_drifts > self.thresholds["entropy_drift"]["p80"]
        noise_mask = abs_drifts < self.thresholds["entropy_drift"]["p20"]

        n_locked = int(np.sum(locked_mask))
        n_noise = int(np.sum(noise_mask))
        n_active = valid - n_locked - n_noise

        self.state_priors = {
            "LOCKED": n_locked / valid if valid else 0,
            "ACTIVE": n_active / valid if valid else 0,
            "NOISE": n_noise / valid if valid else 0,
            "DECOY": 0.0,  # v2 已弃用 DECOY 状态
        }

        print(f"[OddsTSM] 阈值拟合完成:")
        for dim, vals in self.thresholds.items():
            print(f"  {dim}: p50={vals['p50']:.4f}, p80={vals['p80']:.4f}")
        print(f"  状态先验: LOCKED={self.state_priors['LOCKED']:.1%}, "
              f"ACTIVE={self.state_priors['ACTIVE']:.1%}, "
              f"NOISE={self.state_priors['NOISE']:.1%}, "
              f"DECOY={self.state_priors['DECOY']:.1%}")

        return self.thresholds

    # ── 单场推断 ────────────────────────────────────────────────────────

    def infer_single(
        self,
        open_odds: Tuple[float, float, float],
        close_odds: Tuple[float, float, float],
    ) -> StateMachineResult:
        """
        单场比赛的赔率相变分析。

        Args:
            open_odds:  (home, draw, away) 开盘赔率
            close_odds: (home, draw, away) 收盘赔率

        Returns:
            StateMachineResult 含状态推断和锁定期确信度
        """
        open_snap = OddsSnapshot(*open_odds)
        close_snap = OddsSnapshot(*close_odds)
        pv = self.compute_phase_vector(open_snap, close_snap)
        state, lock_conf, probs = self.infer_state(pv)

        return StateMachineResult(
            match_id=None,
            match_date="",
            league="",
            home_team="",
            away_team="",
            open_snapshot=open_snap,
            close_snapshot=close_snap,
            phase_vector=pv,
            state=state,
            lock_confidence=lock_conf,
            state_probabilities=probs,
        )

    # ── 实时时序推断 (v2.0 生产级信号源) ──────────────────────

    def infer_realtime(self, match_id: int, db_path: Optional[str] = None) -> Optional[StateMachineResult]:
        """
        从 odds_timeline 表读取时序数据，实时推断庄家状态。
        
        这是 OTSM 从回测工具升级为生产信号源的核心方法。
        
        逻辑:
        1. 从 odds_timeline 读取该比赛的所有时序快照
        2. 用最早快照作为"开盘"，最新快照作为"收盘"
        3. 计算相空间坐标和 lock_confidence
        4. 额外输出: 熵漂移速率、赔率变化次数、最后更新时间
        
        Args:
            match_id: 比赛 ID
            db_path: 数据库路径
            
        Returns:
            StateMachineResult 或 None (无时序数据时)
        """
        db_path = db_path or self.db_path
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # 读取时序数据
        timeline = conn.execute('''
            SELECT snapshot_time, home_odds, draw_odds, away_odds,
                   change_magnitude, source
            FROM odds_timeline
            WHERE match_id = ?
            ORDER BY snapshot_time ASC
        ''', (match_id,)).fetchall()
        conn.close()

        if not timeline or len(timeline) < 1:
            return None

        # 最早 = 开盘, 最新 = 收盘
        open_row = timeline[0]
        close_row = timeline[-1]

        open_snap = OddsSnapshot(
            home=float(open_row["home_odds"]),
            draw=float(open_row["draw_odds"]),
            away=float(open_row["away_odds"]),
        )
        close_snap = OddsSnapshot(
            home=float(close_row["home_odds"]),
            draw=float(close_row["draw_odds"]),
            away=float(close_row["away_odds"]),
        )

        pv = self.compute_phase_vector(open_snap, close_snap)
        state, lock_conf, probs = self.infer_state(pv)

        # 额外实时信号
        n_snapshots = len(timeline)
        # 熵漂移速率 = 总熵漂移 / 快照数 (反映变化速度)
        entropy_rate = abs(pv.entropy_drift) / max(n_snapshots - 1, 1)

        return StateMachineResult(
            match_id=match_id,
            match_date="",
            league="",
            home_team="",
            away_team="",
            open_snapshot=open_snap,
            close_snapshot=close_snap,
            phase_vector=pv,
            state=state,
            lock_confidence=lock_conf,
            state_probabilities={**probs, "n_snapshots": n_snapshots, "entropy_rate": entropy_rate},
        )

    def batch_infer_realtime(self, db_path: Optional[str] = None, limit: int = 1000) -> List[StateMachineResult]:
        """
        批量实时推断: 对所有有待时序数据的比赛进行状态推断
        
        Returns:
            List[StateMachineResult] (含 match_id 和实时信号)
        """
        db_path = db_path or self.db_path
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # 获取所有有待时序数据的比赛
        match_ids = conn.execute('''
            SELECT DISTINCT match_id FROM odds_timeline
            WHERE match_id IN (
                SELECT match_id FROM matches WHERE home_score IS NULL
            )
            LIMIT ?
        ''', (limit,)).fetchall()
        conn.close()

        results = []
        for row in match_ids:
            result = self.infer_realtime(row["match_id"], db_path)
            if result:
                results.append(result)

        return results

    def get_realtime_signal(self, match_id: int, db_path: Optional[str] = None) -> Dict:
        """
        获取单场比赛的实时 OTSM 信号 (供前端/模型消费)
        
        Returns:
            {
                lock_confidence: float [0,1],
                temporal_lock_score: float [0,1],
                state: str,  # LOCKED/ACTIVE/NOISE
                n_snapshots: int,
                entropy_drift: float,
                water_accel: float,
                kelly_fluct: float,
                has_signal: bool,
            }
        """
        result = self.infer_realtime(match_id, db_path)
        if not result:
            return {"has_signal": False, "match_id": match_id}

        return {
            "has_signal": True,
            "match_id": match_id,
            "lock_confidence": result.lock_confidence,
            "temporal_lock_score": result.lock_confidence,  # 别名
            "state": result.state.value,
            "n_snapshots": result.state_probabilities.get("n_snapshots", 0),
            "entropy_drift": result.phase_vector.entropy_drift,
            "water_accel": result.phase_vector.water_accel,
            "kelly_fluct": result.phase_vector.kelly_fluctuation,
            "entropy_rate": result.state_probabilities.get("entropy_rate", 0),
        }

    # ── 批量分析 ────────────────────────────────────────────────────────

    def analyze_matches(
        self,
        league_filter: Optional[str] = None,
        date_from: Optional[str] = None,
        limit: int = 100000,
    ) -> List[StateMachineResult]:
        """
        批量分析比赛，返回每条的状态推断。

        Args:
            league_filter: 联赛名过滤，None=全部
            date_from:    起始日期，None=全部
            limit:        最大分析条数

        Returns:
            List[StateMachineResult]
        """
        if not self.thresholds:
            print("[OddsTSM] 阈值未拟合，先调用 fit_thresholds()...")
            self.fit_thresholds()

        print(f"[OddsTSM] 批量分析: limit={limit}, league={league_filter}, from={date_from}")

        conn = sqlite3.connect(self.db_path)
        params = []
        conditions = [
            "open_home IS NOT NULL",
            "odds_home IS NOT NULL",
            "home_score IS NOT NULL",
        ]
        if league_filter:
            conditions.append("league_name = ?")
            params.append(league_filter)
        if date_from:
            conditions.append("match_date >= ?")
            params.append(date_from)

        where = " AND ".join(conditions)
        query = f"""
        SELECT ext_id, match_date, league_name, home_team, away_team,
               open_home, open_draw, open_away,
               odds_home, odds_draw, odds_away,
               home_score, away_score, final_result
        FROM training_extended
        WHERE {where}
        ORDER BY match_date
        LIMIT {limit}
        """

        df = pd.read_sql_query(query, conn, params=params)
        conn.close()

        results = []
        skipped = 0
        for _, row in df.iterrows():
            try:
                open_snap = OddsSnapshot(
                    float(row["open_home"]), float(row["open_draw"]), float(row["open_away"])
                )
                close_snap = OddsSnapshot(
                    float(row["odds_home"]), float(row["odds_draw"]), float(row["odds_away"])
                )
                pv = self.compute_phase_vector(open_snap, close_snap)
                state, lock_conf, probs = self.infer_state(pv)

                # 回测：收盘赔率隐含的热门方向 vs 实际结果
                implied_fav = close_snap.favorite
                actual = row["final_result"]
                fav_map = {0: "H", 1: "D", 2: "A"}
                correct = (fav_map.get(implied_fav, "?") == actual)

                result = StateMachineResult(
                    match_id=row["ext_id"],
                    match_date=str(row["match_date"]),
                    league=str(row["league_name"]),
                    home_team=str(row["home_team"]),
                    away_team=str(row["away_team"]),
                    open_snapshot=open_snap,
                    close_snapshot=close_snap,
                    phase_vector=pv,
                    state=state,
                    lock_confidence=lock_conf,
                    state_probabilities=probs,
                    actual_result=actual,
                    prediction_correct=correct,
                )
                results.append(result)
            except (ValueError, ZeroDivisionError, TypeError):
                skipped += 1
                continue

        print(f"[OddsTSM] 分析完成: {len(results)} 条, 跳过 {skipped}")

        # 计算状态准确率
        state_stats = {}
        for s in PhaseState:
            subset = [r for r in results if r.state == s]
            if subset:
                acc = sum(1 for r in subset if r.prediction_correct) / len(subset)
                state_stats[s.value] = {
                    "count": len(subset),
                    "accuracy": acc,
                    "avg_lock_conf": float(np.mean([r.lock_confidence for r in subset])),
                }

        self.state_accuracy = {
            s: state_stats.get(s, {}).get("accuracy", 0) for s in [e.value for e in PhaseState]
        }

        print(f"[OddsTSM] 状态准确率回测:")
        for s in PhaseState:
            ss = state_stats.get(s.value, {})
            if ss:
                print(f"  {s.value:8s}: {ss['count']:>6d}场, "
                      f"准确率={ss['accuracy']:.1%}, "
                      f"avg_lock_conf={ss['avg_lock_conf']:.3f}")

        return results

    # ── 报告 ────────────────────────────────────────────────────────────

    def generate_report(self, results: List[StateMachineResult]) -> str:
        """生成 Markdown 分析报告"""
        if not results:
            return "# 无数据\n"

        state_counts = {}
        for s in PhaseState:
            state_counts[s.value] = sum(1 for r in results if r.state == s)

        locked_results = [r for r in results if r.state == PhaseState.LOCKED and r.lock_confidence > 0.5]

        lines = [
            "# 赔率时序状态机 — 分析报告",
            f"生成时间: {datetime.now(timezone.utc).isoformat()[:19]}",
            f"分析场次: {len(results)}",
            "",
            "## 状态分布",
            "",
            "| 状态 | 场次 | 占比 | 赔率正确率 | avg_lock_conf |",
            "|------|------|------|-----------|---------------|",
        ]
        for s in PhaseState:
            n = state_counts.get(s.value, 0)
            pct = n / len(results) * 100 if results else 0
            acc = self.state_accuracy.get(s.value, 0)
            avg_conf = 0
            subset = [r for r in results if r.state == s]
            if subset:
                avg_conf = float(np.mean([r.lock_confidence for r in subset]))
            lines.append(f"| {s.value:8s} | {n:>5d} | {pct:.1f}% | {acc:.1%} | {avg_conf:.3f} |")

        lines.extend([
            "",
            "## 锁定期高置信场次 (lock_confidence > 0.5)",
            f"共 {len(locked_results)} 场",
            "",
            "| 日期 | 联赛 | 主队 vs 客队 | open | close | 熵漂移 | 水位加速 | 凯利涨落 | lock_conf | 实际 |",
            "|------|------|-------------|------|-------|--------|----------|----------|-----------|------|",
        ])

        for r in locked_results[:50]:  # 前50条
            o = f"H{r.open_snapshot.home:.2f}/D{r.open_snapshot.draw:.2f}/A{r.open_snapshot.away:.2f}"
            c = f"H{r.close_snapshot.home:.2f}/D{r.close_snapshot.draw:.2f}/A{r.close_snapshot.away:.2f}"
            lines.append(
                f"| {r.match_date} | {r.league[:10]} | {r.home_team} vs {r.away_team} | "
                f"{o} | {c} | "
                f"{r.phase_vector.entropy_drift:+.4f} | {r.phase_vector.water_accel:+.4f} | "
                f"{r.phase_vector.kelly_fluctuation:.4f} | {r.lock_confidence:.3f} | "
                f"{r.actual_result} |"
            )

        lines.extend([
            "",
            "## 三维相空间统计",
            "",
            f"- 熵漂移: mean={np.mean([r.phase_vector.entropy_drift for r in results]):+.4f}, "
            f"std={np.std([r.phase_vector.entropy_drift for r in results]):.4f}",
            f"- 水位加速度: mean={np.mean([r.phase_vector.water_accel for r in results]):+.4f}, "
            f"std={np.std([r.phase_vector.water_accel for r in results]):.4f}",
            f"- 凯利涨落: mean={np.mean([r.phase_vector.kelly_fluctuation for r in results]):.4f}, "
            f"std={np.std([r.phase_vector.kelly_fluctuation for r in results]):.4f}",
            "",
            "## 核心发现",
            "",
            f"- 锁定期(LOCKED)场次: {state_counts.get('LOCKED', 0)}, "
            f"赔率正确率: {self.state_accuracy.get('LOCKED', 0):.1%}",
            f"- 噪声期(NOISE)场次: {state_counts.get('NOISE', 0)}, "
            f"赔率正确率: {self.state_accuracy.get('NOISE', 0):.1%}",
            f"- 诱饵期(DECOY)场次: {state_counts.get('DECOY', 0)}, "
            f"赔率正确率: {self.state_accuracy.get('DECOY', 0):.1%}",
        ])

        if self.state_accuracy.get("LOCKED", 0) > self.state_accuracy.get("NOISE", -1):
            delta = self.state_accuracy["LOCKED"] - self.state_accuracy.get("NOISE", 0)
            lines.append(f"- **锁定期 vs 噪声期准确率提升: {delta:+.1%}** ✅")
        else:
            lines.append("- ⚠️ 锁定期准确率未显著高于噪声期，阈值需调优")

        report = "\n".join(lines)

        # 保存
        report_path = os.path.join(self.output_dir,
                                   f"temporal_sm_report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)

        # 保存结果 JSON
        json_path = os.path.join(self.output_dir,
                                 f"temporal_sm_results_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json")
        json_data = {
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "total_matches": len(results),
            "state_distribution": state_counts,
            "state_accuracy": self.state_accuracy,
            "thresholds": self.thresholds,
            "top_locked": [
                {
                    "match_date": r.match_date,
                    "home_team": r.home_team,
                    "away_team": r.away_team,
                    "league": r.league,
                    "lock_confidence": r.lock_confidence,
                    "phase_vector": {
                        "entropy_drift": r.phase_vector.entropy_drift,
                        "water_accel": r.phase_vector.water_accel,
                        "kelly_fluctuation": r.phase_vector.kelly_fluctuation,
                    },
                    "actual": r.actual_result,
                    "correct": r.prediction_correct,
                }
                for r in locked_results[:100]
            ],
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

        print(f"[OddsTSM] 报告: {report_path}")
        print(f"[OddsTSM] JSON:  {json_path}")

        return report

# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="哨响AI — 赔率时序状态机 v1.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python odds_temporal_sm.py --analyze                    # 全量分析 + 报告
  python odds_temporal_sm.py --analyze --league 英超       # 单联赛分析
  python odds_temporal_sm.py --analyze --sample 5000       # 采样分析
  python odds_temporal_sm.py --infer 1.8 3.6 4.0 1.6 3.8 4.5  # 单场推断
        """,
    )
    parser.add_argument("--analyze", action="store_true", help="批量分析")
    parser.add_argument("--league", type=str, help="联赛过滤")
    parser.add_argument("--sample", type=int, default=100000, help="分析样本数")
    parser.add_argument("--date-from", type=str, help="起始日期")
    parser.add_argument("--infer", nargs=6, type=float, metavar=("OH","OD","OA","CH","CD","CA"),
                        help="单场推断: open_h open_d open_a close_h close_d close_a")
    parser.add_argument("--fit-only", action="store_true", help="仅拟合阈值")

    args = parser.parse_args()

    sm = OddsTemporalStateMachine()

    if args.infer:
        oh, od, oa, ch, cd, ca = args.infer
        result = sm.infer_single((oh, od, oa), (ch, cd, ca))
        print("\n═══ 单场赔率相变分析 ═══")
        print(f"开盘: H={oh:.2f} D={od:.2f} A={oa:.2f} "
              f"(熵={result.open_snapshot.entropy:.4f}, 抽水={result.open_snapshot.overround:.4f})")
        print(f"收盘: H={ch:.2f} D={cd:.2f} A={ca:.2f} "
              f"(熵={result.close_snapshot.entropy:.4f}, 抽水={result.close_snapshot.overround:.4f})")
        print(f"\n相空间坐标:")
        print(f"  D1 熵漂移:   {result.phase_vector.entropy_drift:+.4f}")
        print(f"  D2 水位加速: {result.phase_vector.water_accel:+.4f}")
        print(f"  D3 凯利涨落: {result.phase_vector.kelly_fluctuation:.4f}")
        print(f"  合成向量模:  {result.phase_vector.magnitude:.4f}")
        print(f"\n状态推断: {result.state.value}")
        print(f"锁定期确信度: {result.lock_confidence:.3f}")
        print(f"状态概率: {result.state_probabilities}")
        return

    sm.fit_thresholds()

    if args.fit_only:
        return

    if args.analyze:
        results = sm.analyze_matches(
            league_filter=args.league,
            date_from=args.date_from,
            limit=args.sample,
        )
        sm.generate_report(results)

if __name__ == "__main__":
    main()
