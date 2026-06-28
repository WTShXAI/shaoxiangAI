"""
高精度选择性预测引擎 v3.0 - 重构版本
目标：从全量比赛中筛选出高把握场次，目标准确率≥80%

重构说明：
- 将原 _predict_batch() (288行) 拆分为多个单一职责函数
- 提高代码可读性、可维护性和可测试性
- 保持原有业务逻辑完全不变
"""

import sys, os, json, logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import sqlite3
import joblib
from typing import Optional, Dict

logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('SelectivePredictor')

@dataclass
class PredictionResult:
    """单场预测结果"""
    match_id: int
    match_date: str
    home_team: str
    away_team: str
    league: str
    
    # 概率
    prob_h: float
    prob_d: float
    prob_a: float
    
    # 门控得分
    consensus_score: float    # 赔率共识分 (0-100)
    confidence_score: float   # 置信度分 (0-100)
    feature_score: float      # 特征质量分 (0-100)
    odds_clarity_score: float # 赔率清晰度分 (0-100)
    
    total_score: float        # 总分 (0-100)
    tier: str                 # S/A/B/C
    predicted_result: str     # H/D/A
    recommendation: str       # 建议
    
    # 赔率信息
    odds_h: Optional[float] = None
    odds_d: Optional[float] = None
    odds_a: Optional[float] = None
    
    # 降级标记
    odds_fallback: bool = False    # 赔率降级模式
    default_ratio: float = 0.0     # 默认特征比例
    feature_coverage: float = 0.0  # 特征覆盖率
    odds_direction: Optional[str] = None
    
    # Gate 6: 时序锁检测 (v2.0)
    temporal_lock_score: float = 0.0   # 时序锁得分 (0-100)
    lock_confidence: float = 0.0       # OTSM 锁定期确信度 [0,1]
    
    # Gate 7: 让球覆盖检测 (v3.0)
    handicap_cover_score: float = 0.0  # 让球覆盖门控分 (0-100)
    handicap_cover_prob: float = 0.0   # 让球赢盘概率 [0,1]
    handicap_value_exists: int = 0     # 价值投注信号: 0/1
    
    # 元信息
    predicted_at: str = ""

class SelectivePredictor:
    """
    高精度选择性预测引擎 (重构版)
    
    使用方式:
        sp = SelectivePredictor(model_path='saved_models/football_ensemble_20260613_135814.joblib')
        results = sp.predict_upcoming_matches()  # 预测所有未来比赛
        top_picks = sp.get_top_picks(n=10)        # 获取最佳10场
    """
    
    def __init__(self, 
                 model_path: str = 'saved_models/football_ensemble_20260613_135814.joblib',
                 db_path: str = 'data/football_data.db',
                 config: Optional[Dict] = None):
        
        self.model_path = model_path
        self.db_path = db_path
        
        # ── 门控参数 (基于甜区分析) ──
        self.config = {
            # Gate 1: 赔率-模型共识
            'consensus_required': True,       # 共识必须一致(S级)
            'consensus_weight': 30,           # 权重分
            # Gate 2: 置信度
            'confidence_s_threshold': 0.53,   # S级下限 → 80.9%准确率
            'confidence_a_threshold': 0.46,   # A级下限 → 71.9%准确率
            'confidence_b_threshold': 0.40,   # B级下限 → ~60%准确率
            'confidence_weight': 35,          # 权重分(最重要)
            # Gate 3: 特征质量
            'feature_s_threshold': 0.50,      # 50%+特征非默认
            'feature_a_threshold': 0.40,
            'feature_weight': 15,
            # Gate 4: 赔率清晰度
            'odds_clarity_s_threshold': 0.15, # 赔率差距≥15pp
            'odds_clarity_a_threshold': 0.08,
            'odds_clarity_weight': 15,
            # Gate 5: D抑制
            'd_suppression': True,            # 抑制平局预测
            'd_max_score': 75,                # 预测D时总分上限75(不可能S级)
            # Gate 6: 时序锁检测 (v2.0, OTSM)
            'temporal_lock_weight': 10,       # 权重分 (辅助信号，不如赔率共识重要)
            'temporal_lock_s_threshold': 0.85,  # S级: lock_confidence≥0.85
            'temporal_lock_a_threshold': 0.70,  # A级: lock_confidence≥0.70
            'temporal_lock_b_threshold': 0.50,  # B级: lock_confidence≥0.50
            # Gate 7: 让球覆盖检测 (v3.0)
            'handicap_cover_weight': 10,
            'handicap_cover_s_threshold': 0.80,
            'handicap_cover_a_threshold': 0.65,
            'handicap_cover_b_threshold': 0.55,
            # 其他
            'odds_required': True,
            'odds_fallback_threshold': 0.70,
        }
        
        if config:
            self.config.update(config)
        
        # 加载模型
        self._load_model()
        
        # OTSM 时序状态机 (v2.0) — 延迟初始化，需要时加载
        self._otsm = None
        self._otsm_thresholds = None
        
        # 特征计数
        self.default_features = [
            'aerial_advantage', 'card_risk', 'delta_fatigue', 
            'fitness_75', 's_whale', 'discussion_growth', 'news_impact',
            'time_suppression', 'referee_matrix', 'arbitrage_index',
            'arbitrage_window', 'miss_weather', 'miss_drift',
            'weather_modifier',
        ]
        
        logger.info(f"SelectivePredictor 初始化完成 (重构版)")
        logger.info(f"  模型: {model_path}")
        logger.info(f"  S级阈值: conf≥{self.config['confidence_s_threshold']}, "
                    f"共识={self.config['consensus_required']}")
    
    # ── 公有方法 ──────────────────────────────────────────────────────────
    
    def predict_upcoming_matches(self, days_ahead: int = 7) -> List[PredictionResult]:
        """预测未来N天的比赛并进行门控筛选"""
        conn = sqlite3.connect(self.db_path)
        
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        end = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime('%Y-%m-%d')
        
        df = pd.read_sql_query('''
            SELECT m.match_id, m.match_date, m.home_team_name, m.away_team_name,
                   m.league_name, mf.*
            FROM matches m
            JOIN match_features mf ON m.match_id = mf.match_id
            WHERE m.match_date >= ? AND m.match_date <= ?
              AND m.home_score IS NULL
            ORDER BY m.match_date
        ''', conn, params=[now, end])
        
        if len(df) == 0:
            logger.warning(f"未来{days_ahead}天没有可用比赛")
            conn.close()
            return []
        
        df = df.loc[:, ~df.columns.duplicated()].copy()
        odds_df = self._get_odds(conn, df['match_id'].tolist())
        conn.close()
        
        logger.info(f"找到 {len(df)} 场未来比赛")
        
        results = self._predict_batch(df, odds_df)
        results.sort(key=lambda x: x.total_score, reverse=True)
        
        return results
    
    # ── 私有方法：数据获取 ────────────────────────────────────────────────
    
    def _get_odds(self, conn, match_ids: List[int]) -> pd.DataFrame:
        """获取比赛赔率"""
        if not match_ids:
            return pd.DataFrame()
        
        placeholders = ','.join(['?'] * len(match_ids))
        odds_df = pd.read_sql_query(f'''
            SELECT match_id, home_odds, draw_odds, away_odds,
                   MAX(odds_timestamp) as ts
            FROM odds
            WHERE match_id IN ({placeholders})
            GROUP BY match_id
        ''', conn, params=match_ids)
        
        if len(odds_df) > 0:
            odds_df['home_imp'] = 1.0 / odds_df['home_odds']
            odds_df['draw_imp'] = 1.0 / odds_df['draw_odds']
            odds_df['away_imp'] = 1.0 / odds_df['away_odds']
            s = odds_df['home_imp'] + odds_df['draw_imp'] + odds_df['away_imp']
            odds_df['home_imp_prob'] = odds_df['home_imp'] / s
            odds_df['draw_imp_prob'] = odds_df['draw_imp'] / s
            odds_df['away_imp_prob'] = odds_df['away_imp'] / s
        
        return odds_df
    
    # ── 私有方法：批量预测（重构后的核心函数）─────────────────────────────
    
    def _predict_batch(self, df: pd.DataFrame, odds_df: pd.DataFrame) -> List[PredictionResult]:
        """
        批量预测并打分 (重构版)
        
        重构说明：
        - 原函数 288 行 → 现在只有 40 行
        - 核心逻辑委托给多个单一职责函数
        """
        if len(df) == 0:
            return []
        
        # 1. 准备特征和预测
        proba, confidence, pred_direction, feature_coverage, default_ratio = \
            self._prepare_features_and_predict(df)
        
        # 2. 构建赔率映射
        odds_map = self._build_odds_map(odds_df)
        
        # 3. 对每场比赛进行门控打分
        results = []
        for i in range(len(df)):
            result = self._process_single_match(
                df.iloc[i], proba[i], confidence[i], 
                feature_coverage[i], default_ratio[i],
                odds_map
            )
            results.append(result)
        
        return results
    
    # ── 特征准备和预测 ────────────────────────────────────────────────────
    
    def _prepare_features_and_predict(self, df: pd.DataFrame) -> None:
        """
        准备特征并执行预测
        
        Returns:
            proba: 预测概率矩阵 (n_samples, 3)
            confidence: 置信度数组 (n_samples,)
            pred_direction: 预测方向数组 (n_samples,)
            feature_coverage: 特征覆盖率数组 (n_samples,)
            default_ratio: 默认特征比例数组 (n_samples,)
        """
        # 保存原始特征名
        orig_features = list(self.feature_names)
        
        # 准备特征
        X, _ = self.trainer.prepare_features(df)
        
        # 确保所有 orig_features 都有值
        X_mat = np.zeros((len(X), len(orig_features)))
        for i, feat in enumerate(orig_features):
            if feat in X.columns:
                X_mat[:, i] = X[feat].values
            else:
                X_mat[:, i] = 0
        
        X_scaled = self.trainer.scaler.transform(X_mat)
        
        # 预测
        league_names = df['league_name'].tolist()
        proba = self.trainer.ensemble_predict_proba(X_scaled, league_names=league_names)
        
        # 调试：检查 proba 范围
        proba_min, proba_max = float(np.min(proba)), float(np.max(proba))
        print(f"DEBUG: proba range = [{proba_min:.4f}, {proba_max:.4f}]")
        
        # 保护：若 proba 明显是 0-100 范围（值>1），则除以 100
        if proba_max > 1.0:
            proba = proba / 100.0
            print(f"DEBUG: proba divided by 100, new range = [{np.min(proba):.4f}, {np.max(proba):.4f}]")
        
        # 计算置信度
        raw_confidence = np.max(proba, axis=1)
        if np.median(raw_confidence) > 1.0:
            raw_confidence = raw_confidence / 100.0
        confidence = raw_confidence
        pred_direction = np.array(['H','D','A'])[np.argmax(proba, axis=1)]
        
        # 特征覆盖率计算
        feat_values = X_mat
        feat_stds = np.std(feat_values, axis=0)
        
        feature_coverage = np.zeros(len(df))
        default_ratio = np.zeros(len(df))
        
        for i in range(len(df)):
            row_vals = feat_values[i]
            deviations = np.abs(row_vals - np.mean(feat_values, axis=0))
            n_informative = np.sum(deviations > feat_stds * 0.5)
            n_total = len(orig_features)
            feature_coverage[i] = n_informative / max(n_total, 1)
            default_ratio[i] = 1.0 - feature_coverage[i]
        
        return proba, confidence, pred_direction, feature_coverage, default_ratio
    
    # ── 赔率处理 ──────────────────────────────────────────────────────────
    
    def _build_odds_map(self, odds_df: pd.DataFrame) -> Dict[int, dict]:
        """构建 match_id → odds信息的映射"""
        odds_map = {}
        if len(odds_df) > 0:
            for _, row in odds_df.iterrows():
                odds_map[int(row['match_id'])] = row.to_dict()
        return odds_map
    
    def _extract_odds_info(self, odds_row: Optional[dict]) -> Optional[Dict]:
        """
        从 odds_row 提取赔率信息
        
        Returns:
            (o_h, o_d, o_a, imp_h, imp_d, imp_a, odds_direction, odds_clarity, has_odds)
        """
        if odds_row is None or not isinstance(odds_row, dict) or len(odds_row) == 0:
            return (None, None, None, 0, 0, 0, None, 0, False)
        
        o_h, o_d, o_a = odds_row['home_odds'], odds_row['draw_odds'], odds_row['away_odds']
        imp_h = odds_row.get('home_imp_prob', 0)
        imp_d = odds_row.get('draw_imp_prob', 0)
        imp_a = odds_row.get('away_imp_prob', 0)
        
        odds_dir_idx = np.argmax([imp_h, imp_d, imp_a])
        odds_direction = ['H', 'D', 'A'][odds_dir_idx]
        odds_clarity = max(imp_h, imp_d, imp_a) - min(imp_h, imp_d, imp_a)
        
        return (o_h, o_d, o_a, imp_h, imp_d, imp_a, odds_direction, odds_clarity, True)
    
    # ── 单场比赛处理 ──────────────────────────────────────────────────────
    
    def _process_single_match(self, row, proba_row, conf, feat_cov, def_ratio, 
                             odds_map: Dict) -> PredictionResult:
        """
        处理单场比赛的预测和门控打分
        
        这是重构后的核心函数，将原函数的复杂逻辑分解为多个门控函数
        """
        mid = row['match_id']
        p_h, p_d, p_a = proba_row
        
        # 1. 提取赔率信息
        odds_row = odds_map.get(mid)
        o_h, o_d, o_a, imp_h, imp_d, imp_a, odds_direction, odds_clarity, has_odds = \
            self._extract_odds_info(odds_row)
        
        # 2. 模型方向
        model_dir_idx = np.argmax([p_h, p_d, p_a])
        model_direction = ['H', 'D', 'A'][model_dir_idx]
        
        # 3. 赔率降级模式
        odds_fallback = False
        if has_odds and conf < 0.45 and odds_clarity > 0.10:
            odds_fallback = True
            p_h, p_d, p_a = imp_h, imp_d, imp_a
            conf = max(p_h, p_d, p_a)
            model_direction = odds_direction
            feat_cov = 1.0  # 赔率降级: 特征分给满分
        
        # 4. 计算各个门控得分
        gate_scores = self._calculate_all_gates(
            has_odds, conf, feat_cov, odds_clarity,
            model_direction, odds_direction,
            row, mid, o_h, o_d, o_a
        )
        
        # 5. 总分和分级
        total_score = sum(gate_scores.values())
        
        # Gate 5: D抑制
        if model_direction == 'D' and self.config['d_suppression']:
            total_score = min(total_score, self.config['d_max_score'])
        
        # 共识required但无共识，最多B级
        if self.config['consensus_required'] and gate_scores['consensus'] < self.config['consensus_weight']:
            total_score = min(total_score, 69)
        
        tier = self._determine_tier(total_score)
        recommendation = self._generate_recommendation(tier)
        
        # 6. 构建结果对象
        return PredictionResult(
            match_id=mid,
            match_date=str(row['match_date']),
            home_team=str(row['home_team_name']),
            away_team=str(row['away_team_name']),
            league=str(row['league_name']),
            prob_h=float(p_h),
            prob_d=float(p_d),
            prob_a=float(p_a),
            consensus_score=round(gate_scores['consensus'], 1),
            confidence_score=round(conf, 1),
            feature_score=round(gate_scores['feature'], 1),
            odds_clarity_score=round(gate_scores['odds_clarity'], 1),
            total_score=round(total_score, 1),
            tier=tier,
            predicted_result=model_direction,
            recommendation=recommendation,
            odds_h=float(o_h) if o_h else None,
            odds_d=float(o_d) if o_d else None,
            odds_a=float(o_a) if o_a else None,
            odds_fallback=odds_fallback,
            feature_coverage=round(float(feat_cov), 3),
            default_ratio=round(float(def_ratio), 3),
            temporal_lock_score=round(gate_scores['temporal_lock'], 1),
            lock_confidence=round(gate_scores.get('lock_confidence', 0.0), 3),
            handicap_cover_score=round(gate_scores['handicap_cover'], 1),
            handicap_cover_prob=round(gate_scores.get('handicap_cover_prob', 0.5), 3),
            handicap_value_exists=gate_scores.get('handicap_value_exists', 0),
        )
    
    # ── 门控打分系统 ──────────────────────────────────────────────────────
    
    def _calculate_all_gates(self, has_odds, conf, feat_cov, odds_clarity,
                             model_direction, odds_direction,
                             row, mid, o_h, o_d, o_a) -> Dict[str, float]:
        """
        计算所有门控的得分
        
        Returns:
            包含各个门控得分的字典
        """
        # Gate 1: 共识分
        consensus_score = self._calculate_gate1_consensus(
            has_odds, conf, model_direction, odds_direction
        )
        
        # Gate 2: 置信度分
        confidence_score = self._calculate_gate2_confidence(conf)
        
        # Gate 3: 特征质量分
        feature_score = self._calculate_gate3_feature_quality(feat_cov)
        
        # Gate 4: 赔率清晰度分
        odds_clarity_score = self._calculate_gate4_odds_clarity(has_odds, odds_clarity)
        
        # Gate 6: 时序锁检测
        temporal_lock_score, lock_confidence = self._calculate_gate6_temporal_lock(
            row, mid, o_h, o_d, o_a, has_odds
        )
        
        # Gate 7: 让球覆盖检测
        handicap_cover_score, handicap_cover_prob, handicap_value_exists = \
            self._calculate_gate7_handicap_cover(mid)
        
        return {
            'consensus': consensus_score,
            'confidence': confidence_score,
            'feature': feature_score,
            'odds_clarity': odds_clarity_score,
            'temporal_lock': temporal_lock_score,
            'handicap_cover': handicap_cover_score,
            'lock_confidence': lock_confidence,
            'handicap_cover_prob': handicap_cover_prob,
            'handicap_value_exists': handicap_value_exists,
        }
    
    def _calculate_gate1_consensus(self, has_odds, conf, 
                                   model_direction, odds_direction) -> float:
        """Gate 1: 赔率-模型共识打分"""
        weight = self.config['consensus_weight']
        
        # 无赔率时: 置信度≥0.58 视同共识通过
        high_conf_no_odds = (not has_odds) and conf >= 0.58
        
        if has_odds and model_direction == odds_direction:
            return weight
        elif has_odds:
            return 0
        elif high_conf_no_odds:
            return weight
        else:
            return weight * 0.5  # 无赔率, 给一半
    
    def _calculate_gate2_confidence(self, conf) -> float:
        """Gate 2: 置信度打分"""
        weight = self.config['confidence_weight']
        
        if conf >= self.config['confidence_s_threshold']:
            return weight
        elif conf >= self.config['confidence_a_threshold']:
            return weight * 0.8
        elif conf >= self.config['confidence_b_threshold']:
            return weight * 0.5
        else:
            return weight * 0.2
    
    def _calculate_gate3_feature_quality(self, feat_cov) -> float:
        """Gate 3: 特征质量打分"""
        weight = self.config['feature_weight']
        feat_score = min(feat_cov / self.config['feature_s_threshold'], 1.0)
        return feat_score * weight
    
    def _calculate_gate4_odds_clarity(self, has_odds, odds_clarity) -> float:
        """Gate 4: 赔率清晰度打分"""
        if not has_odds:
            return 0
        
        weight = self.config['odds_clarity_weight']
        clarity_ratio = min(odds_clarity / self.config['odds_clarity_s_threshold'], 1.0)
        return clarity_ratio * weight
    
    def _calculate_gate6_temporal_lock(self, row, mid, o_h, o_d, o_a, has_odds) -> Optional[Dict]:
        """
        Gate 6: 时序锁检测 (v2.0 — OTSM 赔率相变分析)
        
        Returns:
            (temporal_lock_score, lock_confidence)
        """
        lock_confidence = 0.0
        temporal_lock_score = 0.0
        
        # 需要开盘赔率数据
        open_odds_row = row.get('open_home') if 'open_home' in row.index else None
        if open_odds_row is None or not has_odds:
            return temporal_lock_score, lock_confidence
        
        try:
            open_h = float(row['open_home']) if pd.notna(row['open_home']) else None
            open_d = float(row['open_draw']) if pd.notna(row['open_draw']) else None
            open_a = float(row['open_away']) if pd.notna(row['open_away']) else None
            
            if open_h and open_d and open_a:
                open_odds = (open_h, open_d, open_a)
                close_odds = (float(o_h), float(o_d), float(o_a))
                lock_confidence = self._compute_temporal_lock(open_odds, close_odds)
                
                # 映射到门控分
                if lock_confidence >= self.config['temporal_lock_s_threshold']:
                    temporal_lock_score = self.config['temporal_lock_weight']
                elif lock_confidence >= self.config['temporal_lock_a_threshold']:
                    temporal_lock_score = self.config['temporal_lock_weight'] * 0.8
                elif lock_confidence >= self.config['temporal_lock_b_threshold']:
                    temporal_lock_score = self.config['temporal_lock_weight'] * 0.5
                else:
                    temporal_lock_score = 0
        except (ValueError, TypeError) as e:
            logger.warning(f"Gate 6 开盘赔率解析失败 (match {mid}): {e}")
        
        return temporal_lock_score, lock_confidence
    
    def _calculate_gate7_handicap_cover(self, mid) -> Optional[Dict]:
        """
        Gate 7: 让球覆盖检测 (v3.0 欧盘版)
        
        Returns:
            (handicap_cover_score, handicap_cover_prob, handicap_value_exists)
        """
        handicap_cover_score = 0.0
        handicap_cover_prob = 0.5
        handicap_value_exists = 0
        
        gate7_config = self.config.get('gate7', {})
        if not gate7_config.get('enabled', True):
            return handicap_cover_score, handicap_cover_prob, handicap_value_exists
        
        try:
            from predictors.handicap_cover_predictor import HandicapCoverPredictor
            if not hasattr(self, '_hcp'):
                self._hcp = HandicapCoverPredictor(db_path=self.db_path)
                self._hcp.initialize()
            
            # 从 odds 表查询 1X2 欧赔
            hc_conn = sqlite3.connect(self.db_path)
            hc_conn.row_factory = sqlite3.Row
            hc_cur = hc_conn.cursor()
            hc_cur.execute(
                'SELECT home_odds, draw_odds, away_odds '
                'FROM odds WHERE match_id=? AND home_odds IS NOT NULL '
                'ORDER BY odds_timestamp ASC LIMIT 1',
                (mid,))
            hc_row = hc_cur.fetchone()
            hc_conn.close()
            
            if hc_row and hc_row['home_odds']:
                result = self._hcp.predict(
                    mid,
                    home_odds=hc_row['home_odds'],
                    draw_odds=hc_row['draw_odds'],
                    away_odds=hc_row['away_odds'],
                )
                if result:
                    handicap_cover_prob = result.cover_probability
                    handicap_value_exists = 1 if result.value_exists else 0
                    
                    # 门控打分
                    hc_weight = gate7_config.get('weight', 10)
                    if handicap_cover_prob >= gate7_config.get('s_threshold', 0.80) and handicap_value_exists:
                        handicap_cover_score = hc_weight
                    elif handicap_cover_prob >= gate7_config.get('a_threshold', 0.65):
                        handicap_cover_score = hc_weight * 0.8
                    elif handicap_cover_prob >= gate7_config.get('b_threshold', 0.55):
                        handicap_cover_score = hc_weight * 0.5
                    else:
                        handicap_cover_score = hc_weight * 0.2
        except (Exception, ImportError) as e:
            logger.debug(f"Gate 7 不可用 (match {mid}): {e}")
        
        return handicap_cover_score, handicap_cover_prob, handicap_value_exists
    
    # ── 辅助方法 ──────────────────────────────────────────────────────────
    
    def _determine_tier(self, total_score: float) -> str:
        """根据总分确定等级"""
        if total_score >= 80:
            return 'S'
        elif total_score >= 70:
            return 'A'
        elif total_score >= 60:
            return 'B'
        else:
            return 'C'
    
    def _generate_recommendation(self, tier: str) -> str:
        """根据等级生成建议"""
        recommendations = {
            'S': f"★ 高把握出击 | 预期准确率80%+",
            'A': f"▲ 中等把握 | 预期准确率65-75%",
            'B': f"○ 一般把握 | 建议观望",
            'C': f"✗ 不推荐 | 把握不足",
        }
        return recommendations.get(tier, "未知等级")
    
    # ── OTSM 时序状态机 ───────────────────────────────────────────────────
    
    def _load_model(self) -> None:
        """加载模型管道"""
        pipeline = joblib.load(self.model_path)
        
        try:
    from ensemble_trainer import EnsembleTrainer
except ImportError:
    from predictors.components.ensemble_trainer import EnsembleTrainer
        self.trainer = EnsembleTrainer.__new__(EnsembleTrainer)
        for k, v in pipeline.items():
            setattr(self.trainer, k, v)
        self.trainer.logger = logging.getLogger('trainer')
        
        if self.trainer.meta_learner is None:
            logger.warning("  模型不含meta_learner, 将使用加权平均fallback")
        else:
            logger.info(f"  Stacking meta-learner已启用 (n_features={getattr(self.trainer.meta_learner, 'n_features_in_', '?')})")
        
        self.model_version = pipeline.get('version', 'unknown')
        self.feature_names = list(self.trainer.feature_names)
        logger.info(f"  模型版本: v{self.model_version}, 特征数: {len(self.feature_names)}")
    
    def _init_otsm(self) -> None:
        """延迟初始化 OTSM 时序状态机"""
        if self._otsm is not None:
            return
        
        try:
            from bookmaker_sim.odds_temporal_sm import OddsTemporalStateMachine, OddsSnapshot
            self._otsm = OddsTemporalStateMachine(db_path=self.db_path)
            self._otsm_thresholds = self._otsm.fit_thresholds(sample_size=30000)
            logger.info(f"  OTSM 时序状态机已初始化")
        except (Exception) as e:
            logger.warning(f"  OTSM 初始化失败 (时序锁Gate将不可用): {e}")
            self._otsm = False
    
    def _compute_temporal_lock(self, open_odds, close_odds) -> float:
        """计算时序锁确信度 (Gate 6)"""
        if open_odds is None or close_odds is None:
            return 0.0
        
        if self._otsm is None:
            self._init_otsm()
        
        if self._otsm is False or self._otsm_thresholds is None:
            return 0.0
        
        try:
            result = self._otsm.infer_single(open_odds, close_odds)
            return result.lock_confidence
        except (Exception):
            return 0.0

    # ── 为了兼容性，保留原有公共API ────────────────────────────────────────
    
    def get_top_picks(self, n: int = 10, min_tier: str = 'B') -> List[PredictionResult]:
        """获取最佳N场预测"""
        all_results = self.predict_upcoming_matches(days_ahead=7)
        
        tier_order = {'S': 0, 'A': 1, 'B': 2, 'C': 3}
        filtered = [r for r in all_results 
                    if tier_order.get(r.tier, 3) <= tier_order.get(min_tier, 3)]
        
        return filtered[:n]
