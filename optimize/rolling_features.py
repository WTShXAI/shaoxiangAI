"""
哨响AI - 滚动窗口特征生成器 (T12)
====================================
从比赛历史数据计算多窗口滚动统计特征，丰富模型输入。

核心设计：
  1. 多窗口统计 (3/5/10场) — 捕获短期/中期/长期状态
  2. 方差/一致性指标 — 度量球队稳定性
  3. 主客场分离统计 — 捕获主场优势
  4. 趋势/加速度特征 — 识别上升/下滑
  5. 对手强度调整 — 校正赛程难度偏差
  6. 特征重要性分析 — 评估特征贡献

输出特征可直接：
  - 写入 match_features 表供 GBDT 模型使用
  - 追加到 SequenceBundle.static_features 供 DL 模型使用
"""

import sqlite3
import logging
import warnings
from typing import Dict, List, Tuple, Optional, Sequence
from dataclasses import dataclass, field
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════════

@dataclass
class RollingWindowConfig:
    """滚动窗口特征配置"""
    # 窗口大小
    windows: Tuple[int, ...] = (3, 5, 10)

    # 各窗口统计项 (每项会对每个窗口生成一个特征)
    # 基础统计
    compute_win_rate: bool = True
    compute_draw_rate: bool = True
    compute_avg_gf: bool = True          # 场均进球
    compute_avg_ga: bool = True          # 场均失球
    compute_avg_gd: bool = True          # 场均净胜球
    compute_avg_pts: bool = True         # 场均得分
    compute_cs_rate: bool = True         # 零封率
    compute_btts_rate: bool = True        # 双方进球率
    compute_over25_rate: bool = True     # 大2.5球率

    # 方差/一致性
    compute_std_gf: bool = True          # 进球标准差
    compute_std_ga: bool = True          # 失球标准差
    compute_std_pts: bool = True        # 得分标准差

    # 主客场分离 (仅5场窗口)
    compute_home_away_split: bool = True
    home_away_window: int = 5

    # 趋势特征
    compute_form_trend: bool = True      # 形式趋势 (线性回归斜率)
    compute_momentum_shift: bool = True  # 动量偏移 (近3 vs 前3)

    # 对手强度调整
    compute_opp_adjusted: bool = True    # 对手强度校正得分
    compute_power_score: bool = True     # 综合实力评分

    # 交锋历史 (h2h)
    compute_h2h_features: bool = True
    h2h_window: int = 5

    # 归一化
    max_goals_norm: float = 3.0         # 进球归一化分母
    max_pts_norm: float = 3.0           # 得分归一化分母
    max_streak_norm: float = 5.0        # 连胜归一化分母

# ═══════════════════════════════════════════════════════════════════
#  特征名称注册表
# ═══════════════════════════════════════════════════════════════════

def get_rolling_feature_names(config: Optional[RollingWindowConfig] = None) -> List[str]:
    """返回所有滚动窗口特征名称（按固定顺序）"""
    cfg = config or RollingWindowConfig()
    names = []

    for w in cfg.windows:
        prefix = f"r{w}"
        if cfg.compute_win_rate:    names.append(f"{prefix}_win_pct")
        if cfg.compute_draw_rate:   names.append(f"{prefix}_draw_pct")
        if cfg.compute_avg_gf:     names.append(f"{prefix}_avg_gf")
        if cfg.compute_avg_ga:     names.append(f"{prefix}_avg_ga")
        if cfg.compute_avg_gd:     names.append(f"{prefix}_avg_gd")
        if cfg.compute_avg_pts:   names.append(f"{prefix}_avg_pts")
        if cfg.compute_cs_rate:    names.append(f"{prefix}_cs_pct")
        if cfg.compute_btts_rate:  names.append(f"{prefix}_btts_pct")
        if cfg.compute_over25_rate: names.append(f"{prefix}_over25_pct")
        if cfg.compute_std_gf:     names.append(f"{prefix}_std_gf")
        if cfg.compute_std_ga:     names.append(f"{prefix}_std_ga")
        if cfg.compute_std_pts:   names.append(f"{prefix}_std_pts")

    # 主客场分离 (仅5场窗口)
    if cfg.compute_home_away_split:
        hw = cfg.home_away_window
        names.extend([
            f"rhome{hw}_win_pct", f"rhome{hw}_avg_gf", f"rhome{hw}_avg_ga",
            f"raway{hw}_win_pct", f"raway{hw}_avg_gf", f"raway{hw}_avg_ga",
        ])

    # 趋势特征
    if cfg.compute_form_trend:
        names.extend(["rtrend_pts_10", "rtrend_gf_10", "rtrend_ga_10"])
    if cfg.compute_momentum_shift:
        names.append("rmomentum_shift")

    # 对手强度调整
    if cfg.compute_opp_adjusted:
        names.append("radj_pts_5")
    if cfg.compute_power_score:
        names.append("rpower_score_10")

    # 交锋历史
    if cfg.compute_h2h_features:
        names.extend([
            f"rh2h_home_w{cfg.h2h_window}",
            f"rh2h_avg_gd{cfg.h2h_window}",
        ])

    return names

# ═══════════════════════════════════════════════════════════════════
#  滚动窗口特征生成器
# ═══════════════════════════════════════════════════════════════════

class RollingWindowFeatureGenerator:
    """
    滚动窗口特征生成器。

    从数据库加载比赛历史，按球队维护滑动窗口，
    为每场比赛计算主客队的滚动统计特征。
    """

    def __init__(
        self,
        db_path: str = 'data/football_data.db',
        config: Optional[RollingWindowConfig] = None,
    ):
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            self.db_path = Path(__file__).parent.parent / db_path
        self.config = config or RollingWindowConfig()
        self.feature_names = get_rolling_feature_names(self.config)

    # ─── 数据加载 ─────────────────────────────────────────────

    def _load_matches(self) -> pd.DataFrame:
        """加载所有完赛比赛，按日期排序"""
        conn = sqlite3.connect(str(self.db_path))
        query = """
        SELECT match_id, home_team_name, away_team_name, match_date,
               league_name, home_score, away_score,
               halftime_home, halftime_away
        FROM matches
        WHERE home_score IS NOT NULL AND away_score IS NOT NULL
          AND status = 'finished'
        ORDER BY match_date ASC, match_id ASC
        """
        df = pd.read_sql_query(query, conn)
        conn.close()
        logger.info(f"[RollingGen] loaded {len(df)} matches")
        return df

    # ─── 单队滚动统计 ─────────────────────────────────────────

    @staticmethod
    def _team_stats_from_history(
        history: List[dict],
        window: int,
        cfg: RollingWindowConfig,
    ) -> dict:
        """从球队历史计算一个窗口的所有统计量

        Args:
            history: 球队历史记录列表，每项含 'pts','gf','ga','gd','cs','btts','over25','is_home','opp_pts'
            window: 窗口大小
            cfg: 配置

        Returns:
            dict: {feature_name: value} 仅包含配置中启用的特征
        """
        recent = history[-window:] if len(history) >= window else history
        n = len(recent)
        if n == 0:
            return {}

        pts_list = [h['pts'] for h in recent]
        gf_list = [h['gf'] for h in recent]
        ga_list = [h['ga'] for h in recent]
        gd_list = [h['gd'] for h in recent]
        cs_list = [h['cs'] for h in recent]
        btts_list = [h['btts'] for h in recent]
        over25_list = [h['over25'] for h in recent]

        p = f"r{window}"
        stats = {}

        if cfg.compute_win_rate:
            stats[f"{p}_win_pct"] = sum(1 for x in pts_list if x == 3) / n
        if cfg.compute_draw_rate:
            stats[f"{p}_draw_pct"] = sum(1 for x in pts_list if x == 1) / n
        if cfg.compute_avg_gf:
            stats[f"{p}_avg_gf"] = np.mean(gf_list) / cfg.max_goals_norm
        if cfg.compute_avg_ga:
            stats[f"{p}_avg_ga"] = np.mean(ga_list) / cfg.max_goals_norm
        if cfg.compute_avg_gd:
            stats[f"{p}_avg_gd"] = np.mean(gd_list) / (cfg.max_goals_norm * 2)
        if cfg.compute_avg_pts:
            stats[f"{p}_avg_pts"] = np.mean(pts_list) / cfg.max_pts_norm
        if cfg.compute_cs_rate:
            stats[f"{p}_cs_pct"] = sum(cs_list) / n
        if cfg.compute_btts_rate:
            stats[f"{p}_btts_pct"] = sum(btts_list) / n
        if cfg.compute_over25_rate:
            stats[f"{p}_over25_pct"] = sum(over25_list) / n
        if cfg.compute_std_gf:
            stats[f"{p}_std_gf"] = np.std(gf_list) / cfg.max_goals_norm if n > 1 else 0.0
        if cfg.compute_std_ga:
            stats[f"{p}_std_ga"] = np.std(ga_list) / cfg.max_goals_norm if n > 1 else 0.0
        if cfg.compute_std_pts:
            stats[f"{p}_std_pts"] = np.std(pts_list) / cfg.max_pts_norm if n > 1 else 0.0

        return stats

    @staticmethod
    def _home_away_split_stats(
        history: List[dict],
        window: int,
        is_home: bool,
    ) -> dict:
        """计算主/客场分离统计

        Args:
            history: 球队历史
            window: 窗口大小
            is_home: True=仅主场, False=仅客场
        """
        filtered = [h for h in history if h['is_home'] == is_home]
        recent = filtered[-window:] if len(filtered) >= window else filtered
        n = len(recent)
        if n == 0:
            return {
                f"r{'home' if is_home else 'away'}{window}_win_pct": 0.5,
                f"r{'home' if is_home else 'away'}{window}_avg_gf": 0.5,
                f"r{'home' if is_home else 'away'}{window}_avg_ga": 0.5,
            }

        prefix = f"r{'home' if is_home else 'away'}{window}"
        return {
            f"{prefix}_win_pct": sum(1 for h in recent if h['pts'] == 3) / n,
            f"{prefix}_avg_gf": np.mean([h['gf'] for h in recent]) / 3.0,
            f"{prefix}_avg_ga": np.mean([h['ga'] for h in recent]) / 3.0,
        }

    @staticmethod
    def _compute_trend(history: List[dict], field: str, window: int = 10) -> float:
        """计算线性趋势 (回归斜率), 归一化到 [-1, 1]

        Args:
            history: 球队历史
            field: 字段名 ('pts', 'gf', 'ga')
            window: 窗口大小
        """
        recent = history[-window:] if len(history) >= window else history
        n = len(recent)
        if n < 3:
            return 0.0

        values = [h[field] for h in recent]
        x = np.arange(n, dtype=np.float64)
        y = np.array(values, dtype=np.float64)

        # 简单线性回归斜率
        x_mean = x.mean()
        y_mean = y.mean()
        denom = ((x - x_mean) ** 2).sum()
        if denom < 1e-8:
            return 0.0

        slope = ((x - x_mean) * (y - y_mean)).sum() / denom
        # 归一化: 斜率范围大约 [-1.5, 1.5] → clip to [-1, 1]
        if field == 'pts':
            return float(np.clip(slope / 1.5, -1.0, 1.0))
        elif field == 'gf' or field == 'ga':
            return float(np.clip(slope / 1.0, -1.0, 1.0))
        return float(np.clip(slope / 2.0, -1.0, 1.0))

    @staticmethod
    def _compute_momentum_shift(history: List[dict]) -> float:
        """动量偏移: 近3场得分均值 - 前3场得分均值 (近6场内)

        正值=上升, 负值=下滑, 归一化到 [-1, 1]
        """
        if len(history) < 4:
            return 0.0

        recent6 = history[-6:] if len(history) >= 6 else history
        n = len(recent6)
        half = n // 2
        if half < 1:
            return 0.0

        # 前半部分 (更早) 和后半部分 (更近)
        early_pts = [h['pts'] for h in recent6[:half]]
        late_pts = [h['pts'] for h in recent6[half:]]

        diff = np.mean(late_pts) - np.mean(early_pts)
        # 范围约 [-3, 3] → [-1, 1]
        return float(np.clip(diff / 3.0, -1.0, 1.0))

    @staticmethod
    def _compute_opp_adjusted_pts(history: List[dict], window: int = 5) -> float:
        """对手强度校正得分

        对手越强(pts越高), 赢球价值越大
        """
        recent = history[-window:] if len(history) >= window else history
        n = len(recent)
        if n == 0:
            return 0.5

        weighted_pts = 0.0
        total_weight = 0.0
        for h in recent:
            # 对手近5场得分越高 → 权重越大 (0.5 ~ 1.5)
            opp_pts = h.get('opp_pts', 1.5)  # 对手近期场均分
            weight = 0.5 + opp_pts / 3.0      # 映射到 [0.5, 1.5]
            weighted_pts += h['pts'] * weight
            total_weight += weight

        if total_weight < 1e-8:
            return 0.5
        return float(np.clip(weighted_pts / total_weight / 3.0, 0.0, 1.0))

    @staticmethod
    def _compute_power_score(history: List[dict], window: int = 10) -> float:
        """综合实力评分 (0~1)

        攻击力40% + 防守力30% + 形式30%
        """
        recent = history[-window:] if len(history) >= window else history
        n = len(recent)
        if n == 0:
            return 0.5

        gf = np.mean([h['gf'] for h in recent]) / 3.0
        ga_inv = 1.0 - np.mean([h['ga'] for h in recent]) / 3.0
        pts = np.mean([h['pts'] for h in recent]) / 3.0

        score = 0.4 * gf + 0.3 * ga_inv + 0.3 * pts
        return float(np.clip(score, 0.0, 1.0))

    @staticmethod
    def _compute_h2h_features(
        h2h_history: List[dict],
        home_team: str,
        window: int = 5,
    ) -> dict:
        """交锋历史特征

        Args:
            h2h_history: 两队交锋记录 [{home, away, home_score, away_score}, ...]
            home_team: 本场主队名
            window: 窗口大小
        """
        recent = h2h_history[-window:] if len(h2h_history) >= window else h2h_history
        n = len(recent)
        if n == 0:
            return {
                f"rh2h_home_w{window}": 0.5,
                f"rh2h_avg_gd{window}": 0.0,
            }

        home_wins = 0
        gd_list = []
        for h in recent:
            if h['home'] == home_team:
                # home_team 是该场主队 → 从主视角看
                gd = h['home_score'] - h['away_score']
                if gd > 0:
                    home_wins += 1
            else:
                # home_team 是该场客队 → 从客视角看
                gd = h['away_score'] - h['home_score']
                if gd > 0:
                    home_wins += 1
            gd_list.append(gd)

        return {
            f"rh2h_home_w{window}": home_wins / n,
            f"rh2h_avg_gd{window}": float(np.clip(np.mean(gd_list) / 3.0, -1.0, 1.0)),
        }

    # ─── 主流程 ─────────────────────────────────────────────

    def generate(self) -> pd.DataFrame:
        """生成所有比赛的滚动窗口特征

        Returns:
            DataFrame: columns = [match_id, home_*, away_*, diff_*]
            其中 home_* / away_* 为各队特征, diff_* = home - away
        """
        df = self._load_matches()
        cfg = self.config

        # 构建球队历史索引
        team_history: Dict[str, List[dict]] = defaultdict(list)
        # 交锋历史索引: (team_a, team_b) → [match_record, ...]
        pair_key = lambda a, b: tuple(sorted([a, b]))
        h2h_history: Dict[tuple, List[dict]] = defaultdict(list)

        results = []
        feature_names = self.feature_names

        for idx, row in df.iterrows():
            ht = row['home_team_name']
            at = row['away_team_name']
            hs = int(row['home_score'])
            aws = int(row['away_score'])
            match_date = str(row['match_date'])

            home_hist = team_history.get(ht, [])
            away_hist = team_history.get(at, [])
            pair = pair_key(ht, at)
            h2h = h2h_history.get(pair, [])

            # 至少需要3场历史才计算
            if len(home_hist) >= 3 and len(away_hist) >= 3:
                record = {'match_id': row['match_id'], 'match_date': match_date}

                # ─── 主队滚动特征 ───
                home_stats = {}
                for w in cfg.windows:
                    home_stats.update(
                        self._team_stats_from_history(home_hist, w, cfg)
                    )

                # 主客场分离
                if cfg.compute_home_away_split:
                    hw = cfg.home_away_window
                    home_stats.update(self._home_away_split_stats(home_hist, hw, is_home=True))
                    home_stats.update(self._home_away_split_stats(home_hist, hw, is_home=False))

                # 趋势
                if cfg.compute_form_trend:
                    home_stats['rtrend_pts_10'] = self._compute_trend(home_hist, 'pts', 10)
                    home_stats['rtrend_gf_10'] = self._compute_trend(home_hist, 'gf', 10)
                    home_stats['rtrend_ga_10'] = self._compute_trend(home_hist, 'ga', 10)

                if cfg.compute_momentum_shift:
                    home_stats['rmomentum_shift'] = self._compute_momentum_shift(home_hist)

                if cfg.compute_opp_adjusted:
                    home_stats['radj_pts_5'] = self._compute_opp_adjusted_pts(home_hist, 5)

                if cfg.compute_power_score:
                    home_stats['rpower_score_10'] = self._compute_power_score(home_hist, 10)

                # 交锋
                if cfg.compute_h2h_features:
                    home_stats.update(self._compute_h2h_features(h2h, ht, cfg.h2h_window))

                # ─── 客队滚动特征 ───
                away_stats = {}
                for w in cfg.windows:
                    away_stats.update(
                        self._team_stats_from_history(away_hist, w, cfg)
                    )

                if cfg.compute_home_away_split:
                    hw = cfg.home_away_window
                    away_stats.update(self._home_away_split_stats(away_hist, hw, is_home=True))
                    away_stats.update(self._home_away_split_stats(away_hist, hw, is_home=False))

                if cfg.compute_form_trend:
                    away_stats['rtrend_pts_10'] = self._compute_trend(away_hist, 'pts', 10)
                    away_stats['rtrend_gf_10'] = self._compute_trend(away_hist, 'gf', 10)
                    away_stats['rtrend_ga_10'] = self._compute_trend(away_hist, 'ga', 10)

                if cfg.compute_momentum_shift:
                    away_stats['rmomentum_shift'] = self._compute_momentum_shift(away_hist)

                if cfg.compute_opp_adjusted:
                    away_stats['radj_pts_5'] = self._compute_opp_adjusted_pts(away_hist, 5)

                if cfg.compute_power_score:
                    away_stats['rpower_score_10'] = self._compute_power_score(away_hist, 10)

                if cfg.compute_h2h_features:
                    away_stats.update(self._compute_h2h_features(h2h, at, cfg.h2h_window))

                # ─── 组装: home_*, away_*, diff_* ───
                for fname in feature_names:
                    hv = home_stats.get(fname, 0.0)
                    av = away_stats.get(fname, 0.0)
                    record[f"home_{fname}"] = hv
                    record[f"away_{fname}"] = av
                    record[f"diff_{fname}"] = hv - av

                results.append(record)

            # 更新历史
            home_rec = {
                'pts': 3 if hs > aws else (1 if hs == aws else 0),
                'gf': hs, 'ga': aws, 'gd': hs - aws,
                'cs': 1 if aws == 0 else 0,
                'btts': 1 if (hs > 0 and aws > 0) else 0,
                'over25': 1 if (hs + aws) > 2 else 0,
                'is_home': True,
                'opp_pts': np.mean([h['pts'] for h in away_hist[-5:]]) if away_hist else 1.5,
            }
            away_rec = {
                'pts': 3 if aws > hs else (1 if aws == hs else 0),
                'gf': aws, 'ga': hs, 'gd': aws - hs,
                'cs': 1 if hs == 0 else 0,
                'btts': 1 if (hs > 0 and aws > 0) else 0,
                'over25': 1 if (hs + aws) > 2 else 0,
                'is_home': False,
                'opp_pts': np.mean([h['pts'] for h in home_hist[-5:]]) if home_hist else 1.5,
            }
            team_history[ht].append(home_rec)
            team_history[at].append(away_rec)

            # 更新H2H
            h2h_history[pair].append({
                'home': ht, 'away': at,
                'home_score': hs, 'away_score': aws,
            })

        result_df = pd.DataFrame(results)
        logger.info(
            f"[RollingGen] generated {len(result_df)} samples, "
            f"{len([c for c in result_df.columns if c.startswith('diff_')])} diff features"
        )
        return result_df

    # ─── 数据库写入 ─────────────────────────────────────────────

    def write_to_db(self, features_df: Optional[pd.DataFrame] = None) -> int:
        """将滚动特征写入 match_features 表

        Args:
            features_df: 如果为 None, 则自动生成

        Returns:
            写入的行数
        """
        if features_df is None:
            features_df = self.generate()

        if features_df.empty:
            logger.warning("[RollingGen] no features to write")
            return 0

        conn = sqlite3.connect(str(self.db_path))

        # 添加新列到 match_features (如果不存在)
        cursor = conn.execute("PRAGMA table_info(match_features)")
        existing_cols = set(row[1] for row in cursor.fetchall())

        new_cols = [c for c in features_df.columns
                     if c not in ('match_id', 'match_date') and c not in existing_cols]
        for col in new_cols:
            try:
                conn.execute(f"ALTER TABLE match_features ADD COLUMN [{col}] REAL")
            except sqlite3.OperationalError:
                pass  # 列已存在

        # 批量更新
        updated = 0
        for _, row in features_df.iterrows():
            match_id = row['match_id']
            update_cols = [c for c in features_df.columns
                           if c not in ('match_id', 'match_date') and c in new_cols]
            if not update_cols:
                continue
            set_clause = ", ".join(f"[{c}] = ?" for c in update_cols)
            values = [row[c] for c in update_cols] + [match_id]
            try:
                conn.execute(
                    f"UPDATE match_features SET {set_clause} WHERE match_id = ?",
                    values,
                )
                updated += 1
            except sqlite3.OperationalError as e:
                logger.debug(f"skip match_id={match_id}: {e}")

        conn.commit()
        conn.close()
        logger.info(f"[RollingGen] wrote {updated} rows, {len(new_cols)} new columns")
        return updated

    # ─── SequenceBundle 增强 ─────────────────────────────────────

    def augment_bundle(
        self,
        bundle,  # SequenceBundle
        features_df: Optional[pd.DataFrame] = None,
        mode: str = 'diff',
    ) -> object:
        """将滚动特征追加到 SequenceBundle 的 static_features

        Args:
            bundle: SequenceBundle 实例
            features_df: 滚动特征 DataFrame (None=自动生成)
            mode: 'diff' 仅差值特征 | 'both' 主客+差值 | 'home' 仅主队

        Returns:
            修改后的 SequenceBundle (static_features 维度增加)
        """
        if features_df is None:
            features_df = self.generate()

        # 按 match_id 索引
        feat_indexed = features_df.set_index('match_id')

        # 选择特征列
        if mode == 'diff':
            use_cols = [c for c in feat_indexed.columns if c.startswith('diff_')]
        elif mode == 'both':
            use_cols = [c for c in feat_indexed.columns
                         if c.startswith('home_') or c.startswith('away_') or c.startswith('diff_')]
        elif mode == 'home':
            use_cols = [c for c in feat_indexed.columns if c.startswith('home_')]
        else:
            raise ValueError(f"unknown mode: {mode}")

        # 逐样本追加
        n = bundle.n_samples
        n_new = len(use_cols)
        new_features = np.zeros((n, n_new), dtype=np.float32)

        for i in range(n):
            mid = bundle.match_ids[i]
            if mid in feat_indexed.index:
                row = feat_indexed.loc[mid]
                for j, col in enumerate(use_cols):
                    val = row[col] if col in row.index else 0.0
                    new_features[i, j] = float(val)

        # 拼接到 static_features
        old_static = bundle.static_features
        bundle.static_features = np.hstack([old_static, new_features])

        # 命名规则: diff_r3_win_pct → rw_r3_win_pct, home_r3_win_pct → rw_h_r3_win_pct
        def _rename_col(c):
            if c.startswith('diff_'):
                return 'rw_' + c[5:]       # diff_r3_win_pct → rw_r3_win_pct
            elif c.startswith('home_'):
                return 'rw_h_' + c[5:]     # home_r3_win_pct → rw_h_r3_win_pct
            elif c.startswith('away_'):
                return 'rw_a_' + c[5:]     # away_r3_win_pct → rw_a_r3_win_pct
            return c

        bundle.static_feature_names = list(bundle.static_feature_names) + [
            _rename_col(c) for c in use_cols
        ]

        logger.info(
            f"[RollingGen] augmented bundle: static_features "
            f"{old_static.shape[1]} -> {bundle.static_features.shape[1]} (+{n_new})"
        )
        return bundle

# ═══════════════════════════════════════════════════════════════════
#  特征重要性分析器
# ═══════════════════════════════════════════════════════════════════

class FeatureImportanceAnalyzer:
    """
    特征重要性分析器。

    支持多种方法：
    1. 排列重要性 (Permutation Importance) — 模型无关
    2. 互信息 (Mutual Information) — 统计方法
    3. 相关性分析 (Correlation) — 线性关系
    4. GBDT 内置重要性 — 树模型原生
    5. SHAP 值 (可选, 需 shap 库)
    """

    def __init__(
        self,
        random_state: int = 42,
        n_repeats: int = 10,
        max_shap_samples: int = 5000,
    ):
        self.random_state = random_state
        self.n_repeats = n_repeats
        self.max_shap_samples = max_shap_samples

    # ─── 排列重要性 ─────────────────────────────────────────────

    def permutation_importance(
        self,
        model,
        X: np.ndarray,
        y: np.ndarray,
        feature_names: Optional[List[str]] = None,
        scoring: str = 'accuracy',
        n_repeats: Optional[int] = None,
    ) -> pd.DataFrame:
        """计算排列重要性

        Args:
            model: 已训练的模型 (需支持 .predict 或 .predict_proba)
            X: 特征矩阵
            y: 标签
            feature_names: 特征名称
            scoring: 评估指标 ('accuracy', 'log_loss')
            n_repeats: 排列重复次数

        Returns:
            DataFrame: [feature, importance_mean, importance_std]
        """
        from sklearn.inspection import permutation_importance as sk_pi
        from sklearn.metrics import make_scorer, accuracy_score, log_loss

        n_repeats = n_repeats or self.n_repeats

        # 包装模型以兼容 sklearn
        if not hasattr(model, 'predict'):
            # PyTorch 模型包装
            model = _SklearnModelWrapper(model)

        if scoring == 'log_loss':
            scorer = make_scorer(
                lambda y_true, y_pred: -log_loss(y_true, y_pred, labels=[0, 1, 2]),
                needs_proba=True,
            )
        else:
            scorer = 'accuracy'

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = sk_pi(
                model, X, y,
                n_repeats=n_repeats,
                random_state=self.random_state,
                scoring=scorer,
            )

        if feature_names is None:
            feature_names = [f"f{i}" for i in range(X.shape[1])]

        df = pd.DataFrame({
            'feature': feature_names,
            'importance_mean': result.importances_mean,
            'importance_std': result.importances_std,
        }).sort_values('importance_mean', ascending=False).reset_index(drop=True)

        return df

    # ─── 互信息 ─────────────────────────────────────────────

    def mutual_information(
        self,
        X: np.ndarray,
        y: np.ndarray,
        feature_names: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """计算互信息 (特征与标签的相关程度)

        Returns:
            DataFrame: [feature, mi_score]
        """
        from sklearn.feature_selection import mutual_info_classif

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            mi = mutual_info_classif(
                X, y,
                random_state=self.random_state,
                n_neighbors=5,
            )

        if feature_names is None:
            feature_names = [f"f{i}" for i in range(X.shape[1])]

        df = pd.DataFrame({
            'feature': feature_names,
            'mi_score': mi,
        }).sort_values('mi_score', ascending=False).reset_index(drop=True)

        return df

    # ─── 相关性分析 ─────────────────────────────────────────────

    def correlation_analysis(
        self,
        X: np.ndarray,
        y: np.ndarray,
        feature_names: Optional[List[str]] = None,
        method: str = 'spearman',
    ) -> pd.DataFrame:
        """计算特征与标签的相关性

        Args:
            method: 'pearson' 或 'spearman'

        Returns:
            DataFrame: [feature, corr, abs_corr, p_value]
        """
        from scipy.stats import pearsonr, spearmanr

        corr_func = pearsonr if method == 'pearson' else spearmanr

        if feature_names is None:
            feature_names = [f"f{i}" for i in range(X.shape[1])]

        rows = []
        for i, fname in enumerate(feature_names):
            col = X[:, i]
            if np.std(col) < 1e-12:
                # 常量特征, 无法计算相关系数
                rows.append({
                    'feature': fname,
                    'corr': 0.0,
                    'abs_corr': 0.0,
                    'p_value': 1.0,
                })
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    corr, pval = corr_func(col, y)
                rows.append({
                    'feature': fname,
                    'corr': corr,
                    'abs_corr': abs(corr),
                    'p_value': pval,
                })

        df = pd.DataFrame(rows).sort_values('abs_corr', ascending=False).reset_index(drop=True)
        return df

    # ─── GBDT 内置重要性 ─────────────────────────────────────────

    def gbdt_builtin_importance(
        self,
        model,
        feature_names: Optional[List[str]] = None,
        importance_type: str = 'gain',
    ) -> pd.DataFrame:
        """提取 GBDT 模型的内置特征重要性

        Args:
            model: XGBoost/LightGBM/CatBoost 模型
            importance_type: 'gain', 'split', 'weight'

        Returns:
            DataFrame: [feature, importance]
        """
        # XGBoost
        if hasattr(model, 'get_booster'):
            booster = model.get_booster()
            raw = booster.get_score(importance_type=importance_type)
            if feature_names is None:
                feature_names = [f"f{i}" for i in range(len(raw))]
            imp_map = {k: v for k, v in raw.items()}
            importances = [imp_map.get(f"f{i}", 0.0) for i in range(len(feature_names))]

        # LightGBM
        elif hasattr(model, 'feature_importances_'):
            importances = model.feature_importances_
            if importance_type == 'gain' and hasattr(model, 'booster_'):
                try:
                    importances = model.booster_.feature_importance(importance_type='gain')
                except (Exception, KeyError, IndexError, requests.exceptions.RequestException):
                    pass

        # CatBoost
        elif hasattr(model, 'get_feature_importance'):
            importances = model.get_feature_importance(type=importance_type)

        else:
            raise ValueError("unknown model type for builtin importance")

        if feature_names is None:
            feature_names = [f"f{i}" for i in range(len(importances))]

        df = pd.DataFrame({
            'feature': feature_names[:len(importances)],
            'importance': importances,
        }).sort_values('importance', ascending=False).reset_index(drop=True)

        return df

    # ─── SHAP 值 (可选) ─────────────────────────────────────────────

    def shap_analysis(
        self,
        model,
        X: np.ndarray,
        feature_names: Optional[List[str]] = None,
        max_samples: Optional[int] = None,
    ) -> pd.DataFrame:
        """SHAP 值分析 (需要 shap 库)

        Args:
            model: 已训练模型
            X: 特征矩阵
            feature_names: 特征名称
            max_samples: 最大采样数

        Returns:
            DataFrame: [feature, shap_mean, shap_std]
        """
        try:
            import shap
        except ImportError:
            logger.warning("[FeatureImportance] shap not installed, skipping SHAP analysis")
            return pd.DataFrame()

        max_samples = max_samples or self.max_shap_samples
        if X.shape[0] > max_samples:
            idx = np.random.RandomState(self.random_state).choice(
                X.shape[0], max_samples, replace=False
            )
            X_sample = X[idx]
        else:
            X_sample = X

        # TreeExplainer for tree models, KernelExplainer otherwise
        try:
            explainer = shap.TreeExplainer(model)
            shap_values = explainer.shap_values(X_sample)
        except (Exception, KeyError, IndexError):
            try:
                if hasattr(model, 'predict_proba'):
                    predict_fn = model.predict_proba
                else:
                    predict_fn = model.predict

                background = shap.sample(X_sample, min(100, X_sample.shape[0]))
                explainer = shap.KernelExplainer(predict_fn, background)
                shap_values = explainer.shap_values(X_sample[:200])
            except (Exception, KeyError, IndexError) as e:
                logger.warning(f"[FeatureImportance] SHAP failed: {e}")
                return pd.DataFrame()

        # 对多分类取平均绝对值
        if isinstance(shap_values, list):
            # 多分类: list of (n_samples, n_features)
            shap_arr = np.mean([np.abs(sv) for sv in shap_values], axis=0)
        else:
            shap_arr = np.abs(shap_values)

        if shap_arr.ndim > 2:
            shap_arr = shap_arr.mean(axis=-1)  # 多输出取平均

        shap_mean = shap_arr.mean(axis=0)
        shap_std = shap_arr.std(axis=0)

        if feature_names is None:
            feature_names = [f"f{i}" for i in range(len(shap_mean))]

        df = pd.DataFrame({
            'feature': feature_names[:len(shap_mean)],
            'shap_mean': shap_mean,
            'shap_std': shap_std,
        }).sort_values('shap_mean', ascending=False).reset_index(drop=True)

        return df

    # ─── 综合报告 ─────────────────────────────────────────────

    def full_report(
        self,
        X: np.ndarray,
        y: np.ndarray,
        feature_names: Optional[List[str]] = None,
        model=None,
        include_shap: bool = False,
    ) -> pd.DataFrame:
        """生成综合特征重要性报告

        合并多种方法的结果，给出综合排名。

        Args:
            X: 特征矩阵
            y: 标签
            feature_names: 特征名称
            model: 已训练模型 (可选, 用于排列重要性和SHAP)
            include_shap: 是否包含SHAP分析

        Returns:
            DataFrame: 综合报告
        """
        if feature_names is None:
            feature_names = [f"f{i}" for i in range(X.shape[1])]

        # 1. 互信息
        mi_df = self.mutual_information(X, y, feature_names)

        # 2. 相关性
        corr_df = self.correlation_analysis(X, y, feature_names)

        # 3. 排列重要性 (需要模型)
        perm_df = None
        if model is not None:
            try:
                perm_df = self.permutation_importance(model, X, y, feature_names)
            except (Exception) as e:
                logger.warning(f"[FeatureImportance] permutation importance failed: {e}")

        # 4. SHAP (需要模型)
        shap_df = None
        if model is not None and include_shap:
            shap_df = self.shap_analysis(model, X, feature_names)

        # ─── 合并排名 ───
        n_features = len(feature_names)
        report = pd.DataFrame({'feature': feature_names})

        # MI 排名 → 归一化得分
        mi_rank = mi_df.set_index('feature')['mi_score']
        mi_max = mi_rank.max() if mi_rank.max() > 0 else 1.0
        report['mi_score'] = report['feature'].map(mi_rank).fillna(0)
        report['mi_rank'] = report['mi_score'].rank(ascending=False)

        # 相关性排名
        corr_rank = corr_df.set_index('feature')['abs_corr']
        corr_max = corr_rank.max() if corr_rank.max() > 0 else 1.0
        report['corr_score'] = report['feature'].map(corr_rank).fillna(0)
        report['corr_rank'] = report['corr_score'].rank(ascending=False)

        # 排列重要性
        if perm_df is not None:
            perm_rank = perm_df.set_index('feature')['importance_mean']
            perm_max = perm_rank.max() if perm_rank.max() > 0 else 1.0
            report['perm_score'] = report['feature'].map(perm_rank).fillna(0)
            report['perm_rank'] = report['perm_score'].rank(ascending=False)
        else:
            report['perm_score'] = 0.0
            report['perm_rank'] = n_features

        # SHAP
        if shap_df is not None and not shap_df.empty:
            shap_rank = shap_df.set_index('feature')['shap_mean']
            shap_max = shap_rank.max() if shap_rank.max() > 0 else 1.0
            report['shap_score'] = report['feature'].map(shap_rank).fillna(0)
            report['shap_rank'] = report['shap_score'].rank(ascending=False)
        else:
            report['shap_score'] = 0.0
            report['shap_rank'] = n_features

        # 综合得分: 归一化后加权平均
        # MI权重0.3, 相关性0.2, 排列0.3, SHAP 0.2
        n_methods = 2 + (1 if perm_df is not None else 0) + (1 if shap_df is not None and not shap_df.empty else 0)

        composite = (
            report['mi_score'] / mi_max * 0.3
            + report['corr_score'] / corr_max * 0.2
        )
        if perm_df is not None:
            composite += report['perm_score'] / perm_max * 0.3
        if shap_df is not None and not shap_df.empty:
            composite += report['shap_score'] / shap_max * 0.2

        report['composite_score'] = composite
        report = report.sort_values('composite_score', ascending=False).reset_index(drop=True)
        report['overall_rank'] = range(1, len(report) + 1)

        return report

# ═══════════════════════════════════════════════════════════════════
#  PyTorch 模型包装器 (用于排列重要性)
# ═══════════════════════════════════════════════════════════════════

class _SklearnModelWrapper:
    """将 PyTorch 模型包装为 sklearn 兼容接口"""

    def __init__(self, model):
        self.model = model
        import torch
        self.torch = torch

    def predict_proba(self, X):
        self.model.eval()
        with self.torch.no_grad():
            X_t = self.torch.FloatTensor(X)
            if hasattr(self.model, 'forward_home_mask'):
                logits = self.model(X_t)
            else:
                logits = self.model(X_t)
            probs = self.torch.softmax(logits, dim=-1).numpy()
        return probs

    def predict(self, X):
        probs = self.predict_proba(X)
        return probs.argmax(axis=1)

# ═══════════════════════════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════════════════════════

def generate_rolling_features(
    db_path: str = 'data/football_data.db',
    config: Optional[RollingWindowConfig] = None,
) -> pd.DataFrame:
    """一键生成滚动窗口特征"""
    gen = RollingWindowFeatureGenerator(db_path=db_path, config=config)
    return gen.generate()

def analyze_feature_importance(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: Optional[List[str]] = None,
    model=None,
    include_shap: bool = False,
) -> pd.DataFrame:
    """一键特征重要性分析"""
    analyzer = FeatureImportanceAnalyzer()
    return analyzer.full_report(X, y, feature_names, model, include_shap)
