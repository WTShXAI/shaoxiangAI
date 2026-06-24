"""
哨响AI v5.0 — JEPA World Model Unified Predictor
==================================================
整合 JEPA 世界模型与 v4.1 Stacking 的单入口预测器。

Phase 1: JEPA作为第6基模型，轻量集成
Phase 2: JEPA升级为Meta决策引擎 (MoE门控，条件触发)

使用方法:
    predictor = V5Predictor()
    result = predictor.predict(
        static_features=np.array([...]),     # (72,)
        match_sequences=np.array([...]),     # (10, 32) or None
        odds_drift=np.array([...]),          # (8, 24) or None
        match_context={'league': 'World Cup', ...}
    )
"""

import numpy as np
from pathlib import Path
from typing import Dict, Optional, Tuple
import logging

logger = logging.getLogger("V5.Predictor")


class V5Predictor:
    """
    v5.0 统一预测器 — JEPA World Model + v4.1 Stacking
    
    三级降级:
      FULL:       JEPA + 5基模型 (Phase 1: JEPA作为第6基模型)
      MODEL_ONLY: JEPA only (无序列/漂移数据时)
      FALLBACK:   v4.1 Stacking (无v5模型时)
    """
    
    def __init__(
        self,
        model_path: Optional[str] = None,
        config_path: Optional[str] = None,
        device: str = 'cpu',
    ):
        self.device = device
        self.model_path = model_path
        
        # Default model: JEPALite (167K, 272K trained, Acc55.9% F1_D=0.507)
        if not model_path:
            default = Path('D:/Architecture v4.0/models/jepa/checkpoints/best_model_lite.pt')
            if default.exists():
                self.model_path = str(default)
            else:
                fallback = Path('D:/Architecture v4.0/models/jepa/checkpoints/best_model.pt')
                if fallback.exists():
                    self.model_path = str(fallback)
        
        self._jepa_adapter = None
        self._v4_predictor = None
        self._initialized = False
    
    def _ensure_init(self):
        if self._initialized:
            return
        
        from predictors.jepa_adapter import JEPAAdapter
        self._jepa_adapter = JEPAAdapter(
            model_path=self.model_path,
            device=self.device,
        )
        
        # Try to load v4.1 predictor
        try:
            import sys
            sys.path.insert(0, str(Path('D:/Architecture v4.0')))
            from predictors.unified_predictor import UnifiedPredictor
            self._v4_predictor = UnifiedPredictor()
            logger.info("v4.1 UnifiedPredictor loaded")
        except Exception as e:
            logger.warning(f"v4.1 predictor unavailable: {e}")
        
        self._initialized = True
    
    def predict(
        self,
        static_features: np.ndarray,
        match_sequences: Optional[np.ndarray] = None,
        odds_drift: Optional[np.ndarray] = None,
        match_context: Optional[Dict] = None,
        n_paths: int = 50,
    ) -> Dict:
        """
        主预测接口
        
        Returns:
            {
                'probabilities': np.array([P(H), P(D), P(A)]),
                'prediction': 'home'|'draw'|'away',
                'confidence': float,
                'source': 'jepa+v4'|'jepa_only'|'v4_fallback',
                'jepa_probs': np.array([...]),   # JEPA世界模型概率
                'stacking_probs': np.array([...]), # Stacking概率 (if available)
                'draw_signal': str,              # WEAK|VIABLE|STRONG|NONE
            }
        """
        self._ensure_init()
        
        # ── JEPA 预测 ──
        jepa_probs = self._jepa_adapter.predict_proba(
            static_features, match_sequences, odds_drift, n_paths=n_paths
        )
        if jepa_probs.ndim == 2:
            jepa_probs = jepa_probs[0]
        
        # ── v4.1 Stacking 预测 (if available) ──
        stacking_probs = None
        if self._v4_predictor:
            try:
                # Build minimal features for v4.1
                from core.context import MatchContext
                ctx = MatchContext()
                if match_context:
                    ctx.home_team = match_context.get('home_team', '')
                    ctx.away_team = match_context.get('away_team', '')
                    ctx.league = match_context.get('league', '')
                
                # Use odds from static features
                ho = static_features[0] * 20  # denormalize
                do = static_features[1] * 20
                ao = static_features[2] * 20
                
                # Try to get v4.1 prediction
                v4_result = self._v4_predictor._sky_predict(
                    home_odds=ho, draw_odds=do, away_odds=ao,
                    handicap=0.0, ou_line=2.5,
                    home_team=ctx.home_team, away_team=ctx.away_team,
                    league_name=ctx.league,
                )
                stacking_probs = np.array([
                    v4_result.get('proba_home', 0.33),
                    v4_result.get('proba_draw', 0.33),
                    v4_result.get('proba_away', 0.33),
                ])
            except Exception as e:
                logger.debug(f"v4.1 prediction failed: {e}")
        
        # ── 融合 (Phase 1: 加权平均) ──
        if stacking_probs is not None:
            # JEPA权重 0.08 (Phase 1保守), Stacking 0.92
            w_jepa = 0.08
            fused = w_jepa * jepa_probs + (1 - w_jepa) * stacking_probs
            source = 'jepa+v4'
        else:
            fused = jepa_probs
            source = 'jepa_only'
        
        # ── 判型 (阈值 0.32, 与v4.1一致) ──
        prob_h, prob_d, prob_a = fused
        draw_threshold = 0.32
        
        if prob_d >= draw_threshold:
            prediction = 'draw'
            draw_signal = 'STRONG' if prob_d > 0.35 else 'VIABLE'
        else:
            idx = np.argmax(fused)
            prediction = ['home', 'draw', 'away'][idx]
            draw_signal = 'WEAK' if prob_d > 0.20 else 'NONE'
        
        return {
            'probabilities': fused,
            'prediction': prediction,
            'confidence': float(np.max(fused)),
            'source': source,
            'jepa_probs': jepa_probs,
            'stacking_probs': stacking_probs,
            'draw_signal': draw_signal,
        }


def quick_predict(home_team: str, away_team: str, league: str = '',
                  home_odds: float = 2.0, draw_odds: float = 3.5, away_odds: float = 3.0,
                  handicap: float = 0.0, ou_line: float = 2.5) -> Dict:
    """
    快速预测接口 — 只需要队名和赔率
    
    示例:
        result = quick_predict('突尼斯', '日本', 'World Cup', 4.90, 3.45, 1.69)
    """
    # 构建简化特征
    imp = 1/home_odds + 1/draw_odds + 1/away_odds
    static = np.zeros(72, dtype=np.float32)
    static[0:3] = [home_odds/20, draw_odds/20, away_odds/20]
    static[3:6] = [(1/home_odds)/imp, (1/draw_odds)/imp, (1/away_odds)/imp]
    static[6] = imp
    static[7] = 1/home_odds - 1/away_odds  # odds diff
    
    ctx = {
        'home_team': home_team,
        'away_team': away_team,
        'league': league,
    }
    
    predictor = V5Predictor()
    return predictor.predict(static, match_context=ctx, n_paths=30)


if __name__ == '__main__':
    print("=" * 50)
    print("V5Predictor Smoke Test")
    print("=" * 50)
    
    result = quick_predict('突尼斯', '日本', 'World Cup', 4.90, 3.45, 1.69)
    
    print(f"\n突尼斯 vs 日本 (World Cup)")
    print(f"Odds: 4.90 / 3.45 / 1.69")
    print(f"Prediction: {result['prediction'].upper()}")
    print(f"Probabilities: H={result['probabilities'][0]:.3f} D={result['probabilities'][1]:.3f} A={result['probabilities'][2]:.3f}")
    print(f"Source: {result['source']}")
    print(f"Draw signal: {result['draw_signal']}")
    print(f"Confidence: {result['confidence']:.3f}")
    
    if result['stacking_probs'] is not None:
        print(f"\nJEPA only: H={result['jepa_probs'][0]:.3f} D={result['jepa_probs'][1]:.3f} A={result['jepa_probs'][2]:.3f}")
        print(f"Stacking:  H={result['stacking_probs'][0]:.3f} D={result['stacking_probs'][1]:.3f} A={result['stacking_probs'][2]:.3f}")
    
    print("\n✅ Smoke test passed")
