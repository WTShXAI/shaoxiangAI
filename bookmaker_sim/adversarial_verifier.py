"""
哨响AI - 对抗赔率验证器 v1.0 (角色互换逆向推演)
=================================================
AORE框架第三层: 对抗式验证管线

核心方法: "如果我是庄家, 知道真实比分会如何定价?"

流程:
1. 收集所有可用玩法的真实赔率 → 提取隐含概率向量
2. 对每一个可能的比分 (0-0 到 5-5):
   a. 用 ScoreDistSimulator.informed_distribution() 生成"庄家知情分布"
   b. 用 MarketDerivationEngine 从知情分布推导全市场赔率
   c. 对比推导赔率 vs 真实赔率 (KL散度)
3. KL散度最小的比分 = 最可能的"庄家隐藏预期"
4. 输出: 逆向推演比分 + 置信度 + 异常信号强度

关键洞察:
  单一盘口的赔率偏差可能是噪声。
  但跨盘口 (1X2 + AH + Totals + Correct Score) 的一致性偏差 =
  博彩公司私密预期的指纹。
"""

import numpy as np
import sqlite3
import json
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from datetime import datetime
import logging

from .score_distribution import ScoreDistSimulator, ScoreDistribution
from .market_derivation import MarketDerivationEngine, MarketOdds

logger = logging.getLogger(__name__)


@dataclass
class AdversarialResult:
    """角色互换推演结果"""
    match_id: int
    home_team: str = ""
    away_team: str = ""
    
    # 逆向推演的最优比分
    best_score_h: int = 0
    best_score_a: int = 0
    confidence: float = 0.0
    
    # 异常分数 (越低越正常, 与庄家常态一致)
    anomaly_score: float = 0.0
    
    # 信号强度 (0-1, 1=极强信号)
    signal_strength: float = 0.0
    
    # 推演详情
    top_candidates: List[Dict] = field(default_factory=list)
    
    # 跨市场一致性
    consistency_1x2_ah: float = 0.0   # 1X2 vs AH 一致性
    consistency_1x2_totals: float = 0.0
    
    # 原模型预测
    model_pred_h: float = 0.0
    model_pred_d: float = 0.0
    model_pred_a: float = 0.0
    
    # 元信息
    timestamp: str = ""
    markets_used: List[str] = field(default_factory=list)
    error: str = ""


class AdversarialOddsVerifier:
    """
    对抗赔率验证器 — AORE角色互换核心
    
    用法:
        verifier = AdversarialOddsVerifier()
        result = verifier.verify_match(match_id=123)
        print(f"逆向推演比分: {result.best_score_h}-{result.best_score_a}")
        print(f"置信度: {result.confidence:.2%}")
    """
    
    def __init__(self, db_path: str = None, sigma_hiding: float = 0.35):
        self.db_path = db_path or "data/football_data.db"
        self.simulator = ScoreDistSimulator(self.db_path)
        self.engine = MarketDerivationEngine(default_margin=0.06)
        self.sigma_hiding = sigma_hiding
        
        # 搜索范围
        self.max_search_score = 5
        
        # 赔率向量维度定义
        # [P(H), P(D), P(A), P(O2.5), P(H-cover-AH-0.5), P(O1.5), P(U2.5)]
        self.vector_dim = 7
    
    # ──────────── 主验证流程 ────────────
    
    def verify_match(self, match_id: int) -> AdversarialResult:
        """
        对单场比赛执行完整的角色互换逆向推演
        
        Args:
            match_id: 比赛ID
        
        Returns:
            AdversarialResult 对象
        """
        result = AdversarialResult(
            match_id=match_id,
            timestamp=datetime.now().isoformat(),
        )
        
        try:
            # Step 1: 从数据库收集真实赔率
            real_odds = self._collect_real_odds(match_id)
            if not real_odds:
                result.error = "No real odds data available"
                return result
            
            result.markets_used = list(real_odds.keys())
            
            # 获取球队名
            team_info = self._get_match_info(match_id)
            result.home_team = team_info.get('home_team', '')
            result.away_team = team_info.get('away_team', '')
            
            # Step 2: 提取真实赔率隐含概率向量
            real_vector = self._real_odds_to_vector(real_odds)
            
            # Step 3: 对每个可能的比分, 生成"庄家知情分布"并对比
            candidates = []
            for s_h in range(self.max_search_score + 1):
                for s_a in range(self.max_search_score + 1):
                    # 生成知情分布
                    informed_dist = self.simulator.informed_distribution(
                        s_h, s_a, sigma_hiding=self.sigma_hiding, n_samples=2000
                    )
                    
                    # 从知情分布生成全市场赔率
                    simulated_markets = self.engine.derive_all_markets(informed_dist)
                    
                    # 提取模拟赔率向量
                    sim_vector = self._simulated_markets_to_vector(simulated_markets)
                    
                    # 计算 KL 散度
                    kl = self._safe_kl_divergence(real_vector, sim_vector)
                    
                    candidates.append({
                        'score_h': s_h,
                        'score_a': s_a,
                        'kl_divergence': float(kl),
                        'sim_vector': sim_vector.tolist(),
                    })
            
            # 按 KL 散度排序 (越小越好)
            candidates.sort(key=lambda x: x['kl_divergence'])
            
            result.top_candidates = candidates[:5]
            
            best = candidates[0]
            result.best_score_h = best['score_h']
            result.best_score_a = best['score_a']
            result.anomaly_score = float(best['kl_divergence'])
            
            # 置信度 = 最优 vs 次优的距离 / 次优距离
            if len(candidates) > 1:
                gap = candidates[1]['kl_divergence'] - candidates[0]['kl_divergence']
                # 归一化到 0-1
                max_kl = max(c['kl_divergence'] for c in candidates)
                if max_kl > 0:
                    result.confidence = min(1.0, gap / (max_kl + 1e-10) * 10)
                result.confidence = np.clip(result.confidence, 0.0, 1.0)
            else:
                result.confidence = 0.5
            
            # 信号强度 = 1 - (最优KL / 平均KL)
            avg_kl = np.mean([c['kl_divergence'] for c in candidates])
            if avg_kl > 0:
                result.signal_strength = float(np.clip(1.0 - best['kl_divergence'] / avg_kl, 0.0, 1.0))
            
            # Step 4: 跨市场一致性
            result.consistency_1x2_ah = self._check_pairwise_consistency(
                real_odds, '1x2', 'ah'
            )
            result.consistency_1x2_totals = self._check_pairwise_consistency(
                real_odds, '1x2', 'totals'
            )
            
            # Step 5: 获取模型预测对比
            model_pred = self._get_model_prediction(match_id)
            if model_pred:
                result.model_pred_h = model_pred.get('prob_h', 0)
                result.model_pred_d = model_pred.get('prob_d', 0)
                result.model_pred_a = model_pred.get('prob_a', 0)
            
            # Step 6: 写入 cross_market_consistency 表
            self._save_to_db(result)
            
        except (Exception, requests.exceptions.RequestException) as e:
            logger.error(f"Adversarial verification failed for match {match_id}: {e}")
            result.error = str(e)
        
        return result
    
    def batch_verify(self, match_ids: List[int], verbose: bool = True) -> List[AdversarialResult]:
        """
        批量逆向推演
        """
        results = []
        for i, mid in enumerate(match_ids):
            if verbose and (i + 1) % 10 == 0:
                logger.info(f"  推演进度: {i+1}/{len(match_ids)}")
            result = self.verify_match(mid)
            results.append(result)
        return results
    
    # ──────────── 内部方法 ────────────
    
    def _collect_real_odds(self, match_id: int) -> Dict[str, Dict[str, float]]:
        """
        从 betting_markets / odds / odds_timeline 表收集真实赔率
        
        Returns:
            {
                '1x2': {'home': 2.1, 'draw': 3.5, 'away': 3.2},
                'ah': {'home_cover': 1.95, 'home_not_cover': 1.95},
                'totals': {'over': 1.9, 'under': 1.9},
                ... (如果有)
            }
        """
        odds_data = {}
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        # 先尝试 betting_markets 表
        cur.execute("""
            SELECT market_type, market_line, outcome_name, odds 
            FROM betting_markets 
            WHERE match_id = ? AND provider = 'pinnacle'
            ORDER BY timestamp DESC
        """, (match_id,))
        rows = cur.fetchall()
        
        if rows:
            for market_type, line, outcome, odd in rows:
                key = market_type
                if line:
                    key = f"{market_type}_{line}"
                if key not in odds_data:
                    odds_data[key] = {}
                odds_data[key][outcome] = odd
        else:
            # Fallback: odds 表 (只有 1X2)
            cur.execute("""
                SELECT home_odds, draw_odds, away_odds 
                FROM odds 
                WHERE match_id = ? AND home_odds IS NOT NULL
                ORDER BY odds_id DESC LIMIT 1
            """, (match_id,))
            row = cur.fetchone()
            if row:
                odds_data['1x2'] = {'home': row[0], 'draw': row[1], 'away': row[2]}
        
        conn.close()
        return odds_data
    
    def _real_odds_to_vector(self, real_odds: Dict[str, Dict[str, float]]) -> np.ndarray:
        """真实赔率 → 隐含概率向量"""
        vec = np.zeros(self.vector_dim)
        
        # 1X2
        if '1x2' in real_odds and all(k in real_odds['1x2'] for k in ['home','draw','away']):
            probs = self.engine.odds_to_implied_probs(real_odds['1x2'], True)
            vec[0] = probs.get('home', 0)
            vec[1] = probs.get('draw', 0)
            vec[2] = probs.get('away', 0)
        
        # Over 2.5
        for key in real_odds:
            if key.startswith('totals') and ('2.5' in key or '2_5' in key or '2_50' in key):
                ou = real_odds[key]
                if 'over' in ou and 'under' in ou:
                    probs = self.engine.odds_to_implied_probs(ou, True)
                    vec[3] = probs.get('over', 0)
        
        # AH -0.5 (主让半球)
        for key in real_odds:
            if 'ah' in key.lower() and ('-0.5' in key or 'NEG_0_5' in key or 'NEG_0_50' in key):
                ah = real_odds[key]
                if 'home_cover' in ah:
                    probs = self.engine.odds_to_implied_probs(ah, True)
                    vec[4] = probs.get('home_cover', 0)
        
        # 归一化
        if vec.sum() > 0:
            pass  # 各维度独立, 不全局归一化
        
        return vec
    
    def _simulated_markets_to_vector(self, markets: Dict[str, MarketOdds]) -> np.ndarray:
        """模拟推导的市场赔率 → 隐含概率向量"""
        vec = np.zeros(self.vector_dim)
        
        # 1X2
        if '1x2' in markets:
            m = markets['1x2']
            vec[0] = m.implied_probs.get('home', 0)
            vec[1] = m.implied_probs.get('draw', 0)
            vec[2] = m.implied_probs.get('away', 0)
        
        # Totals 2.5
        for key in markets:
            if key.startswith('totals') and '2_50' in key:
                m = markets[key]
                vec[3] = m.implied_probs.get('over', 0)
        
        # AH -0.5
        for key in markets:
            if 'ah' in key and 'NEG_0_50' in key:
                m = markets[key]
                vec[4] = m.implied_probs.get('home_cover', 0)
        
        return vec
    
    def _safe_kl_divergence(self, p: np.ndarray, q: np.ndarray) -> float:
        """安全的 KL 散度计算"""
        eps = 1e-10
        p = np.clip(p, eps, 1 - eps)
        q = np.clip(q, eps, 1 - eps)
        # 只对非零维度计算
        mask = (p > eps * 10) & (q > eps * 10)
        if mask.sum() == 0:
            return 10.0  # 完全无信号
        
        p_f = p[mask]
        q_f = q[mask]
        p_f = p_f / p_f.sum()
        q_f = q_f / q_f.sum()
        
        return float(np.sum(p_f * np.log(p_f / q_f)))
    
    def _check_pairwise_consistency(self, real_odds: Dict, market_a: str, market_b: str) -> float:
        """检查两个玩法之间的一致性"""
        # 简化实现: 如果两个玩法都存在, 检查方向一致性
        if market_a not in real_odds or market_b not in real_odds:
            return 0.0
        
        # 实际应使用 ScoreDistSimulator 做完整推导
        # 这里返回占位值
        return 0.5
    
    def _get_match_info(self, match_id: int) -> Dict:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT home_team_name, away_team_name FROM matches WHERE match_id=?", (match_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {'home_team': row[0], 'away_team': row[1]}
        return {}
    
    def _get_model_prediction(self, match_id: int) -> Optional[Dict]:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT prob_h, prob_d, prob_a, predicted_result 
            FROM predictions WHERE match_id=? 
            ORDER BY prediction_id DESC LIMIT 1
        """, (match_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {'prob_h': row[0], 'prob_d': row[1], 'prob_a': row[2], 'predicted': row[3]}
        return None
    
    def _save_to_db(self, result: AdversarialResult):
        """保存推演结果到 cross_market_consistency 表"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO cross_market_consistency 
                (match_id, analysis_time, consistency_score, anomaly_score, kl_divergence,
                 adversarial_score_h, adversarial_score_a, adversarial_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result.match_id,
                result.timestamp,
                result.consistency_1x2_ah,  # placeholder
                result.anomaly_score,
                result.anomaly_score,  # kl_divergence
                result.best_score_h,
                result.best_score_a,
                result.confidence,
            ))
            conn.commit()
            conn.close()
        except (Exception) as e:
            logger.warning(f"Failed to save adversarial result: {e}")
    
    # ──────────── 信号统计 ────────────
    
    def signal_summary(self, results: List[AdversarialResult]) -> Dict:
        """批量推演结果的信号摘要"""
        if not results:
            return {}
        
        valid = [r for r in results if not r.error]
        high_signal = [r for r in valid if r.signal_strength > 0.6]
        
        return {
            'total_matches': len(results),
            'verified': len(valid),
            'errors': len(results) - len(valid),
            'high_signal_matches': len(high_signal),
            'avg_confidence': float(np.mean([r.confidence for r in valid])),
            'avg_signal_strength': float(np.mean([r.signal_strength for r in valid])),
            'score_distribution': self._score_histogram(valid),
        }
    
    def _score_histogram(self, results: List[AdversarialResult]) -> Dict[str, int]:
        hist = {}
        for r in results:
            key = f"{r.best_score_h}-{r.best_score_a}"
            hist[key] = hist.get(key, 0) + 1
        return dict(sorted(hist.items(), key=lambda x: -x[1])[:10])


# ──────────── 便捷函数 ────────────

def create_verifier(db_path: str = None) -> AdversarialOddsVerifier:
    return AdversarialOddsVerifier(db_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # 模拟测试: 没有真实赔率数据时, 用模拟数据验证框架
    from .score_distribution import ScoreDistSimulator
    from .market_derivation import MarketDerivationEngine
    
    sim = ScoreDistSimulator()
    engine = MarketDerivationEngine()
    
    # 模拟: 假设庄家知道真实比分是 2-1
    true_h, true_a = 2, 1
    informed_dist = sim.informed_distribution(true_h, true_a, sigma_hiding=0.3)
    
    # 从知情分布生成"真实赔率" (模拟庄家定价)
    real_markets = engine.derive_all_markets(informed_dist)
    
    # 现在用验证器搜索最优比分
    real_vec = np.zeros(7)
    if '1x2' in real_markets:
        real_vec[0] = real_markets['1x2'].implied_probs.get('home', 0)
        real_vec[1] = real_markets['1x2'].implied_probs.get('draw', 0)
        real_vec[2] = real_markets['1x2'].implied_probs.get('away', 0)
    if 'totals_2_50' in real_markets:
        real_vec[3] = real_markets['totals_2_50'].implied_probs.get('over', 0)
    if 'ah_NEG_0_50' in real_markets:
        real_vec[4] = real_markets['ah_NEG_0_50'].implied_probs.get('home_cover', 0)
    
    print(f"真实隐含概率向量: {np.round(real_vec[:5], 3)}")
    print(f"已知比分: {true_h}-{true_a}")
    
    # 搜索
    result = sim.search_optimal_score(real_vec, sigma_hiding=0.3)
    print(f"\n逆向推演结果: {result['best_score_h']}-{result['best_score_a']}")
    print(f"是否正确? {'✓' if result['best_score_h'] == true_h and result['best_score_a'] == true_a else '✗'}")
    print("\nTop 5 候选:")
    for r in result['top5']:
        marker = " ← 正确" if r['score_h'] == true_h and r['score_a'] == true_a else ""
        print(f"  {r['score_h']}-{r['score_a']}: KL={r['kl_divergence']:.6f}{marker}")
