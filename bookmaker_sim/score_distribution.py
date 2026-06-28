"""
哨响AI - 比分分布模拟器 v1.0
============================
基于泊松分布 (Poisson) 和 Dixon-Coles 调整的足球比分概率模型。

核心功能:
1. 从 λ_h, λ_a (预期进球) 生成完整比分分布 P(s_h, s_a)
2. 支持"知情模式" (informed mode): 围绕已知赛果生成概率分布
3. Dixon-Coles 调整: 修正低比分平局的泊松低估问题
4. 参数校准: 从历史数据估计进攻/防守强度

数学:
基础: P(s_h, s_a | λ_h, λ_a) = Poisson(s_h|λ_h) × Poisson(s_a|λ_a)
Dixon-Coles: 引入 τ 因子修正 0-0, 1-0, 0-1, 1-1 概率

知情模式 (角色互换核心):
给定"真实比分" R* = (r_h, r_a), 模拟庄家的内部分布:
  λ_h = max(0.05, r_h + ε_h),  ε_h ~ N(0, σ_hiding²)
  λ_a = max(0.05, r_a + ε_a),  ε_a ~ N(0, σ_hiding²)
其中 σ_hiding 控制庄家隐藏意图的力度。
"""

import numpy as np
import sqlite3
from typing import Dict, Tuple, Optional, List
from scipy.stats import poisson
from dataclasses import dataclass
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)

# 比分搜索空间
MAX_GOALS = 8
SCORE_SPACE = [(i, j) for i in range(MAX_GOALS + 1) for j in range(MAX_GOALS + 1)]

@dataclass
class ScoreDistribution:
    """比分概率分布"""
    matrix: np.ndarray          # (MAX_GOALS+1) × (MAX_GOALS+1) 概率矩阵
    lambda_h: float             # 主队预期进球
    lambda_a: float             # 客队预期进球 
    dixon_coles_rho: float = 0.0
    
    def prob(self, s_h: int, s_a: int) -> float:
        """查询特定比分的概率"""
        if 0 <= s_h <= MAX_GOALS and 0 <= s_a <= MAX_GOALS:
            return self.matrix[s_h, s_a]
        return 0.0
    
    def prob_home_win(self) -> float:
        return sum(self.matrix[i, j] for i, j in SCORE_SPACE if i > j)
    
    def prob_draw(self) -> float:
        return sum(self.matrix[i, j] for i, j in SCORE_SPACE if i == j)
    
    def prob_away_win(self) -> float:
        return sum(self.matrix[i, j] for i, j in SCORE_SPACE if i < j)
    
    def prob_total_over(self, threshold: float) -> float:
        return sum(self.matrix[i, j] for i, j in SCORE_SPACE if i + j > threshold)
    
    def prob_total_under(self, threshold: float) -> float:
        return sum(self.matrix[i, j] for i, j in SCORE_SPACE if i + j < threshold)
    
    def prob_handicap_cover(self, line: float, side: str = 'home') -> float:
        """让球盘覆盖概率: line<0 表示主队让球"""
        result = 0.0
        push = 0.0
        for i, j in SCORE_SPACE:
            adjusted = (i - j) + line  # line<0: 主让球; line>0: 主受让
            if adjusted > 0:
                result += self.matrix[i, j]
            elif adjusted == 0:
                push += self.matrix[i, j]
        return result + 0.5 * push  # 走水退半
    
    def prob_btts(self) -> float:
        """Both Teams to Score"""
        return sum(self.matrix[i, j] for i, j in SCORE_SPACE if i >= 1 and j >= 1)
    
    def most_likely_score(self) -> Tuple[int, int, float]:
        """最可能的比分及其概率"""
        best = max(SCORE_SPACE, key=lambda x: self.matrix[x[0], x[1]])
        return best[0], best[1], self.matrix[best[0], best[1]]
    
    def expected_total_goals(self) -> float:
        return sum((i + j) * self.matrix[i, j] for i, j in SCORE_SPACE)

class ScoreDistSimulator:
    """
    比分分布模拟器 — AORE框架第一层
    
    三种运行模式:
    1. basic: 给定 λ_h, λ_a → 独立泊松分布
    2. dixon_coles: 带低分平局调整的泊松分布  
    3. informed: 给定"已知赛果" → 生成庄家内部分布
    """
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or "data/football_data.db"
        self.max_goals = MAX_GOALS
        self._score_cache: Dict[str, np.ndarray] = {}
        
        # 联赛级别进攻/防守参数 (从历史数据校准)
        self.league_params: Dict[str, Dict[str, float]] = {}
        
        # 默认参数
        self.default_hfa = 0.35      # 主场优势 (额外进球)
        self.default_avg_goals = 2.75 # 联赛场均进球 (用于无数据联赛)
        self.dixon_coles_rho = -0.05  # Dixon-Coles ρ (默认轻微负相关)
        self.sigma_hiding = 0.40      # 知情模式噪声标准差
    
    # ──────────── 核心分布生成 ────────────
    
    def basic_poisson(self, lambda_h: float, lambda_a: float) -> ScoreDistribution:
        """
        独立泊松分布 (最基础模型)
        
        Args:
            lambda_h: 主队预期进球
            lambda_a: 客队预期进球
        
        Returns:
            ScoreDistribution 对象
        """
        lambda_h = max(0.02, lambda_h)
        lambda_a = max(0.02, lambda_a)
        
        cache_key = f"bp_{lambda_h:.4f}_{lambda_a:.4f}"
        if cache_key in self._score_cache:
            matrix = self._score_cache[cache_key].copy()
        else:
            matrix = np.zeros((self.max_goals + 1, self.max_goals + 1))
            for s_h in range(self.max_goals + 1):
                for s_a in range(self.max_goals + 1):
                    matrix[s_h, s_a] = poisson.pmf(s_h, lambda_h) * poisson.pmf(s_a, lambda_a)
            
            # 截断归一化
            total = matrix.sum()
            if total > 0:
                matrix /= total
            
            self._score_cache[cache_key] = matrix.copy()
        
        return ScoreDistribution(matrix=matrix, lambda_h=lambda_h, lambda_a=lambda_a)
    
    def dixon_coles(self, lambda_h: float, lambda_a: float, rho: float = None) -> ScoreDistribution:
        """
        Dixon-Coles 调整的比分分布
        
        修正独立泊松对 0-0, 1-0, 0-1, 1-1 的概率估计偏差。
        
        Args:
            lambda_h: 主队预期进球
            lambda_a: 客队预期进球
            rho: 低分平局调整参数 (负值=低估低分平局, 正值=高估)
        """
        if rho is None:
            rho = self.dixon_coles_rho
        
        lambda_h = max(0.02, lambda_h)
        lambda_a = max(0.02, lambda_a)
        
        matrix = np.zeros((self.max_goals + 1, self.max_goals + 1))
        
        for s_h in range(self.max_goals + 1):
            for s_a in range(self.max_goals + 1):
                # 基础独立泊松
                p = poisson.pmf(s_h, lambda_h) * poisson.pmf(s_a, lambda_a)
                
                # Dixon-Coles τ 调整因子
                tau = self._dixon_coles_tau(s_h, s_a, lambda_h, lambda_a, rho)
                matrix[s_h, s_a] = p * tau
        
        # 归一化
        total = matrix.sum()
        if total > 0:
            matrix /= total
        
        return ScoreDistribution(
            matrix=matrix, lambda_h=lambda_h, lambda_a=lambda_a, dixon_coles_rho=rho
        )
    
    def _dixon_coles_tau(self, s_h: int, s_a: int, lam_h: float, lam_a: float, rho: float) -> float:
        """Dixon-Coles τ 调整因子"""
        if s_h == 0 and s_a == 0:
            return 1.0 - lam_h * lam_a * rho
        elif s_h == 0 and s_a == 1:
            return 1.0 + lam_h * rho
        elif s_h == 1 and s_a == 0:
            return 1.0 + lam_a * rho
        elif s_h == 1 and s_a == 1:
            return 1.0 - rho
        else:
            return 1.0
    
    # ──────────── 知情模式 (角色互换核心) ────────────
    
    def informed_distribution(
        self, true_score_h: int, true_score_a: int,
        sigma_hiding: float = None, n_samples: int = 5000,
        method: str = 'dixon_coles'
    ) -> ScoreDistribution:
        """
        知情模式: 围绕已知赛果生成庄家内部概率分布
        
        这是角色互换逆向推演的核心：
        "如果庄家知道真实比分是 (r_h, r_a), 他会设定什么样的内部分布?"
        
        庄家面对两个矛盾:
        1. 需要盈利 → 赔率必须反映真实预期
        2. 需要隐藏 → 不能直接把 P(r_h, r_a) 设为 1.0
        
        策略: 围绕真实比分生成 λ 分布, 用 σ_hiding 控制信息泄漏程度
               σ_hiding 小 = 强烈信号 (庄家确信且大胆)
               σ_hiding 大 = 弱信号 (庄家谨慎隐藏)
        
        Args:
            true_score_h: 假设的真实主队进球
            true_score_a: 假设的真实客队进球
            sigma_hiding: 隐藏噪声标准差 (越小信号越强)
            n_samples: Monte Carlo 采样数
            method: 基础分布方法
        
        Returns:
            "庄家内部分布" ScoreDistribution
        """
        sigma = sigma_hiding if sigma_hiding is not None else self.sigma_hiding
        
        # Monte Carlo: 从 λ 分布采样, 聚合比分概率
        agg_matrix = np.zeros((self.max_goals + 1, self.max_goals + 1))
        
        for _ in range(n_samples):
            # λ 围绕真实比分, 加噪声
            lam_h = max(0.05, true_score_h + np.random.normal(-0.1, sigma))
            lam_a = max(0.05, true_score_a + np.random.normal(-0.1, sigma))
            
            # 生成此 λ 下的比分分布
            if method == 'dixon_coles':
                lam_h += self.default_hfa * 0.5  # 知情模式下减半主场优势
                dist = self.dixon_coles(lam_h, lam_a)
            else:
                dist = self.basic_poisson(lam_h, lam_a)
            
            agg_matrix += dist.matrix / n_samples
        
        avg_lam_h = max(0.05, true_score_h - 0.1)
        avg_lam_a = max(0.05, true_score_a - 0.1)
        
        return ScoreDistribution(matrix=agg_matrix, lambda_h=avg_lam_h, lambda_a=avg_lam_a)
    
    def search_optimal_score(
        self, real_odds_vector: np.ndarray, 
        sigma_hiding: float = None, max_score: int = 5
    ) -> Dict:
        """
        搜索最优比分假设: 在所有可能的比分中, 找到使模拟赔率最接近真实赔率的那个
        
        Args:
            real_odds_vector: 真实赔率隐含概率向量 [P(H), P(D), P(A), P(O2.5), P(H-cover-AH), ...]
            sigma_hiding: 隐藏力度
            max_score: 搜索的比分上限
        
        Returns:
            {best_score_h, best_score_a, best_anomaly, all_scores: [...]}
        """
        sigma = sigma_hiding or self.sigma_hiding
        all_scores = []
        
        for s_h in range(max_score + 1):
            for s_a in range(max_score + 1):
                # 生成知情分布
                dist = self.informed_distribution(s_h, s_a, sigma_hiding=sigma, n_samples=2000)
                
                # 提取模拟赔率向量
                simulated = self._distribution_to_odds_vector(dist, len(real_odds_vector))
                
                # KL 散度 (越小越接近)
                kl = self._kl_divergence(real_odds_vector, simulated)
                
                all_scores.append({
                    'score_h': s_h, 'score_a': s_a,
                    'kl_divergence': kl,
                    'simulated_vector': simulated.tolist(),
                })
        
        # 按 KL 散度排序
        all_scores.sort(key=lambda x: x['kl_divergence'])
        
        best = all_scores[0]
        return {
            'best_score_h': best['score_h'],
            'best_score_a': best['score_a'],
            'best_anomaly': best['kl_divergence'],
            'top5': all_scores[:5],
            'all_scores': all_scores,
        }
    
    def _distribution_to_odds_vector(self, dist: ScoreDistribution, vector_len: int = 7) -> np.ndarray:
        """从比分分布提取核心赔率向量"""
        vec = np.zeros(vector_len)
        # [P(H), P(D), P(A), P(O2.5), P(H-cover-AH-0.5), P(BTTS), P(total<1.5)]
        vec[0] = dist.prob_home_win()
        vec[1] = dist.prob_draw()
        vec[2] = dist.prob_away_win()
        if vector_len > 3:
            vec[3] = dist.prob_total_over(2.5)
        if vector_len > 4:
            vec[4] = dist.prob_handicap_cover(-0.5)
        if vector_len > 5:
            vec[5] = dist.prob_btts()
        if vector_len > 6:
            vec[6] = dist.prob_total_under(1.5)
        return vec
    
    def _kl_divergence(self, p: np.ndarray, q: np.ndarray) -> float:
        """KL(P||Q), 加平滑避免 log(0)"""
        eps = 1e-10
        p = np.clip(p, eps, 1 - eps)
        q = np.clip(q, eps, 1 - eps)
        # 归一化
        p = p / p.sum()
        q = q / q.sum()
        return float(np.sum(p * np.log(p / q)))
    
    # ──────────── λ 参数估计 ────────────
    
    def estimate_lambda_from_team_strength(
        self, home_attack: float, home_defense: float,
        away_attack: float, away_defense: float,
        league_avg_goals: float = None
    ) -> Tuple[float, float]:
        """
        从攻防强度估计 λ
        
        标准足球预期进球模型:
          λ_h = Attack_home × Defense_away × HomeAdvantage × LeagueAvg
          λ_a = Attack_away × Defense_home × LeagueAvg
        
        Args:
            home_attack: 主队进攻强度 (>1强, <1弱)
            home_defense: 主队防守强度 (>1弱, <1强)
            away_attack: 客队进攻强度
            away_defense: 客队防守强度
        
        Returns:
            (lambda_h, lambda_a)
        """
        avg = league_avg_goals or self.default_avg_goals
        base = avg / 2.0  # 平均每队进球
        
        lam_h = base * home_attack * (2.0 - away_defense) * (1 + self.default_hfa / base)
        lam_a = base * away_attack * (2.0 - home_defense)
        
        return max(0.05, lam_h), max(0.05, lam_a)
    
    def calibrate_from_db(self, league_name: str = None) -> Dict:
        """
        从数据库历史比赛校准 Dixson-Coles 参数
        
        Returns:
            {avg_goals, home_win_rate, draw_rate, dixon_coles_rho}
        """
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        query = """
        SELECT home_score, away_score FROM matches 
        WHERE home_score IS NOT NULL AND away_score IS NOT NULL
        """
        params = []
        if league_name:
            query += " AND league_name = ?"
            params.append(league_name)
        
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()
        
        if not rows:
            logger.warning(f"No historical data for calibration (league={league_name})")
            return {'avg_goals': self.default_avg_goals, 'sample_size': 0}
        
        scores = np.array(rows)
        total_goals = scores[:, 0] + scores[:, 1]
        avg_goals = float(np.mean(total_goals))
        
        # 计算观测概率
        n = len(rows)
        obs_0_0 = np.sum((scores[:, 0] == 0) & (scores[:, 1] == 0)) / n
        obs_1_0 = np.sum((scores[:, 0] == 1) & (scores[:, 1] == 0)) / n
        obs_0_1 = np.sum((scores[:, 0] == 0) & (scores[:, 1] == 1)) / n
        obs_1_1 = np.sum((scores[:, 0] == 1) & (scores[:, 1] == 1)) / n
        
        # 估计 ρ (简化版: 以 1-1 为例反推)
        # 独立泊松下 P(1,1) = λ_h*e^{-λ_h} * λ_a*e^{-λ_a}
        # Dixon-Coles: P(1,1) = (1-ρ) * P_indep(1,1)
        lam = avg_goals / 2.0
        p_11_indep = lam * np.exp(-lam) * lam * np.exp(-lam)
        if p_11_indep > 1e-10:
            rho_est = 1.0 - obs_1_1 / p_11_indep
            rho_est = np.clip(rho_est, -0.3, 0.3)  # 合理范围
        else:
            rho_est = -0.05
        
        home_win_rate = np.sum(scores[:, 0] > scores[:, 1]) / n
        
        result = {
            'avg_goals': round(avg_goals, 3),
            'home_win_rate': round(home_win_rate, 4),
            'draw_rate': round(np.sum(scores[:, 0] == scores[:, 1]) / n, 4),
            'dixon_coles_rho': round(rho_est, 4),
            'sample_size': n,
        }
        
        # 更新实例参数
        self.default_avg_goals = avg_goals
        self.dixon_coles_rho = rho_est
        
        logger.info(f"Calibrated: avg_goals={avg_goals:.2f}, rho={rho_est:.4f}, n={n}")
        return result

# ──────────── 便捷函数 ────────────

def create_simulator(db_path: str = None) -> ScoreDistSimulator:
    return ScoreDistSimulator(db_path)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    sim = ScoreDistSimulator()
    
    # 测试: 基础泊松
    print("=== 基础泊松分布 (λ_h=1.8, λ_a=1.2) ===")
    dist = sim.basic_poisson(1.8, 1.2)
    print(f"P(H)={dist.prob_home_win():.4f}, P(D)={dist.prob_draw():.4f}, P(A)={dist.prob_away_win():.4f}")
    print(f"P(Over 2.5)={dist.prob_total_over(2.5):.4f}")
    best = dist.most_likely_score()
    print(f"Most likely: {best[0]}-{best[1]} ({best[2]:.4f})")
    
    # 测试: Dixon-Coles
    print("\n=== Dixon-Coles (λ_h=1.8, λ_a=1.2, ρ=-0.08) ===")
    dc = sim.dixon_coles(1.8, 1.2, rho=-0.08)
    print(f"P(1-1) basic={dist.prob(1,1):.4f}, DC={dc.prob(1,1):.4f} (DC adjusts draws)")
    
    # 测试: 知情模式
    print("\n=== 知情模式 (true=2-1) ===")
    informed = sim.informed_distribution(2, 1, sigma_hiding=0.3)
    print(f"P(H)={informed.prob_home_win():.4f}, P(D)={informed.prob_draw():.4f}, P(A)={informed.prob_away_win():.4f}")
    print(f"P(2-1)={informed.prob(2,1):.4f} (highest?)")
    best_i = informed.most_likely_score()
    print(f"Most likely: {best_i[0]}-{best_i[1]} ({best_i[2]:.4f})")
    
    # 测试: 搜索最优比分
    print('\n=== 搜索最优比分 (模拟真实赔率 来自 2-1 知情分布) ===')
    real_vec = sim._distribution_to_odds_vector(informed, 7)
    result = sim.search_optimal_score(real_vec, sigma_hiding=0.3)
    print(f"Best guess: {result['best_score_h']}-{result['best_score_a']} (KL={result['best_anomaly']:.6f})")
    print("Top 5:")
    for r in result['top5']:
        print(f"  {r['score_h']}-{r['score_a']}: KL={r['kl_divergence']:.6f}")
