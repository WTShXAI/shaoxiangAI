"""
赔率逆向校准器 v1.0 — 从庄家赔率反向推导 xG / 贝叶斯 / 联赛平局先验参数
======================================================================

核心理念（赔率=加密协议）：
  庄家赔率 = 竞技模型输出 + 抽水 + 资金扰动
  逆向后提取纯竞技信号 → 作为监督标签校准自有 xG 与贝叶斯参数

三层校准：
  L1: xG 参数（α, β, H, S_league）→ 最小化 λ 偏差
  L2: 贝叶斯双层（联赛 D 先验 + γ 映射系数）→ 修正平局偏移
  L3: 全局 KL 正则 → 防止过拟合赔率盘口

用法：
    calibrator = OddsInverseCalibrator()
    result = calibrator.calibrate(
        db_path="data/football_data.db",
        train_years=(2012, 2022),  # 训练集
        val_years=(2023, 2025),    # 验证集
    )
    # result.xg_params → {alpha, beta, H, S_league}
    # result.bayes_params → {league_prior, gamma}
"""

import sqlite3
import re
import logging
import time
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict

import numpy as np

try:
    from scipy.optimize import minimize
    from scipy.special import gammaln
    _SCIPY = True
except ImportError:
    _SCIPY = False

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# 数据类
# ════════════════════════════════════════════════════════════════

@dataclass
class CalibrateResult:
    """校准输出"""
    xg_params: Dict[str, Any] = field(default_factory=dict)
    bayes_params: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, float] = field(default_factory=dict)
    train_loss: List[float] = field(default_factory=list)
    val_loss: List[float] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)

@dataclass
class MatchRecord:
    """单场比赛记录"""
    match_id: int
    home_team: str
    away_team: str
    league: str          # 清洗后的联赛名
    odds_h: float
    odds_d: float
    odds_a: float
    lam_h_book: float    # 庄家 λ_H
    lam_a_book: float    # 庄家 λ_A
    p_book: np.ndarray   # 庄家去偏概率 [H,D,A]
    home_score: float
    away_score: float
    result: str          # 'H'|'D'|'A'
    is_train: bool
    # P2 庄家风控溢价
    risk_tier: int = 0   # 0=常规 1=轻度风控 2=重度防线
    rp_max: float = 1.0  # 最大比分风控溢价
    rp_features: Dict = field(default_factory=dict)

# ════════════════════════════════════════════════════════════════
# 主校准器
# ════════════════════════════════════════════════════════════════

class OddsInverseCalibrator:
    """
    协同校准 xG + 贝叶斯参数

    参数:
      lambda_reg: KL 正则权重（防止过拟合赔率）
      max_iter: 优化最大迭代次数
      min_team_matches: 球队最低出场数
      verbose: 是否输出详细日志
    """

    def __init__(self,
                 lambda_reg: float = 0.2,
                 max_iter: int = 200,
                 min_team_matches: int = 10,
                 verbose: bool = True,
                 risk_premium_optimize: bool = True,
                 heavy_rp_threshold: float = 8.0,
                 apply_risk_weight_loss: bool = True):
        self.lambda_reg = lambda_reg
        self.max_iter = max_iter
        self.min_team_matches = min_team_matches
        self.verbose = verbose
        self._msg = lambda s: logger.info(s) if verbose else None

        # P2 庄家风控溢价
        self.risk_premium_optimize = risk_premium_optimize
        self.heavy_risk_threshold = heavy_rp_threshold
        self.apply_risk_weight_loss = apply_risk_weight_loss

        # 内部状态
        self.teams: List[str] = []
        self.team_idx: Dict[str, int] = {}
        self.leagues: List[str] = []
        self.league_idx: Dict[str, int] = {}
        self.matches: List[MatchRecord] = []

    # ── 联赛名清洗 ──

    @staticmethod
    def _clean_league(raw_league: str) -> str:
        """清洗联赛名：去掉赛季/轮次信息"""
        if not raw_league:
            return 'default'
        # 匹配类似 '15/16英超第20轮' → '英超'
        m = re.match(r'(?:\d{2}/\d{2})?(.+?)(?:第\d+轮|第\d+节|决赛|半决赛|半准决赛|分组赛|附加赛|季后赛)', raw_league)
        if m:
            base = m.group(1).strip()
            if len(base) >= 2:
                return base
        # 直接匹配纯联赛名
        base = raw_league.strip()
        return base if len(base) >= 2 else 'default'

    # ── 数据加载 ──

    def load_data(self, db_path: str,
                  train_start: str = '2012-01-01',
                  train_end: str = '2022-12-31',
                  val_start: str = '2023-01-01',
                  val_end: str = '2025-12-31',
                  max_samples: int = 200000,
                  fast_mode: bool = True) -> Tuple[int, int]:
        """
        加载历史比赛 + 赔率数据

        Args:
          fast_mode: True=比例法快速逆推(秒级), False=贝叶斯全逆推(分钟级)
        """
        self._msg(f"加载数据: {db_path} ({train_start}~{val_end})" +
                  (" [快速模式]" if fast_mode else " [贝叶斯模式]"))
        db = sqlite3.connect(db_path)

        query = """
        SELECT ext_id, home_team, away_team, league_name,
               match_date, odds_home, odds_draw, odds_away,
               home_score, away_score, final_result
        FROM training_extended
        WHERE odds_home > 1.0 AND odds_draw > 1.0 AND odds_away > 1.0
          AND odds_home < 30
          AND home_score IS NOT NULL
          AND match_date BETWEEN ? AND ?
        ORDER BY match_date
        """

        # 训练集
        train_rows = db.execute(query, (train_start, train_end)).fetchall()
        if max_samples and len(train_rows) > max_samples:
            indices = np.linspace(0, len(train_rows)-1, max_samples, dtype=int)
            train_rows = [train_rows[i] for i in indices]

        # 验证集 (限制样本量)
        val_rows = db.execute(query, (val_start, val_end)).fetchall()
        if len(val_rows) > 50000:
            indices = np.linspace(0, len(val_rows)-1, 50000, dtype=int)
            val_rows = [val_rows[i] for i in indices]

        db.close()

        self._msg(f"  训练: {len(train_rows)} 场, 验证: {len(val_rows)} 场")

        # 收集所有 team + league
        team_set = set()
        league_set = set()
        for row in train_rows + val_rows:
            team_set.add(row[1]); team_set.add(row[2])
            league_set.add(self._clean_league(row[3] or ''))

        self.teams = sorted(team_set)
        self.team_idx = {t: i for i, t in enumerate(self.teams)}
        self.leagues = sorted(league_set)
        self.league_idx = {l: i for i, l in enumerate(self.leagues)}

        self._msg(f"  球队: {len(self.teams)} 支, 联赛: {len(self.leagues)} 个")

        # 逆推庄家 λ 标签
        self._msg("  逆推庄家 λ 标签...")
        self.matches = []
        t0 = time.time()

        if fast_mode:
            self._load_fast(all_rows=[(r, True) for r in train_rows] + [(r, False) for r in val_rows], t0=t0)
        else:
            self._load_bayesian(all_rows=[(r, True) for r in train_rows] + [(r, False) for r in val_rows], t0=t0)

        n_train = sum(1 for m in self.matches if m.is_train)
        n_val = sum(1 for m in self.matches if not m.is_train)
        return n_train, n_val

    def _load_fast(self, all_rows: List[Tuple], t0: float):
        """快速模式: 比例法去抽水"""
        total = len(all_rows)
        for i, (row, is_train) in enumerate(all_rows):
            ext_id, ht, at, raw_league, mdate, oh, od, oa, hs, aws, result = row
            league = self._clean_league(raw_league or '')
            if ht not in self.team_idx or at not in self.team_idx:
                continue
            try:
                oh, od, oa = float(oh), float(od), float(oa)
                # 比例法去抽水
                raw_sum = 1/oh + 1/od + 1/oa
                p_book = np.array([1/(oh*raw_sum), 1/(od*raw_sum), 1/(oa*raw_sum)])
                # 估计 λ: 二分搜索
                lam_h, lam_a = self._solve_lambda_from_probs(p_book)
            except (ValueError, TypeError, ZeroDivisionError):
                continue

            match = MatchRecord(
                match_id=ext_id or i,
                home_team=ht, away_team=at, league=league,
                odds_h=oh, odds_d=od, odds_a=oa,
                lam_h_book=lam_h, lam_a_book=lam_a, p_book=p_book,
                home_score=float(hs or 0), away_score=float(aws or 0),
                result=result or 'H', is_train=is_train,
            )
            self._classify_risk(match)  # P2: RP分档
            self.matches.append(match)
            if (i+1) % 30000 == 0:
                self._msg(f"    {i+1}/{total} ({time.time()-t0:.0f}s)")

    def _load_bayesian(self, all_rows: List[Tuple], t0: float):
        """贝叶斯模式: 完整 BookmakerBayesInfer"""
        from bookmaker_sim.margin_likelihood_bridge import BookmakerBayesInfer
        infer = BookmakerBayesInfer()
        total = len(all_rows)
        for i, (row, is_train) in enumerate(all_rows):
            ext_id, ht, at, raw_league, mdate, oh, od, oa, hs, aws, result = row
            league = self._clean_league(raw_league or '')
            if ht not in self.team_idx or at not in self.team_idx:
                continue
            try:
                odds_1x2 = {'home': float(oh), 'draw': float(od), 'away': float(oa)}
                r = infer.infer_parameters(odds_1x2=odds_1x2, league_name=league)
                lam_h = float(r.posterior_lambda_h)
                lam_a = float(r.posterior_lambda_a)
                p_book = np.array([
                    r.posterior_probs.get('home', 1/3),
                    r.posterior_probs.get('draw', 1/3),
                    r.posterior_probs.get('away', 1/3),
                ])
            except (ValueError, TypeError, ZeroDivisionError):
                continue

            match = MatchRecord(
                match_id=ext_id or i,
                home_team=ht, away_team=at, league=league,
                odds_h=float(oh), odds_d=float(od), odds_a=float(oa),
                lam_h_book=lam_h, lam_a_book=lam_a, p_book=p_book,
                home_score=float(hs or 0), away_score=float(aws or 0),
                result=result or 'H', is_train=is_train,
            )
            self._classify_risk(match)  # P2: RP分档
            self.matches.append(match)
            if (i+1) % 10000 == 0:
                self._msg(f"    {i+1}/{total} ({time.time()-t0:.0f}s)")

    @staticmethod
    def _solve_lambda_from_probs(p_book: np.ndarray) -> Tuple[float, float]:
        """
        从去偏概率快速反推 λ_H, λ_A (二分搜索, 微秒级)
        联立: P_d = Σ_k Pois(k|λ_H)·Pois(k|λ_A)
              P_h/(P_h+P_a) ≈ share_H (简化)
        """
        p_h, p_d, p_a = p_book[0], p_book[1], p_book[2]
        # share = 主队获胜者在非平局中的份额
        share_h = p_h / max(p_h + p_a, 0.01)
        total = share_h * 2.0 + (1 - share_h) * 2.0  # ~2.7 typical
        # 二分搜索 λ_H+λ_A = total 使泊松平局概率等于 p_d
        target = p_d
        lo, hi = 0.3, 8.0
        best_t, best_err = 2.7, 1.0
        for _ in range(20):
            mid = (lo + hi) / 2
            lh, la = mid * share_h, mid * (1 - share_h)
            d_pred = OddsInverseCalibrator._poisson_draw_prob(lh, la)
            err = abs(d_pred - target)
            if err < best_err:
                best_err, best_t = err, mid
            if d_pred < target:
                lo = mid
            else:
                hi = mid
        lam_h = max(best_t * share_h, 0.1)
        lam_a = max(best_t * (1 - share_h), 0.1)
        return lam_h, lam_a

    @staticmethod
    def _poisson_draw_prob(lam_h: float, lam_a: float, max_g: int = 12) -> float:
        """纯泊松平局概率"""
        import math
        ph = np.array([max(np.exp(-lam_h) * lam_h**k / math.factorial(k), 1e-30) for k in range(max_g+1)])
        pa = np.array([max(np.exp(-lam_a) * lam_a**k / math.factorial(k), 1e-30) for k in range(max_g+1)])
        ph /= ph.sum(); pa /= pa.sum()
        return float(sum(ph[k] * pa[k] for k in range(max_g+1)))

    # ════════════════════════════════════════════════
    # 机构真实比分赔率计算（多机构融合 → 去抽水 → 公允赔率）
    # ════════════════════════════════════════════════

    @staticmethod
    def calc_fair_score_odds(
        book_correct_score_odds_list: List[float],
        win_draw_loss_odds_list: List[Tuple[float, float, float]],
    ) -> Tuple[float, float, Dict]:
        """
        三步得到市场公允真实比分赔率:

        Step1: 多机构1X2返还率均值 R̄ = mean(1/M_bk)
        Step2: 比分赔率中位数 O_med (抗极端值)
        Step3: Odds_market = O_med / R̄ (剥离抽水)

        Args:
          book_correct_score_odds_list: 同比分多家机构原始赔率 [54,56,58,...]
          win_draw_loss_odds_list: 对应机构1X2赔率 [(O_h,O_d,O_a),...]

        Returns:
          (odds_market, cv_score, diagnostics)
        """
        if not book_correct_score_odds_list or not win_draw_loss_odds_list:
            return 0.0, 0.0, {'error': 'empty_input'}

        # 1. 每家机构返还率
        r_list = []
        for oh, od, oa in win_draw_loss_odds_list:
            margin = 1.0 / oh + 1.0 / od + 1.0 / oa
            r_list.append(1.0 / margin)
        avg_return = float(np.mean(r_list))

        # 2. 多机构中位数 (抗极端高赔干扰)
        odds_arr = np.array(book_correct_score_odds_list, dtype=float)
        o_med = float(np.median(odds_arr))

        # 3. 离散系数 (风控判定用)
        cv_score = float(np.std(odds_arr)) / max(o_med, 0.01)

        # 4. 剥离抽水 → 公允真实比分赔率
        odds_market = o_med / max(avg_return, 0.01)

        diagnostics = {
            'o_median': o_med,
            'avg_return': avg_return,
            'cv_score': cv_score,
            'n_bookmakers': len(book_correct_score_odds_list),
            'single_trap_suspect': cv_score > 0.3,
        }
        return odds_market, cv_score, diagnostics

    # ════════════════════════════════════════════════
    # 庄家风控防线识别体系 (四层联合判定)
    # ════════════════════════════════════════════════

    @staticmethod
    def calc_risk_premium(match: MatchRecord,
                           max_g: int = 6,
                           score_odds_real: Dict[str, List[float]] = None,
                           overround: float = None) -> Dict:
        """
        第一层: 风控溢价指数 RP = odds_real / odds_theo

        真实赔率源: score_odds_real[{score}] = [odd_bet365, odd_interwetten, ...]
        当无外部数据时(RP≈1.0), 仅做框架就绪标记

        Returns: {score: rp_value, ...}
        """
        import math
        lam_h, lam_a = match.lam_h_book, match.lam_a_book
        if overround is None:
            oh, od, oa = match.odds_h, match.odds_d, match.odds_a
            raw_sum = 1/oh + 1/od + 1/oa
            overround = raw_sum - 1.0

        ph = np.array([max(np.exp(-lam_h) * lam_h**k / math.factorial(k), 1e-30)
                       for k in range(max_g + 1)])
        pa = np.array([max(np.exp(-lam_a) * lam_a**k / math.factorial(k), 1e-30)
                       for k in range(max_g + 1)])
        ph /= ph.sum(); pa /= pa.sum()
        V = 1.0 + overround

        rp = {}
        for gh in range(max_g + 1):
            for ga in range(max_g + 1):
                key = f"{gh}-{ga}"
                p_theo = ph[gh] * pa[ga]
                odds_theo = 1.0 / max(p_theo * V, 1e-8)

                # 有外部真实比分赔率 → RP = mean(real) / theo
                ext = (score_odds_real or {}).get(key)
                if ext and len(ext) > 0:
                    odds_real = float(np.mean(ext))
                    rp_val = odds_real / max(odds_theo, 1.01)
                else:
                    # 无数据时 RP≈1 (就绪标记)
                    rp_val = 1.0

                rp[key] = float(np.clip(rp_val, 0.5, 200.0))
        return rp

    @staticmethod
    def detect_risk_barrier(
        score_odds_raw: Dict[str, List[float]],
        theo_score_odds: Dict[str, float],
        wdl_odds_by_bookmaker: List[Tuple[float, float, float]] = None,
        league: str = '',
        is_final_tournament: bool = False,
        is_heavy_favorite: bool = False,
        heavy_risk_threshold: float = 8.0,
    ) -> Tuple[bool, float, Dict]:
        """
        四层联合判定: 某比分是否为庄家风控防线

        层1 (RP指数): RP = Odds_market / Odds_theo
                      Odds_market = median(raw) / R̄ (去抽水多机构公允赔率)
        层2 (离散度): CV = std(raw) / median(raw) < 0.2
        层3 (凯利值): Kelly = 1/(Odds_market × P_theo) < 0.85
        层4 (场景规则): 决赛 / 豪门大比分 RP>6 放宽

        判定: 基础条件A(RP超阈值) + (校验B1或B2) 或 A+场景规则
        """
        diagnostics = {'trigger_score': None, 'max_rp': 1.0, 'cv': None,
                       'kelly': None, 'reason': 'none'}
        max_rp = 1.0
        threshold = 6.0 if (is_final_tournament and is_heavy_favorite) else heavy_risk_threshold

        # 计算机构返还率均值 (去抽水)
        avg_return = 1.0
        if wdl_odds_by_bookmaker and len(wdl_odds_by_bookmaker) >= 3:
            r_list = [1.0 / (1/oh + 1/od + 1/oa) for oh, od, oa in wdl_odds_by_bookmaker]
            avg_return = float(np.mean(r_list))

        for score_key, raw_odds_list in score_odds_raw.items():
            theo_odd = theo_score_odds.get(score_key, 100.0)
            if not raw_odds_list or theo_odd <= 1.0 or len(raw_odds_list) < 3:
                continue

            raw_arr = np.array(raw_odds_list, dtype=float)
            o_med = float(np.median(raw_arr))
            cv_score = float(np.std(raw_arr)) / max(o_med, 0.01)

            # 去抽水 → 市场公允真实比分赔率: Odds_market = median(raw) / R̄
            odds_market = o_med / max(avg_return, 0.01)
            rp = odds_market / max(theo_odd, 1.01)
            if rp > max_rp:
                max_rp = rp

            if rp <= threshold:
                continue

            # 凯利: 用公允赔率
            p_theo = 1.0 / (theo_odd * max(avg_return, 0.01))
            kelly = 1.0 / max(odds_market * p_theo, 1e-8)
            kelly = float(np.clip(kelly, 0.0, 5.0))

            b1_pass = cv_score < 0.2
            b2_pass = kelly < 0.85
            scene_pass = is_final_tournament and is_heavy_favorite and rp > 6.0

            if (b1_pass or b2_pass) or scene_pass:
                diagnostics.update({
                    'trigger_score': score_key, 'max_rp': max_rp,
                    'cv': round(cv_score, 4), 'kelly': round(kelly, 4),
                    'o_market': round(odds_market, 1),
                    'reason': f"{'scene' if scene_pass else 'cv' if b1_pass else 'kelly'}_rp={rp:.1f}",
                })
                return True, max_rp, diagnostics

        diagnostics['max_rp'] = max_rp
        return False, max_rp, diagnostics

    def _classify_risk(self, match: MatchRecord,
                        score_odds_real: Dict = None,
                        is_final: bool = False,
                        is_heavy_fav: bool = False) -> MatchRecord:
        """
        按四层判定体系分档:
          0 = 常规竞技盘 (RP ≤ 3)
          1 = 轻度风控 (3 < RP ≤ threshold, 未通过四层联合判定)
          2 = 重度风控防线 (通过四层联合判定 + RP > 8, 或场景规则 RP > 6)
        """
        if not self.risk_premium_optimize:
            match.risk_tier = 0
            match.rp_max = 1.0
            return match

        # 理论比分赔率
        theo_score_odds = {}
        import math
        lam_h, lam_a = match.lam_h_book, match.lam_a_book
        oh, od, oa = match.odds_h, match.odds_d, match.odds_a
        overround = 1/oh + 1/od + 1/oa - 1.0
        V = 1.0 + overround
        ph_arr = np.array([max(np.exp(-lam_h) * lam_h**k / math.factorial(k), 1e-30)
                           for k in range(7)])
        pa_arr = np.array([max(np.exp(-lam_a) * lam_a**k / math.factorial(k), 1e-30)
                           for k in range(7)])
        ph_arr /= ph_arr.sum(); pa_arr /= pa_arr.sum()
        for gh in range(7):
            for ga in range(7):
                key = f"{gh}-{ga}"
                p_theo = ph_arr[gh] * pa_arr[ga]
                theo_score_odds[key] = 1.0 / max(p_theo * V, 1e-8)

        # 真实比分赔率 (从 betting_markets 获取，或空)
        score_odds_real = score_odds_real or {}

        if score_odds_real:
            is_barrier, max_rp, diag = self.detect_risk_barrier(
                score_odds_real, theo_score_odds,
                league=match.league,
                is_final_tournament=is_final,
                is_heavy_favorite=is_heavy_fav,
                heavy_risk_threshold=self.heavy_risk_threshold,
            )
            match.rp_max = max_rp
            match.rp_features = {'rp_max': float(max_rp), 'is_barrier': is_barrier,
                                 'trigger_score': diag.get('trigger_score'),
                                 'cv': diag.get('cv'), 'kelly': diag.get('kelly')}
            match.risk_tier = 2 if is_barrier else (1 if max_rp > 3.0 else 0)
        else:
            # 无真实比分赔率 → 用理论简化判定
            rp_map = OddsInverseCalibrator.calc_risk_premium(match)
            max_rp = max(rp_map.values()) if rp_map else 1.0
            match.rp_max = max_rp
            if max_rp <= 3.0:
                match.risk_tier = 0
            elif max_rp <= self.heavy_risk_threshold:
                match.risk_tier = 1
            else:
                match.risk_tier = 2
            rp_sorted = sorted(rp_map.values(), reverse=True)
            match.rp_features = {
                'rp_max': float(max_rp),
                'rp_top3_mean': float(np.mean(rp_sorted[:3])) if len(rp_sorted) >= 3 else max_rp,
                'rp_count_gt_3': int(sum(1 for v in rp_map.values() if v > 3)),
                'rp_count_gt_8': int(sum(1 for v in rp_map.values() if v > self.heavy_risk_threshold)),
                'risk_tier': match.risk_tier, 'is_barrier': match.risk_tier == 2,
            }

        return match

    def _compute_sample_weight(self, match: MatchRecord) -> float:
        """风控样本权重: w = 1/RP_max (重度防线→权重趋近0)"""
        if not self.apply_risk_weight_loss:
            return 1.0
        if match.risk_tier == 0:
            return 1.0
        if match.risk_tier == 1:
            return float(np.clip(1.0 / match.rp_max, 0.2, 1.0))
        # risk_tier == 2: 不参与全局参数优化
        return 0.0

    @staticmethod
    def generate_rp_features(match: MatchRecord,
                              is_final_tournament: bool = False) -> Dict[str, float]:
        """
        生成单场 RP 衍生特征向量 (供 Stacking 元模型使用)

        特征清单:
          max_rp, rp_bin, tournament_final_rp,
          rp_draw_bias, top3_rp_avg, has_risk_barrier
        """
        rf = match.rp_features or {}
        rp_max = match.rp_max
        rp_bin = match.risk_tier  # 0=常规 1=轻度 2=重度

        # 决赛专项特征
        tournament_final_rp = 1.0 if (is_final_tournament and rp_bin == 2) else 0.0

        # 平局比分平均RP (估算: 0-0, 1-1, 2-2)
        draw_rp_avg = rf.get('rp_draw_avg', rp_max)

        # top3 冷门比分RP均值
        top3_avg = rf.get('rp_top3_mean', rp_max)

        return {
            'max_rp': float(rp_max),
            'rp_bin': float(rp_bin),
            'tournament_final_rp': tournament_final_rp,
            'rp_draw_bias': float(draw_rp_avg),
            'top3_rp_avg': float(top3_avg),
            'has_risk_barrier': float(rf.get('is_barrier', False)),
        }

    @staticmethod
    def top_scores(match: MatchRecord,
                   lam_h: float = None, lam_a: float = None,
                   display_n: int = 4, store_n: int = 10,
                   min_display_prob: float = 0.03,
                   final_min_prob: float = 0.015,
                   force_risk: bool = True,
                   is_final: bool = False,
                   max_g: int = 6) -> Tuple[List[Dict], List[Dict]]:
        """
        输出比分概率矩阵，分两层：
          display_scores: Top N 展示比分 (含RP/防线标记)
          store_scores:   Top M 留存比分 (完整概率矩阵)

        自适应冷门纳入:
          存在风控防线 → 强制纳入1个高赔冷门比分
          无防线      → 仅输出主流比分
        """
        import math
        lh = lam_h if lam_h is not None else match.lam_h_book
        la = lam_a if lam_a is not None else match.lam_a_book

        # 泊松比分概率矩阵
        ph = np.array([max(np.exp(-lh)*lh**k/math.factorial(k), 1e-30) for k in range(max_g+1)])
        pa = np.array([max(np.exp(-la)*la**k/math.factorial(k), 1e-30) for k in range(max_g+1)])
        ph /= ph.sum(); pa /= pa.sum()

        scores = []
        for gh in range(max_g+1):
            for ga in range(max_g+1):
                p = float(ph[gh] * pa[ga])
                if p < 0.005:  # 过滤 <0.5% 极低概率
                    continue
                outcome = 'H' if gh > ga else ('D' if gh == ga else 'A')
                rp_val = match.rp_features.get(f'rp_{gh}_{ga}', None) if match.rp_features else None
                scores.append({
                    'score': f'{gh}-{ga}', 'gh': gh, 'ga': ga,
                    'prob': round(p, 4), 'outcome': outcome,
                    'rp': round(rp_val, 2) if rp_val else None,
                    'is_barrier': (rp_val is not None and rp_val > 8.0) or
                                  (is_final and rp_val is not None and rp_val > 6.0),
                    'is_cold': gh + ga >= 3 and abs(gh - ga) >= 2,  # 净胜≥2的大比分
                })

        scores.sort(key=lambda s: s['prob'], reverse=True)

        # 展示层 Top N
        min_prob = final_min_prob if is_final else min_display_prob
        has_barrier = match.risk_tier == 2 or match.rp_features.get('is_barrier', False)

        display = []
        cold_added = False
        for s in scores:
            if len(display) >= display_n:
                break
            if s['prob'] < min_prob:
                continue
            display.append(s)
            if s['is_cold'] or s['is_barrier']:
                cold_added = True

        # 强制纳入风控冷门
        if force_risk and has_barrier and not cold_added and len(display) < display_n:
            cold_candidates = [s for s in scores if s['is_cold'] and s['prob'] >= final_min_prob]
            if cold_candidates:
                display.append(cold_candidates[0])

        # 留存层 Top M
        store = scores[:store_n]

        return display, store

    def _apply_xg_rp_offset(self, lam_h: float, lam_a: float,
                              rp_max: float) -> Tuple[float, float]:
        """
        xG 偏移修正: λ_final = λ_cal × (1 - 0.05 × min(RP/8, 1))

        RP 越高 → λ 小幅下压，抵消庄家刻意放大冷门赔率造成的概率虚低
        """
        factor = 1.0 - 0.05 * min(rp_max / 8.0, 1.0)
        return lam_h * factor, lam_a * factor

    # ── L1: xG 参数优化 ──

    def _init_xg_params(self, train_only: bool = True) -> Dict:
        """初始化 xG 参数（α, β, H, S_league）"""
        matches = [m for m in self.matches if (not train_only or m.is_train)]
        n_teams = len(self.teams)
        n_leagues = len(self.leagues)

        # 从历史数据初始化 α, β
        team_home_goals = defaultdict(lambda: [0.0, 0])   # [sum, count]
        team_away_goals = defaultdict(lambda: [0.0, 0])
        team_home_conceded = defaultdict(lambda: [0.0, 0])
        team_away_conceded = defaultdict(lambda: [0.0, 0])

        for m in matches:
            ht, at = m.home_team, m.away_team
            team_home_goals[ht][0] += m.home_score; team_home_goals[ht][1] += 1
            team_home_conceded[ht][0] += m.away_score; team_home_conceded[ht][1] += 1
            team_away_goals[at][0] += m.away_score; team_away_goals[at][1] += 1
            team_away_conceded[at][0] += m.home_score; team_away_conceded[at][1] += 1

        # 全局平均
        all_home_goals = np.mean([v[0]/max(v[1],1) for v in team_home_goals.values() if v[1]>0])
        all_away_goals = np.mean([v[0]/max(v[1],1) for v in team_away_goals.values() if v[1]>0])

        alpha_raw = np.zeros(n_teams)
        beta_raw = np.zeros(n_teams)
        for i, team in enumerate(self.teams):
            hg = team_home_goals[team]
            ag = team_away_goals[team]
            hc = team_home_conceded[team]
            ac = team_away_conceded[team]
            # 进攻 = log(进球/全局平均), 防守 = log(全局平均/失球)
            attack = np.log(max(hg[0]/max(hg[1],1), 0.3) / max(all_home_goals, 0.5))
            defense = np.log(max(all_home_goals, 0.5) / max(hc[0]/max(hc[1],1), 0.3))
            alpha_raw[i] = np.clip(attack, -1.5, 1.5)
            beta_raw[i] = np.clip(defense, -1.5, 1.5)

        # 联赛缩放 S_league, 全局主场 H
        H_global = max(all_home_goals - all_away_goals, 0.05)
        S_league = np.zeros(n_leagues)

        self._msg(f"  初始化: α∈[{alpha_raw.min():.2f},{alpha_raw.max():.2f}] "
                  f"β∈[{beta_raw.min():.2f},{beta_raw.max():.2f}] H={H_global:.3f}")

        return {
            'alpha': alpha_raw, 'beta': beta_raw,
            'H_global': H_global, 'S_league': S_league,
        }

    def _assemble_params(self, params: Dict) -> np.ndarray:
        """组装优化参数向量"""
        return np.concatenate([
            params['alpha'],
            params['beta'],
            np.array([params['H_global']]),
            params['S_league'],
        ])

    def _disassemble_params(self, theta: np.ndarray) -> Dict:
        """拆解优化参数向量"""
        n_teams = len(self.teams)
        n_leagues = len(self.leagues)
        return {
            'alpha': theta[:n_teams],
            'beta': theta[n_teams:2*n_teams],
            'H_global': theta[2*n_teams],
            'S_league': theta[2*n_teams+1:2*n_teams+1+n_leagues],
        }

    def _xfg_loss(self, theta: np.ndarray,
                   matches: List[MatchRecord],
                   penalty_strength: float = 0.01) -> float:
        """xG 损失：λ 偏差 + KL 散度 + L2 正则 + 风控样本加权衰减"""
        p = self._disassemble_params(theta)
        alpha, beta = p['alpha'], p['beta']
        H = p['H_global']
        S = p['S_league']

        xg_loss = 0.0
        kl_loss = 0.0
        n_eff = 0.0

        for m in matches:
            hi = self.team_idx.get(m.home_team)
            ai = self.team_idx.get(m.away_team)
            li = self.league_idx.get(m.league)
            if hi is None or ai is None or li is None:
                continue

            # 风控样本权重: w=1/RP (重度防线→0, 不参与全局优化)
            w = self._compute_sample_weight(m)
            if w <= 0.0:
                continue

            # xG 公式: ln(λ) = α_h - β_a + H + S_league
            log_lam_h = alpha[hi] - beta[ai] + H + (S[li] if li < len(S) else 0)
            log_lam_a = alpha[ai] - beta[hi] + (S[li] if li < len(S) else 0)

            lam_h = max(np.exp(log_lam_h), 0.05)
            lam_a = max(np.exp(log_lam_a), 0.05)

            # L_xg: λ 偏差 (加权)
            xg_loss += w * ((lam_h - m.lam_h_book)**2 + (lam_a - m.lam_a_book)**2)

            # L_kl: 概率分布 KL (加权)
            p_self = self._poisson_1x2_probs(lam_h, lam_a)
            kl_loss += w * self._kl_div(p_self, m.p_book)
            n_eff += w

        n_eff = max(n_eff, 1.0)
        xg_loss /= n_eff
        kl_loss /= n_eff

        # L2 正则
        l2_reg = penalty_strength * (
            np.sum(alpha**2) + np.sum(beta**2) +
            np.sum(S**2) + H**2
        )

        return xg_loss + self.lambda_reg * kl_loss + l2_reg

    @staticmethod
    def _poisson_1x2_probs(lam_h: float, lam_a: float, max_g: int = 12) -> np.ndarray:
        """单场泊松 → 1X2 概率向量"""
        import math
        ph = np.array([max(np.exp(-lam_h) * lam_h**k / math.factorial(k), 1e-30)
                        for k in range(max_g + 1)])
        pa = np.array([max(np.exp(-lam_a) * lam_a**k / __import__('math').factorial(k), 1e-30)
                       for k in range(max_g + 1)])
        ph /= ph.sum(); pa /= pa.sum()

        p_h = sum(ph[i] * sum(pa[:i]) for i in range(1, max_g+1))
        p_d = sum(ph[i] * pa[i] for i in range(max_g+1))
        p_a = sum(pa[i] * sum(ph[:i]) for i in range(1, max_g+1))

        total = p_h + p_d + p_a
        return np.array([p_h, p_d, p_a]) / max(total, 1e-10)

    @staticmethod
    def _kl_div(p: np.ndarray, q: np.ndarray) -> float:
        """KL 散度 D_KL(p||q)"""
        eps = 1e-10
        p = np.clip(p, eps, 1 - eps)
        q = np.clip(q, eps, 1 - eps)
        return float(np.sum(p * np.log(p / q)))

    def calibrate_xg(self,
                     train_only: bool = True,
                     learning_rate: float = 0.02,
                     use_adam: bool = True) -> Dict:
        """
        梯度下降优化 xG 参数

        优化:
           min_α,β,H,S  L_xg + λ_reg * L_kl + L2_regularization
        """
        if not _SCIPY:
            self._msg("⚠ scipy 不可用, 使用手动 SGD")
            use_adam = False

        params = self._init_xg_params(train_only)
        matches = [m for m in self.matches if (not train_only or m.is_train)]
        if len(matches) < 100:
            self._msg("⚠ 训练场次不足 ({len(matches)}), 跳过 xG 校准")
            return params

        self._msg(f"  优化 xG ({len(matches)} 场, λ_reg={self.lambda_reg})")

        theta0 = self._assemble_params(params)
        n_teams = len(self.teams)

        # 约束: α, β ∈ [-2, 2], H ∈ [0, 1], S ∈ [-1, 1]
        bounds = (
            [(-2.0, 2.0)] * n_teams +          # alpha
            [(-2.0, 2.0)] * n_teams +          # beta
            [(0.0, 1.0)] +                      # H_global
            [(-1.0, 1.0)] * len(self.leagues)   # S_league
        )

        train_losses = []
        val_losses = []

        # L-BFGS-B 优化
        result = minimize(
            lambda t: self._xfg_loss(t, matches),
            theta0,
            method='L-BFGS-B',
            bounds=bounds,
            options={'maxiter': self.max_iter, 'disp': False},
            callback=lambda x: train_losses.append(self._xfg_loss(x, matches)),
        )

        calibrated = self._disassemble_params(result.x)

        self._msg(f"  完成: loss={result.fun:.4f}  nfev={result.nfev}  status={result.message}")

        return {
            'alpha': calibrated['alpha'],
            'beta': calibrated['beta'],
            'H_global': float(calibrated['H_global']),
            'S_league': calibrated['S_league'],
            'optim_result': result,
            'train_losses': train_losses,
        }

    # ── L2: 贝叶斯先验 + γ 校准 ──

    def calibrate_bayes(self, xg_params: Dict) -> Dict:
        """
        校准贝叶斯双层参数（含风控溢价加权）：
          模块A: 联赛平局 Beta 先验 (weight = 1/RP)
          模块B: γ 映射系数（常规盘 / 冷门盘分离）
          模块C: 冷门专属 γ_risk (RP>8 时专用)
        """
        self._msg("  校准贝叶斯参数...")

        # ── 模块A: 联赛平局先验 (加权) ──
        league_d_book = defaultdict(list)
        league_d_weights = defaultdict(list)

        for m in self.matches:
            w = self._compute_sample_weight(m)
            if w <= 0.0:  # 重度防线不参与先验拟合
                continue
            league_d_book[m.league].append(m.p_book[1])
            league_d_weights[m.league].append(w)

        league_prior = {}
        for league, d_list in league_d_book.items():
            if len(d_list) < 20:
                continue
            w_arr = np.array(league_d_weights[league])
            w_arr = w_arr / w_arr.sum()  # 归一化
            # 加权期望
            mu_book = float(np.average(d_list, weights=w_arr))
            # 有效样本数 = (Σw)^2 / Σw^2
            n_eff = min(float(np.sum(w_arr)**2 / max(np.sum(w_arr**2), 1e-6)), 100)
            a = max(mu_book * n_eff, 0.5)
            b = max((1 - mu_book) * n_eff, 0.5)
            league_prior[league] = {'a': float(a), 'b': float(b), 'mu_book': float(mu_book)}

        # 全局默认
        all_d = []
        all_w = []
        for d_list, w_list in zip(league_d_book.values(), league_d_weights.values()):
            all_d.extend(d_list); all_w.extend(w_list)
        if all_d:
            w_arr = np.array(all_w) / max(sum(all_w), 1e-6)
            global_mu = float(np.average(all_d, weights=w_arr))
            global_eff = min(float(np.sum(w_arr)**2 / max(np.sum(w_arr**2), 1e-6)), 200)
        else:
            global_mu, global_eff = 0.25, 50
        global_prior = {'a': global_mu * global_eff, 'b': (1 - global_mu) * global_eff}

        # ── 模块B: γ 映射系数 (仅常规盘训练) ──
        league_gamma = {}
        league_gamma_risk = {}  # 冷门专属 γ
        for league in self.leagues:
            # 分离常规盘 / 冷门盘
            normal_matches = [m for m in self.matches
                              if m.league == league and m.risk_tier <= 1]
            risk_matches = [m for m in self.matches
                            if m.league == league and m.risk_tier == 2]

            if len(normal_matches) >= 30:
                hi_arr = [self.team_idx.get(m.home_team, 0) for m in normal_matches]
                ai_arr = [self.team_idx.get(m.away_team, 0) for m in normal_matches]
                gamma_best = self._optimize_gamma(xg_params, normal_matches, hi_arr, ai_arr, league)
                league_gamma[league] = float(gamma_best)

            if len(risk_matches) >= 10:
                hi_arr = [self.team_idx.get(m.home_team, 0) for m in risk_matches]
                ai_arr = [self.team_idx.get(m.away_team, 0) for m in risk_matches]
                gamma_risk = self._optimize_gamma(xg_params, risk_matches, hi_arr, ai_arr, league)
                league_gamma_risk[league] = float(gamma_risk)

        # 全局 γ
        all_gamma = list(league_gamma.values())
        global_gamma = float(np.median(all_gamma)) if all_gamma else 1.0
        all_gr = list(league_gamma_risk.values())
        global_gamma_risk = float(np.median(all_gr)) if all_gr else global_gamma

        self._msg(
            f"  联赛先验: {len(league_prior)} 个 (加权), "
            f"γ: {len(league_gamma)} 个 (常规), "
            f"γ_risk: {len(league_gamma_risk)} 个 (冷门), "
            f"全局γ={global_gamma:.3f}"
        )

        return {
            'league_prior': league_prior,
            'global_prior': global_prior,
            'league_gamma': league_gamma,
            'global_gamma': global_gamma,
            'league_gamma_risk': league_gamma_risk,
            'global_gamma_risk': global_gamma_risk,
        }

    def _optimize_gamma(self, xg_params, matches, hi_arr, ai_arr, league) -> float:
        """单联赛 γ 优化：最小化 |P_d(xG)^γ - P_d(book)|"""
        alpha = xg_params['alpha']
        beta = xg_params['beta']
        H = xg_params['H_global']
        li = self.league_idx.get(league, 0)
        S = xg_params['S_league'][li] if li < len(xg_params['S_league']) else 0.0

        d_book = np.array([m.p_book[1] for m in matches])
        d_self = np.zeros_like(d_book)

        for j, m in enumerate(matches):
            hi, ai = hi_arr[j], ai_arr[j]
            lam_h = np.exp(alpha[hi] - beta[ai] + H + S)
            lam_a = np.exp(alpha[ai] - beta[hi] + S)
            probs = self._poisson_1x2_probs(lam_h, lam_a)
            d_self[j] = probs[1]

        # 网格搜索最优 γ
        best_gamma = 1.0
        best_loss = float('inf')
        for gamma in np.linspace(0.3, 3.0, 55):
            d_calib = np.clip(d_self, 1e-10, 0.99) ** gamma
            loss = np.mean(np.abs(d_calib - d_book))
            if loss < best_loss:
                best_loss = loss
                best_gamma = gamma
        return best_gamma

    # ── 完整校准流程 ──

    def calibrate(self, db_path: str,
                  train_start: str = '2012-01-01',
                  train_end: str = '2022-12-31',
                  val_start: str = '2023-01-01',
                  val_end: str = '2025-12-31',
                  max_samples: int = 200000,
                  fast_mode: bool = True,
                  ) -> CalibrateResult:
        """
        完整三日历通道校准

        Returns:
          CalibrateResult:
            .xg_params   → {alpha, beta, H_global, S_league, train_losses}
            .bayes_params → {league_prior, global_prior, league_gamma, global_gamma}
            .metrics      → 校准指标
        """
        result = CalibrateResult()

        # Step 1: 数据加载 + 庄家 λ 标签
        n_train, n_val = self.load_data(
            db_path, train_start, train_end, val_start, val_end, max_samples,
            fast_mode=fast_mode,
        )
        result.messages.append(f"加载: 训练 {n_train} 场, 验证 {n_val} 场")

        # P2: 风控分层统计
        tiers = [0, 0, 0]
        for m in self.matches:
            if m.risk_tier < 3:
                tiers[m.risk_tier] += 1
        result.messages.append(
            f"风控分层: 常规={tiers[0]} 轻度={tiers[1]} 重度防线={tiers[2]}"
        )

        # Step 2: xG 参数优化
        xg_result = self.calibrate_xg(train_only=True)
        result.xg_params = {
            'alpha': xg_result['alpha'],
            'beta': xg_result['beta'],
            'H_global': xg_result['H_global'],
            'S_league': xg_result['S_league'],
            'team_names': self.teams,
            'league_names': self.leagues,
        }
        result.train_loss = xg_result.get('train_losses', [])
        result.messages.append(
            f"xG 校准: final_loss={xg_result.get('optim_result',{}).get('fun','N/A')}"
        )

        # Step 3: 贝叶斯校准
        bayes_result = self.calibrate_bayes(xg_result)
        result.bayes_params = bayes_result
        result.messages.append(
            f"贝叶斯: {len(bayes_result['league_prior'])} 联赛先验, "
            f"γ={bayes_result['global_gamma']:.3f}"
        )

        # Step 4: 验证集评估 + 分层误差监控
        val_matches = [m for m in self.matches if not m.is_train]
        if val_matches:
            # 全局 loss
            val_loss = self._xfg_loss(
                self._assemble_params(xg_result), val_matches, penalty_strength=0.0
            )
            result.metrics['val_loss'] = float(val_loss)

            # 分层误差 (常规 / 轻度 / 重度)
            for tier_name, tier_val in [('normal', 0), ('mild', 1), ('heavy', 2)]:
                tier_matches = [m for m in val_matches if m.risk_tier == tier_val]
                if len(tier_matches) >= 10:
                    tier_loss = self._xfg_loss(
                        self._assemble_params(xg_result), tier_matches, penalty_strength=0.0
                    )
                    result.metrics[f'val_loss_{tier_name}'] = float(tier_loss)

            result.messages.append(
                f"验证集 loss: {val_loss:.4f} "
                f"(常规={result.metrics.get('val_loss_normal','?'):.4f} "
                f"轻度={result.metrics.get('val_loss_mild','?'):.4f} "
                f"重度={result.metrics.get('val_loss_heavy','?'):.4f})"
            )

        return result

    # ── 预测 lambda ──

    def predict_lambda(self, home_team: str, away_team: str, league: str,
                         rp_max: float = 1.0) -> Tuple[float, float]:
        """用校准后的 xG 参数预测某场比赛的 λ (含 RP 偏移修正)"""
        if not self.matches:  # 尚未校准
            return 1.3, 1.1
        alpha = getattr(self, '_calibrated_alpha', None) or self._init_xg_params()['alpha']
        beta = getattr(self, '_calibrated_beta', None) or self._init_xg_params()['beta']
        H = getattr(self, '_calibrated_H', 0.15)
        S = getattr(self, '_calibrated_S', np.zeros(len(self.leagues)))

        hi = self.team_idx.get(home_team, 0)
        ai = self.team_idx.get(away_team, 0)
        li = self.league_idx.get(league, 0)

        lam_h = np.exp(alpha[hi] - beta[ai] + H + (S[li] if li < len(S) else 0))
        lam_a = np.exp(alpha[ai] - beta[hi] + (S[li] if li < len(S) else 0))

        # P2: xG 偏移修正 (RP 高 → 小幅下压 λ)
        if rp_max > 1.0:
            lam_h, lam_a = self._apply_xg_rp_offset(lam_h, lam_a, rp_max)

        return max(lam_h, 0.1), max(lam_a, 0.1)

    def predict_proba(self, home_team: str, away_team: str, league: str) -> Dict[str, float]:
        """校准后的 1X2 概率"""
        lam_h, lam_a = self.predict_lambda(home_team, away_team, league)
        probs = self._poisson_1x2_probs(lam_h, lam_a)
        return {'home': float(probs[0]), 'draw': float(probs[1]), 'away': float(probs[2])}

    def save(self, path: str):
        """保存校准结果"""
        import joblib
        joblib.dump({
            'xg_params': {
                'alpha': getattr(self, '_calibrated_alpha', None),
                'beta': getattr(self, '_calibrated_beta', None),
                'H_global': getattr(self, '_calibrated_H', None),
                'S_league': getattr(self, '_calibrated_S', None),
                'teams': self.teams,
                'team_idx': self.team_idx,
                'leagues': self.leagues,
                'league_idx': self.league_idx,
            },
            'bayes_params': getattr(self, '_calibrated_bayes', None),
        }, path)
        self._msg(f"保存至: {path}")

    def load(self, path: str):
        """加载校准结果"""
        import joblib
        data = joblib.load(path)
        if 'xg_params' in data:
            self._calibrated_alpha = data['xg_params'].get('alpha')
            self._calibrated_beta = data['xg_params'].get('beta')
            self._calibrated_H = data['xg_params'].get('H_global', 0.15)
            self._calibrated_S = data['xg_params'].get('S_league')
            self.teams = data['xg_params'].get('teams', [])
            self.team_idx = data['xg_params'].get('team_idx', {})
            self.leagues = data['xg_params'].get('leagues', [])
            self.league_idx = data['xg_params'].get('league_idx', {})
        if 'bayes_params' in data:
            self._calibrated_bayes = data['bayes_params']
        self._msg(f"加载校准参数: {path}")

# ════════════════════════════════════════════════════════════════
# 便捷函数
# ════════════════════════════════════════════════════════════════

def quick_calibrate(db_path: str = "data/football_data.db",
                    max_samples: int = 50000,
                    fast_mode: bool = True,
                    verbose: bool = True) -> CalibrateResult:
    """快速校准（fast_mode=True: 比例法, 秒级; False: 贝叶斯逆推, 分钟级）"""
    calibrator = OddsInverseCalibrator(
        lambda_reg=0.2, max_iter=100, verbose=verbose
    )
    return calibrator.calibrate(
        db_path=db_path,
        train_start='2018-01-01',
        train_end='2022-12-31',
        val_start='2023-01-01',
        val_end='2025-12-31',
        max_samples=max_samples,
        fast_mode=fast_mode,
    )

def full_calibrate(db_path: str = "data/football_data.db",
                   max_samples: int = 200000,
                   fast_mode: bool = True) -> CalibrateResult:
    """完整校准（全量数据）"""
    calibrator = OddsInverseCalibrator(
        lambda_reg=0.2, max_iter=200, verbose=True
    )
    return calibrator.calibrate(
        db_path=db_path,
        train_start='2012-01-01',
        train_end='2022-12-31',
        val_start='2023-01-01',
        val_end='2025-12-31',
        max_samples=max_samples,
        fast_mode=fast_mode,
    )

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    result = quick_calibrate(max_samples=5000)
    print(f"\n校准完成!")
    print(f"  联赛先验: {len(result.bayes_params.get('league_prior', {}))} 个")
    print(f"  γ: {result.bayes_params.get('global_gamma', 'N/A')}")
    for msg in result.messages:
        print(f"  → {msg}")

def apply_goal_segment_correction(p_raw, total_goals):
    if total_goals <= 1: return p_raw * 1.08
    elif total_goals <= 3: return p_raw * 0.97
    else: return p_raw * 0.88
