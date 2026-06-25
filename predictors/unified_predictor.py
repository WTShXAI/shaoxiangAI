"""
FootballAI v4.1 • Unified Predictor
====================================
整合 v4.1 Stacking + VIP Math (λ融合+陷阱检测) 为一统一预测器

v4.1 增强:
  - DrawExpert 信号衰减 ×0.25 (修复过度预测平局)
  - 阈值分类: Draw>0.46 (替代 argmax)
  - Home 类别权重 ×0.90

架构:
    ┌─────────────────────────────────────────┐
    │         UnifiedPredictor v4.1            │
    ├─────────────────────────────────────────┤
    │  [L1] v4.1 Stacking Ensemble             │
    │        LGB + XGB + NN + DrawExpert(0.25×)│
    │        ↓ 原始 1X2 概率                    │
    │  [L2] λ Fusion                           │
    │        模型 λ × 庄家 λ → 融合 λ           │
    │        ↓ 融合概率                          │
    │  [L3] Threshold Classification           │
    │        Draw>0.46 → Draw, else 概率比      │
    │        ↓ 最终预测                          │
    │  [L4] 16-Engine Trap Detection (可选)     │
    │        ↓ 修正后概率                         │
    │  [L5] Confidence + Report                │
    └─────────────────────────────────────────┘

用法:
    from predictors.unified_predictor import UnifiedPredictor
    up = UnifiedPredictor()
    result = up.predict(
        home="英格兰", away="克罗地亚",
        odds_h=1.30, odds_d=5.00, odds_a=8.30
    )
"""
import os, sys, math, logging, warnings
from typing import Dict, Optional, Tuple, Any
from datetime import datetime
import numpy as np

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 修复P0-13: 消除footballAI外部依赖, 项目内components自包含
FOOTBALLAI_ROOT = os.path.join(ROOT, 'predictors', 'components')  # 内部化
sys.path.insert(0, FOOTBALLAI_ROOT)

# ── 加载依赖 (带fallback) ──
_DEPS = {}

def _lazy_import(name, module_path, class_name=None):
    if name in _DEPS:
        return _DEPS[name]
    try:
        mod = __import__(module_path, fromlist=[class_name] if class_name else [])
        obj = getattr(mod, class_name) if class_name else mod
        _DEPS[name] = obj
        return obj
    except Exception as e:
        logger.warning(f"依赖加载失败 {name}: {e}")
        _DEPS[name] = None
        return None


class UnifiedPredictor:
    """
    FootballAI v4.0 统一预测器
    
    整合: SKY v4.0 Stacking + VIP Math Fusion + Trap Detection
    """

    def __init__(
        self,
        model_path: str = None,
        sky_weight: float = 0.55,
        vip_weight: float = 0.45,
        trap_threshold: float = 0.35,
        draw_gate_threshold: float = 0.35,
        enable_trap: bool = False,
        enable_dh: bool = False,
        use_threshold: bool = True,
        enable_jepa: bool = False,
        jepa_weight: float = 1.0,
    ):
        """
        Args:
            model_path: 模型路径 (None=自动搜索 v4.1 > v4.0)
            sky_weight: SKY 通道权重 (v4.1 Stacking)
            vip_weight: VIP Math 通道权重
            trap_threshold: 陷阱检测阈值
            draw_gate_threshold: DrawExpert 门控阈值 (v4.1: 0.35)
            enable_trap: 启用陷阱检测
            enable_dh: 启用数字人引擎
            use_threshold: 使用 v4.1 阈值分类
            enable_jepa: 启用 v5.0 JEPA (默认False=纯v4.1, True=纯JEPA)
            jepa_weight: JEPA权重 (默认1.0=纯JEPA, 验证最优 Acc55.9% F1_D=0.507)
        """
        self.sky_weight = sky_weight
        self.vip_weight = vip_weight
        self.trap_threshold = trap_threshold
        self.draw_gate_threshold = draw_gate_threshold
        self.enable_trap = enable_trap
        self.enable_dh = enable_dh
        self.use_threshold = use_threshold
        self.enable_jepa = enable_jepa
        self.jepa_weight = jepa_weight
        self._jepa_predictor = None
        self.sky_weight = sky_weight
        self.vip_weight = vip_weight
        self.trap_threshold = trap_threshold
        self.draw_gate_threshold = draw_gate_threshold
        self.enable_trap = enable_trap
        self.enable_dh = enable_dh
        self.use_threshold = use_threshold

        # P0 优化: draw_threshold 从 0.46 → 0.32 (网格搜索最优解)
        # 回测验证: 平局F1 0→0.353, MacroF1 0.465→0.507, 准确率仅降3.8pp
        self.draw_threshold = 0.32
        self.ha_gap = 0.0
        self.de_mult = 0.25  # DrawExpert 衰减

        # ── 加载 v4.0 模型 ──
        self.trainer = None
        self._load_model(model_path)

        # ── 加载 VIP 组件 ──
        self.trap_detector = None
        self.digital_human = None
        self._load_vip_components()

        self._ready = self.trainer is not None
        if self._ready:
            logger.info(f"UnifiedPredictor 就绪: v{self.trainer.model_version} | "
                       f"{len(self.trainer.feature_names)}特征 | "
                       f"NN={'✓' if self.trainer.nn_model else '✗'} | "
                       f"DrawExpert={'✓' if self.trainer.draw_expert_model else '✗'}")

    def _load_model(self, model_path=None):
        """加载模型: v4.1 > v4.0 > v3.2"""
        from ensemble_trainer import EnsembleTrainer

        if model_path is None:
            # 自动搜索: 优先 v4.1, 项目内路径优先
            candidates = [
                os.path.join(ROOT, 'models', 'main', 'football_v4.1_production.joblib'),
                os.path.join(ROOT, 'saved_models', 'football_v4.1_production.joblib'),
                os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'football_v4.1_production.joblib'),
                os.path.join(ROOT, 'models', 'main', 'football_v4.0_production.joblib'),
                os.path.join(ROOT, 'saved_models', 'football_v4.0_production.joblib'),
                os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'football_v4.0_production.joblib'),
            ]
            for c in candidates:
                if os.path.exists(c):
                    model_path = c
                    break

        if model_path and os.path.exists(model_path):
            try:
                self.trainer = EnsembleTrainer.load_pipeline(model_path)
                logger.info(f"模型加载: {os.path.basename(model_path)}")

                # 检测 v4.1 配置
                v41 = getattr(self.trainer, 'v41_config', None)
                if v41:
                    logger.info(f"检测到 v4.1 配置: DE×{v41.get('draw_expert_mult','?')}, "
                               f"阈值={v41.get('draw_threshold','?')}")
                    self.de_mult = v41.get('draw_expert_mult', 0.25)
                    # P0: 不使用模型内嵌的0.46阈值, 强制使用网格搜索最优值0.32
                    self.draw_threshold = 0.32
                    self.ha_gap = v41.get('ha_gap', 0.0)
                    self.use_threshold = True
                elif 'v4.1' in model_path or 'v4.1' in os.path.basename(model_path):
                    logger.info("v4.1 模型 (未检测到内嵌配置, 使用默认)")
                return
            except Exception as e:
                logger.error(f"模型加载失败: {e}")

        logger.error("未找到可用模型!")
        self.trainer = None

    def _load_vip_components(self):
        """加载 VIP Math 融合组件"""
        self.trap_detector = None
        self.trap_loaded = False
        self.digital_human = None

        if self.enable_dh:
            try:
                from digital_human import DigitalHuman
                self.digital_human = DigitalHuman(name="Unified-DH")
                logger.info("数字人引擎就绪")
            except Exception as e:
                logger.warning(f"数字人加载失败: {e}")

        if self.enable_trap:
            self._ensure_trap()

    def _ensure_trap(self):
        """懒加载陷阱检测器"""
        if self.trap_loaded:
            return
        try:
            from bookmaker_trap_detector import BookmakerTrapDetector
            self.trap_detector = BookmakerTrapDetector()
            logger.info("陷阱检测器就绪 (16引擎)")
        except Exception as e:
            logger.warning(f"陷阱检测器加载失败: {e}")
        self.trap_loaded = True

    # ════════════════════════════════════════════════════════════
    # λ Fusion (模型λ × 庄家λ)
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _derive_lambda_from_odds(oh: float, od: float, oa: float) -> Tuple[float, float]:
        """从欧赔逆向庄家λ"""
        raw_sum = 1.0 / oh + 1.0 / od + 1.0 / oa
        p_book = np.array([1/(oh*raw_sum), 1/(od*raw_sum), 1/(oa*raw_sum)])
        share_h = p_book[0] / max(p_book[0] + p_book[2], 0.01)

        lo, hi = 0.3, 8.0
        best_t, best_err = 2.7, 1.0
        for _ in range(20):
            mid = (lo + hi) / 2
            lh, la = mid * share_h, mid * (1 - share_h)
            d_pred = UnifiedPredictor._poisson_draw_prob(lh, la)
            err = abs(d_pred - p_book[1])
            if err < best_err:
                best_err, best_t = err, mid
            if d_pred < p_book[1]:
                lo = mid
            else:
                hi = mid
        return max(best_t * share_h, 0.1), max(best_t * (1 - share_h), 0.1)

    @staticmethod
    def _poisson_draw_prob(lam_h: float, lam_a: float) -> float:
        """泊松模型平局概率"""
        ph = np.array([max(np.exp(-lam_h) * lam_h**k / math.factorial(k), 1e-30) for k in range(13)])
        pa = np.array([max(np.exp(-lam_a) * lam_a**k / math.factorial(k), 1e-30) for k in range(13)])
        ph /= ph.sum(); pa /= pa.sum()
        return float(sum(ph[k] * pa[k] for k in range(13)))

    @staticmethod
    def _lambda_to_probs(lam_h: float, lam_a: float) -> np.ndarray:
        """泊松 λ → 1X2 概率"""
        ph = np.array([max(np.exp(-lam_h) * lam_h**k / math.factorial(k), 1e-30) for k in range(13)])
        pa = np.array([max(np.exp(-lam_a) * lam_a**k / math.factorial(k), 1e-30) for k in range(13)])
        ph /= ph.sum(); pa /= pa.sum()
        p_h = sum(ph[i] * sum(pa[:i]) for i in range(1, 13))
        p_d = sum(ph[i] * pa[i] for i in range(13))
        p_a = sum(pa[i] * sum(ph[:i]) for i in range(1, 13))
        total = p_h + p_d + p_a
        return np.array([p_h, p_d, p_a]) / max(total, 1e-10)

    # ════════════════════════════════════════════════════════════
    # 主预测方法
    # ════════════════════════════════════════════════════════════

    def predict(
        self,
        home: str,
        away: str,
        odds_h: float,
        odds_d: float,
        odds_a: float,
        asian_handicap: float = 0.0,
        ou_line: float = 2.5,
        over_water: float = 1.90,
        under_water: float = 1.92,
        open_h: float = 0,
        open_d: float = 0,
        open_a: float = 0,
        match_type: str = 'unknown',
    ) -> Dict[str, Any]:
        """
        统一预测入口

        Args:
            home: 主队名
            away: 客队名
            odds_h/d/a: 欧赔收盘价
            asian_handicap: 亚盘让球 (正=客让)
            ou_line: 大小球盘口
            over_water/under_water: 大小球水位
            open_h/d/a: 欧赔开盘价 (0=未知, 使用收盘价)
        """
        t0 = datetime.now()

        if not self._ready:
            return self._fallback(odds_h, odds_d, odds_a)

        result = {
            'home': home,
            'away': away,
            'odds': {'H': odds_h, 'D': odds_d, 'A': odds_a},
            'timestamp': datetime.now().isoformat(),
            'warnings': [],
        }

        # ── [L1] SKY: v4.0 Stacking 推理 ──
        try:
            sky_probs = self._sky_predict(home, away, odds_h, odds_d, odds_a, 
                                           open_h, open_d, open_a, asian_handicap,
                                           ou_line, over_water, under_water)
        except Exception as e:
            logger.error(f"SKY 通道失败: {e}")
            sky_probs = np.array([0.40, 0.28, 0.32])

        # ── [JEPA] v5.0 World Model ──
        if self.enable_jepa:
            try:
                jepa_probs = self._get_jepa_probs(odds_h, odds_d, odds_a)
                if self.jepa_weight >= 1.0:
                    sky_probs = jepa_probs
                else:
                    sky_probs = sky_probs * (1 - self.jepa_weight) + jepa_probs * self.jepa_weight
            except Exception as e:
                logger.debug(f"JEPA增强跳过: {e}")

        # ── [L2] λ Fusion ──
        try:
            book_lam_h, book_lam_a = self._derive_lambda_from_odds(odds_h, odds_d, odds_a)
            model_probs_1x2 = sky_probs.copy()
            model_lam_h, model_lam_a = self._probs_to_lambda(model_probs_1x2)

            # 融合: 70% model λ + 30% book λ
            fused_lam_h = model_lam_h * 0.7 + book_lam_h * 0.3
            fused_lam_a = model_lam_a * 0.7 + book_lam_a * 0.3
            lambda_probs = self._lambda_to_probs(fused_lam_h, fused_lam_a)

            lambda_info = {
                'model_lam': [float(model_lam_h), float(model_lam_a)],
                'book_lam': [float(book_lam_h), float(book_lam_a)],
                'fused_lam': [float(fused_lam_h), float(fused_lam_a)],
            }
        except Exception as e:
            logger.warning(f"λ融合失败: {e}")
            lambda_probs = sky_probs.copy()
            lambda_info = {'error': str(e)}

        # ── [L3] Trap Detection ──
        trap_level = 'none'
        trap_type = None
        trap_score = 0.0
        trap_correction = np.array([1.0, 1.0, 1.0])

        if self.enable_trap:
            try:
                self._ensure_trap()
                if self.trap_detector:
                    match_data = {
                        'home': home, 'away': away, 'league': match_type or 'unknown',
                        'odds_h': odds_h, 'odds_d': odds_d, 'odds_a': odds_a,
                        'asian_handicap': asian_handicap,
                        'over_under_line': ou_line, 'over_water': over_water, 'under_water': under_water,
                        'water_level': 0.92, 'water_trend': 'stable',
                        'odds_trend': 'stable', 'handicap_change': 'stable',
                        'handicap_change_magnitude': 0, 'multi_bookmaker_sync': True,
                        'squad_quality_change': 0, 'tactical_shift': 0, 'counter_threat_level': 0.5,
                        'score_odds': None, 'score_odds_other': None,
                    }
                    trap_report = self.trap_detector.detect(match_data)
                    if hasattr(trap_report, 'trap_score'):
                        trap_score = float(getattr(trap_report, 'trap_score', 0))
                    elif hasattr(trap_report, 'total_score'):
                        trap_score = float(getattr(trap_report, 'total_score', 0))

                    trap_level = getattr(trap_report, 'level', 'none')
                    trap_type = getattr(trap_report, 'trap_type', None) or getattr(trap_report, 'type', None)

                    if trap_score > self.trap_threshold:
                        direction = getattr(trap_report, 'trap_direction', None)
                        if direction:
                            if 'H' in str(direction):
                                trap_correction = np.array([0.7, 1.1, 1.2])
                            elif 'A' in str(direction):
                                trap_correction = np.array([1.2, 1.1, 0.7])
                            else:
                                trap_correction = np.array([1.0, 1.2, 1.0])
                        result['warnings'].append(f"陷阱检测: {trap_level} score={trap_score:.2f}")
            except Exception as e:
                logger.warning(f"陷阱检测失败: {e}")

        # ── 融合: SKY + λ + Trap ──
        vip_math_probs = lambda_probs * trap_correction
        vip_math_probs = vip_math_probs / vip_math_probs.sum()

        # 加权融合
        final_probs = sky_probs * self.sky_weight + vip_math_probs * (1 - self.sky_weight)
        final_probs = final_probs / final_probs.sum()

        # ── [L4] DrawGate v5.3: 平局专用识别 (D-Gate + DrawExpert 合并) ──
        draw_signal = 0.0
        risk_tag = "clean"
        dgate_mode = "none"
        draw_threshold_eff = self.draw_threshold  # 默认0.32

        # Step 1: 获取 DrawExpert 信号 (如可用)
        if self.trainer.draw_expert_model:
            try:
                draw_signal = self._get_draw_expert_signal(home, away, odds_h, odds_d, odds_a,
                                                           asian_handicap, ou_line)
                # v5.3: 线性ramp校准 (0.26→0.25x, 0.42→0.95x)
                if draw_signal > 0:
                    if draw_signal <= 0.26:
                        draw_signal *= 0.25
                    elif draw_signal >= 0.42:
                        draw_signal *= 0.95
                    else:
                        t = (draw_signal - 0.26) / 0.16
                        draw_signal *= 0.25 + t * 0.70
                draw_signal = draw_signal * self.de_mult  # v4.1 衰减
            except Exception:
                draw_signal = 0.0

        # Step 2: DrawGate v5.3 检测
        try:
            imp_h = 1.0/odds_h / (1.0/odds_h + 1.0/odds_d + 1.0/odds_a)
            imp_d = 1.0/odds_d / (1.0/odds_h + 1.0/odds_d + 1.0/odds_a)
            imp_a = 1.0/odds_a / (1.0/odds_h + 1.0/odds_d + 1.0/odds_a)

            from rules.drawgate_v53 import apply_drawgate, detect_match_type
            dg = apply_drawgate(
                imp_h, imp_d, imp_a,
                odds={'home': odds_h, 'draw': odds_d, 'away': odds_a},
                handicap=asian_handicap, ou_line=ou_line,
                match_type=detect_match_type(match_type or ''),
                draw_expert_signal=draw_signal if draw_signal > 0 else None,
            )

            risk_tag = dg['risk_tag']
            dgate_mode = dg['dgate_mode']
            draw_threshold_eff = dg['draw_threshold_adj']

            # Step 3: 置信度衰减 — 强队降权
            if dg['confidence_mult'] < 1.0:
                strong_idx = 0 if imp_h >= imp_a else 2
                final_probs[strong_idx] *= dg['confidence_mult']

            # Step 4: DrawExpert boost — 平局抬权
            if dg['draw_boost'] > 0:
                de_boost = min(dg['draw_boost'], 0.12)
                final_probs[1] += de_boost

            # Step 5: DrawExpert 原始信号辅助 boost (若前两步都触发)
            if draw_signal > 0.30 and risk_tag != 'clean':
                final_probs[1] += min(draw_signal * 0.10, 0.05)

            final_probs = final_probs / final_probs.sum()

            if dgate_mode != 'none':
                result['warnings'].append(
                    f"DrawGate[{dgate_mode}] {risk_tag}: "
                    f"阈值{self.draw_threshold:.2f}→{draw_threshold_eff:.2f} "
                    f"置信度×{dg['confidence_mult']:.2f} "
                    f"DEboost=+{dg['draw_boost']:.3f}"
                )

        except Exception as e:
            logger.debug(f"DrawGate跳过: {e}")
            # 降级: 旧版 DrawExpert 弱信号提升
            if draw_signal > self.draw_gate_threshold:
                boost = min(draw_signal * 0.15, 0.08)
                final_probs[1] += boost
                final_probs = final_probs / final_probs.sum()

        # ── [L5] 最终输出 (v4.1 阈值分类, v5.3 动态阈值) ──
        if self.use_threshold:
            p_h, p_d, p_a = final_probs[0], final_probs[1], final_probs[2]
            if p_d > draw_threshold_eff:
                prediction = 'D'
            elif p_h > p_a + self.ha_gap:
                prediction = 'H'
            else:
                prediction = 'A'
            method = f'threshold(D>{draw_threshold_eff:.2f})'
        else:
            prediction = ['H', 'D', 'A'][int(np.argmax(final_probs))]
            method = 'argmax'

        confidence = float(final_probs.max() / final_probs.sum())

        # 进球预测
        goal_pred = self._predict_goals(final_probs, odds_h, odds_d, odds_a,
                                        over_water, under_water, ou_line)

        elapsed = (datetime.now() - t0).total_seconds() * 1000

        result.update({
            'probabilities': {
                'H': round(float(final_probs[0]), 4),
                'D': round(float(final_probs[1]), 4),
                'A': round(float(final_probs[2]), 4),
            },
            'prediction': prediction,
            'method': method,
            'confidence': round(confidence, 4),
            'trap_level': trap_level,
            'trap_type': trap_type,
            'trap_score': round(trap_score, 4),
            'draw_signal': round(draw_signal, 4),
            'risk_tag': risk_tag,
            'dgate_mode': dgate_mode,
            'draw_threshold_eff': round(draw_threshold_eff, 4),
            'lambda_fusion': lambda_info,
            'channel_breakdown': {
                'sky': [round(float(x), 4) for x in sky_probs],
                'vip_math': [round(float(x), 4) for x in vip_math_probs],
                'final': [round(float(x), 4) for x in final_probs],
                'weights': {'sky': self.sky_weight, 'vip_math': round(1 - self.sky_weight, 2)},
            },
            'goal_prediction': goal_pred,
            'elapsed_ms': round(elapsed, 1),
        })

        return result

    def _sky_predict(
        self, home: str, away: str, oh: float, od: float, oa: float,
        open_h: float = 0, open_d: float = 0, open_a: float = 0,
        asian_handicap: float = 0, ou_line: float = 2.5,
        over_water: float = 1.90, under_water: float = 1.92,
    ) -> np.ndarray:
        """SKY 通道: v4.1 Stacking — 使用 FeatureAligner 构建72维特征 (P2 解耦)"""
        # P2: 使用 FeatureAligner 统一构建特征 (与 DrawExpert/NN 共用)
        try:
            from features.feature_aligner import FeatureAligner
        except ImportError:
            from feature_aligner import FeatureAligner
        aligner = FeatureAligner.from_trainer(self.trainer)
        vec = aligner.build(
            home=home, away=away, oh=oh, od=od, oa=oa,
            asian_handicap=asian_handicap, ou_line=ou_line,
            over_water=over_water, under_water=under_water,
            open_h=open_h, open_d=open_d, open_a=open_a,
        )

        # 获取原始特征字典 (用于覆盖率检测)
        feat_vals = aligner.build_raw_dict(
            oh, od, oa, asian_handicap, ou_line,
            over_water, under_water, open_h, open_d, open_a,
        )

        X = vec.reshape(1, -1)

        # v4.7: 冷启动检测 — 特征覆盖率<60%时旁路meta-learner
        # 根因: meta-learner在55/72维=默认值时, 回归训练先验(主场偏置)
        # 基模型(XGB/LGB)在部分特征下仍能可靠预测(已验证XGB正确识别平局)
        coverage = feat_vals.get('feat_coverage_ratio', 0.35)
        if coverage < 0.60:
            try:
                # 基模型加权平均: XGB(抗干扰强)×0.45 + LGB×0.35 + DrawExpert×0.20
                proba_xgb_raw = self.trainer.xgb_model.predict_proba(X)[0]
                proba_lgb_raw = self.trainer.lgb_model.predict_proba(X)[0]
                # DrawExpert P(Draw) → 扩展为3维
                # 修复P0-14: 原硬编码[0.33,0.34,0.33]注入0.068恒定平局偏置
                # 改为等权中性值, DrawExpert可用时再用真实信号覆盖
                de_signal = np.array([1/3, 1/3, 1/3])
                if self.trainer.draw_expert_model:
                    try:
                        de_p = self.trainer.draw_expert_model.predict_proba(X)
                        if de_p.shape[1] == 2:  # 二分类: [P(not draw), P(draw)]
                            de_d = float(de_p[0, 1])
                            # v5.3: 线性ramp校准 (无悬崖, 中高区保留)
                            # 0.26→0.065 0.34→0.204 0.40→0.345 0.42→0.399
                            if de_d <= 0.26:
                                de_d *= 0.25
                            elif de_d >= 0.42:
                                de_d *= 0.95
                            else:
                                t = (de_d - 0.26) / 0.16
                                de_d *= 0.25 + t * 0.70
                            de_signal = np.array([(1-de_d)*0.5, de_d, (1-de_d)*0.5])
                    except Exception as de_err:
                        logger.warning(f"[SKY冷启动] DrawExpert不可用, 使用中性等权: {de_err}")
                else:
                    logger.debug("[SKY冷启动] DrawExpert模型未加载, 使用中性等权信号")
                # 加权融合
                proba = proba_xgb_raw * 0.45 + proba_lgb_raw * 0.35 + de_signal * 0.20
                proba = proba / proba.sum()
                logger.info(f"[SKY冷启动] cov={coverage:.0%} XGB={proba_xgb_raw[0]:.2f}/{proba_xgb_raw[1]:.2f} LGB={proba_lgb_raw[0]:.2f}/{proba_lgb_raw[1]:.2f} DE={de_signal[1]:.2f} → final={proba[0]:.3f}/{proba[1]:.3f}/{proba[2]:.3f}")
                return proba
            except Exception:
                pass  # 降级到标准路径

        try:
            proba = self.trainer.ensemble_predict_proba(X)
        except Exception:
            return np.array([0.40, 0.28, 0.32])

        proba = proba[0] / proba[0].sum()
        return proba

    def _get_draw_expert_signal(
        self, home: str, away: str, oh: float, od: float, oa: float,
        asian_handicap: float = 0.0, ou_line: float = 2.5,
    ) -> float:
        """DrawExpert P(Draw) 信号 (P2: 使用 FeatureAligner 统一构建 72 维特征)"""
        # P2 修复: 优先使用 trainer 内置的 draw_expert_model (已对齐)
        if self.trainer and self.trainer.draw_expert_model:
            try:
                try:
                    from features.feature_aligner import FeatureAligner
                except ImportError:
                    from feature_aligner import FeatureAligner
                aligner = FeatureAligner.from_trainer(self.trainer)
                vec = aligner.build(
                    home=home, away=away, oh=oh, od=od, oa=oa,
                    asian_handicap=asian_handicap, ou_line=ou_line,
                )
                # DrawExpert 是二分类: [P(not draw), P(draw)]
                de_p = self.trainer.draw_expert_model.model.predict_proba(vec.reshape(1, -1))
                return float(de_p[0, 1])
            except Exception as e:
                logger.warning(f"DrawExpert (trainer内置) 失败, 尝试独立加载: {e}")

        # Fallback: 独立加载 DrawExpert
        from draw_expert import DrawExpert
        import joblib

        de_path = os.path.join(ROOT, 'models', 'draw_expert', 'draw_expert_v1.joblib')
        if not os.path.exists(de_path):
            de_path = os.path.join(ROOT, 'saved_models', 'draw_expert_v1.joblib')
        if not os.path.exists(de_path):
            de_path = os.path.join(FOOTBALLAI_ROOT, 'saved_models', 'draw_expert_v1.joblib')

        if os.path.exists(de_path):
            try:
                de = DrawExpert.load(de_path)
                # P2: 使用 FeatureAligner 构建完整 72 维特征 (原来只传 5 维 → 恒定输出 0.331)
                try:
                    from features.feature_aligner import FeatureAligner
                except ImportError:
                    from feature_aligner import FeatureAligner
                if self.trainer:
                    aligner = FeatureAligner.from_trainer(self.trainer)
                else:
                    # 从独立 DE 模型加载特征名
                    de_state = joblib.load(de_path)
                    aligner = FeatureAligner()
                    aligner.feature_names = de_state.get('feature_names', [])
                    aligner.scaler = None  # 独立 DE 无 scaler

                vec = aligner.build(
                    home=home, away=away, oh=oh, od=od, oa=oa,
                    asian_handicap=asian_handicap, ou_line=ou_line,
                )
                # DrawExpert.predict_proba 返回 (n, 1)
                p = de.predict_proba(vec.reshape(1, -1))
                return float(p[0, 0])
            except Exception as e:
                logger.warning(f"DrawExpert 独立加载也失败: {e}")
        return 0.0

    def _predict_goals(
        self, probs: np.ndarray, oh: float, od: float, oa: float,
        over_water: float, under_water: float, ou_line: float
    ) -> Dict:
        """进球预测"""
        # 从概率和赔率估算进球
        imp_sum = 1/oh + 1/od + 1/oa
        market_h = (1/oh) / imp_sum * max(oh, 1.5)
        market_a = (1/oa) / imp_sum * max(oa, 1.5)

        # 模型期望进球
        total_lam = market_h + market_a
        # 大小球水位反推
        if over_water > 0 and under_water > 0:
            ou_lam = ou_line * (1 + (over_water - under_water) / max(over_water + under_water, 0.01) * 2)
        else:
            ou_lam = ou_line

        # 融合
        final_total = total_lam * 0.5 + ou_lam * 0.5
        home_goals = final_total * probs[0] / max(probs[0] + probs[2], 0.01)
        away_goals = final_total - home_goals

        return {
            'home': round(float(home_goals), 2),
            'away': round(float(away_goals), 2),
            'total': round(float(final_total), 2),
            'ou_prediction': 'Over' if final_total > ou_line else 'Under',
        }

    @staticmethod
    def _probs_to_lambda(probs: np.ndarray) -> Tuple[float, float]:
        """1X2概率 → λ值"""
        p_h, p_d, p_a = probs[0], probs[1], probs[2]
        share = p_h / max(p_h + p_a, 0.01)
        lo, hi = 0.3, 8.0
        best_t, best_err = 2.5, 1.0
        for _ in range(20):
            mid = (lo + hi) / 2
            lh, la = mid * share, mid * (1 - share)
            d_pred = UnifiedPredictor._poisson_draw_prob(lh, la)
            err = abs(d_pred - p_d)
            if err < best_err:
                best_err, best_t = err, mid
            if d_pred < p_d:
                lo = mid
            else:
                hi = mid
        return max(best_t * share, 0.1), max(best_t * (1 - share), 0.1)

    def _get_jepa_probs(self, odds_h, odds_d, odds_a) -> np.ndarray:
        """Get JEPA world model probabilities (lazy init, cached)"""
        if self._jepa_predictor is None:
            from predictors.jepa_predictor import quick_predict
            self._jepa_predictor = quick_predict
        
        result = self._jepa_predictor('', '', '', odds_h, odds_d, odds_a)
        return result['jepa_probs']

    def _fallback(self, oh: float, od: float, oa: float) -> Dict:
        """模型未就绪时的退化预测"""
        imp_sum = 1/oh + 1/od + 1/oa
        return {
            'probabilities': {
                'H': round(1/(oh*imp_sum), 4),
                'D': round(1/(od*imp_sum), 4),
                'A': round(1/(oa*imp_sum), 4),
            },
            'prediction': 'H',
            'confidence': 0.33,
            'warnings': ['模型未就绪, 使用赔率隐含概率'],
            'elapsed_ms': 0,
        }


# ── 便捷工厂 ──
_unified_instance = None

def get_unified_predictor() -> UnifiedPredictor:
    """获取全局 UnifiedPredictor 单例"""
    global _unified_instance
    if _unified_instance is None:
        _unified_instance = UnifiedPredictor()
    return _unified_instance


# ── CLI 测试 ──
if __name__ == '__main__':
    print("=" * 60)
    print("  UnifiedPredictor v1.0 • 测试")
    print("=" * 60)

    up = UnifiedPredictor()
    if not up._ready:
        print("❌ 模型未就绪")
        sys.exit(1)

    # 测试1: 英格兰 vs 克罗地亚 (6.18 实际 H)
    r1 = up.predict(
        home="英格兰", away="克罗地亚",
        odds_h=1.30, odds_d=5.00, odds_a=8.30,
        match_type="world_cup"
    )
    print(f"\n{'英格兰 vs 克罗地亚':^60}")
    print(f"  赔率: H=1.30 D=5.00 A=8.30")
    print(f"  预测: {r1['prediction']} | 置信度: {r1['confidence']:.3f}")
    print(f"  概率: H={r1['probabilities']['H']:.3f} D={r1['probabilities']['D']:.3f} A={r1['probabilities']['A']:.3f}")
    print(f"  通道: SKY={r1['channel_breakdown']['sky']} | VIP={r1['channel_breakdown']['vip_math']}")
    print(f"  陷阱: {r1['trap_level']} ({r1['trap_type'] or '无'})")
    print(f"  Draw信号: {r1['draw_signal']:.3f}")
    if r1['warnings']:
        for w in r1['warnings']:
            print(f"  ⚠️ {w}")
    print(f"  耗时: {r1['elapsed_ms']:.0f}ms")

    # 测试2: 澳大利亚 vs 土耳其 (6.14 实际 H)
    r2 = up.predict(
        home="澳大利亚", away="土耳其",
        odds_h=4.55, odds_d=3.35, odds_a=1.76,
        match_type="world_cup"
    )
    print(f"\n{'澳大利亚 vs 土耳其':^60}")
    print(f"  赔率: H=4.55 D=3.35 A=1.76")
    print(f"  预测: {r2['prediction']} | 置信度: {r2['confidence']:.3f}")
    print(f"  概率: H={r2['probabilities']['H']:.3f} D={r2['probabilities']['D']:.3f} A={r2['probabilities']['A']:.3f}")
    if r2['warnings']:
        for w in r2['warnings']:
            print(f"  ⚠️ {w}")

    # 测试3: 卡塔尔 vs 瑞士 (6.14 实际 D)
    r3 = up.predict(
        home="卡塔尔", away="瑞士",
        odds_h=5.60, odds_d=3.75, odds_a=1.61,
        match_type="world_cup"
    )
    print(f"\n{'卡塔尔 vs 瑞士':^60}")
    print(f"  赔率: H=5.60 D=3.75 A=1.61  (实际: D)")
    print(f"  预测: {r3['prediction']} | {'✅' if r3['prediction']=='D' else '❌'} | 概率: D={r3['probabilities']['D']:.3f}")

    print(f"\n{'='*60}")
    print(f"  ✅ UnifiedPredictor 就绪")
