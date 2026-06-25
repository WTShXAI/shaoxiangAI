"""
预测服务 — 封装 EnsembleTrainer / ModelBridge 调用 + 泊松比分 + 大小球 + 融合 + 收割防护
"""
import sys
import os
import time
import logging
import sqlite3
from typing import Optional, List, Tuple, Dict, Any
from datetime import datetime

import numpy as np
import pandas as pd
import sqlalchemy
from sqlalchemy import create_engine, text

from utils.constants import DEFAULT_DRAW_PROB
from sqlalchemy.exc import SQLAlchemyError
from typing import Optional, Dict

logger = logging.getLogger(__name__)

def _safe_float(v, default=0.0):
    """安全转float — None/NaN/非数字 → default，不抛异常"""
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default

def _safe_int(v, default=0):
    """安全转int — None/NaN/非数字 → default，不抛异常"""
    if v is None:
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default

# ── HeuristicPredictor (P0-2: 挂入预测管线) ──
try:
    from agents.heuristic_predictor import HeuristicPredictor
    _heuristic_predictor = HeuristicPredictor()
    _heuristic_available = True
except Exception as _e:
    logger.warning(f"HeuristicPredictor 导入失败: {_e}")
    _heuristic_predictor = None
    _heuristic_available = False

# ── BookmakerBayesInfer (P1: 赔率→贝叶斯先验注入) ──
try:
    from bookmaker_sim.margin_likelihood_bridge import BookmakerBayesInfer, BayesInferResult
    _bayes_infer = BookmakerBayesInfer()
    _bayes_available = True
except Exception as _be:
    logger.warning(f"BookmakerBayesInfer 导入失败: {_be}")
    _bayes_infer = None
    _bayes_available = False

# ── 收割防护墙 ──
try:
    from bookmaker_sim.harvesting_guard import HarvestingGuard, HarvestingReport
    _guard = HarvestingGuard()
    _guard_available = True
except Exception as e:
    logger.warning(f"HarvestingGuard 不可用 (收菜检测关闭): {type(e).__name__}: {e}")
    _guard_available = False
    _guard = None

# ── 操盘手陷阱检测器 (P2: 反诱盘) ──
try:
    from bookmaker_sim.bookmaker_trap_detector import BookmakerTrapDetector
    _trap_detector = BookmakerTrapDetector()
    _trap_available = True
except Exception as _te:
    logger.warning(f"BookmakerTrapDetector 导入失败: {_te}")
    _trap_detector = None
    _trap_available = False

# ── VIP-2 预测器 (12场批量回测 + 葡萄牙实时验证通过) ──
try:
    from vip_2_predictor import VIP2Predictor
    _vip_predictor = VIP2Predictor()
    _vip_available = True
    try:
        from core.model_registry_helper import get_active_model_version
    except ImportError:
        from backend.core.model_registry_helper import get_active_model_version
    _active_ver = get_active_model_version()
    logger.info(f"[VIP-2] VIP2Predictor 初始化完成 ({_active_ver}模型 + 16引擎陷阱检测)")
except Exception as _ve:
    logger.warning(f"VIP2Predictor 导入失败: {_ve}")
    _vip_predictor = None
    _vip_available = False


class PredictionService:
    """预测服务（桥接 ModelBridge + 泊松比分 + 大小球 + 融合方案）"""

    # 进程级单例：仅在模型文件变更时重新加载
    _model = None
    _model_path = None
    _model_mtime = 0

    # 融合权重配置
    FUSION_ODDS_WEIGHT = 0.70   # 赔率隐含概率权重
    FUSION_MODEL_WEIGHT = 0.30  # 模型概率权重
    OVER_UNDER_LINE = 2.5       # 大小球标准盘口

    @property
    def model(self) -> Optional[Dict]:
        """延迟加载模型（基于 mtime 检测文件变更）"""
        cls = type(self)
        if cls._model is not None and cls._model_path:
            try:
                current_mtime = os.path.getmtime(cls._model_path)
                if current_mtime == cls._model_mtime:
                    return cls._model
            except OSError:
                pass
        self._load_model()
        return cls._model

    def _load_model(self) -> None:
        """P0-1: 通过 ModelBridge 加载模型（禁止直接 joblib.load 绕过）"""
        try:
            from agents.model_bridge import get_model_bridge
            bridge = get_model_bridge()
            if bridge.available:
                self.__class__._model = bridge
                self.__class__._model_path = bridge.model_name
                self.__class__._model_mtime = 0  # ModelBridge 自管理
                logger.info(f"[P0-1] 模型已通过 ModelBridge 加载: {bridge.model_name}")
            else:
                logger.warning("ModelBridge 模型不可用")
                self.__class__._model = None
                self.__class__._model_mtime = 0
        except (ValueError, KeyError, FileNotFoundError) as e:
            logger.error(f"ModelBridge 初始化失败: {e}")
            self.__class__._model = None
            self.__class__._model_mtime = 0

    def get_model_version(self) -> str:
        """获取当前模型版本"""
        if self._model_path:
            return os.path.basename(self._model_path).replace(".joblib", "")
        return "unknown"

    # ════════════════════════════════════════════════════════════════
    # VIP-2 预测通道
    # ════════════════════════════════════════════════════════════════

    def _predict_with_vip(
        self, home_team: str, away_team: str,
        league: Optional[str] = None,
        custom_odds: Optional[Dict[str, float]] = None,
        vip_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        """
        VIP-2 预测通道: 三层融合(λ+16引擎陷阱+双通道投票) + 比分预测

        仅在 vip_enabled=True 且有可用赔率数据时调用。
        使用已验证通过的组件(活跃模型、λ融合、16引擎陷阱检测、陷阱→概率桥、
        RP降噪、比分分段修正)，避开已删除的失败组件(v3.3/v3.4/OddsExpert solo)。

        Args:
            home_team: 主队名
            away_team: 客队名
            league: 联赛名
            custom_odds: 自定义赔率 {home, draw, away, asian_handicap, ...}
            vip_context: VIP 上下文 {tactical_shift, squad_change, match_type, ...}

        Returns:
            预测结果 dict 或 None (降级到标准流程)
        """
        if not _vip_available or _vip_predictor is None:
            return None

        try:
            # ── Step 1: 构建VIP match输入 ──
            vip_match = {
                'home': home_team,
                'away': away_team,
                'league': league or '其他',
            }

            # 赔率数据 (优先 custom_odds, 其次从数据库获取)
            if custom_odds:
                vip_match['odds_h'] = _safe_float(custom_odds.get('home') or custom_odds.get('home_win'), 2.0)
                vip_match['odds_d'] = _safe_float(custom_odds.get('draw'), 3.5)
                vip_match['odds_a'] = _safe_float(custom_odds.get('away') or custom_odds.get('away_win'), 4.0)
                vip_match['asian_handicap'] = custom_odds.get('asian_handicap')
                # 可选增强字段
                vip_match['water_level'] = _safe_float(custom_odds.get('water_level'), 0.92)
                vip_match['water_trend'] = custom_odds.get('water_trend', 'stable')
                vip_match['odds_trend'] = custom_odds.get('odds_trend', 'stable')
                vip_match['handicap_change'] = custom_odds.get('handicap_change', 'stable')
                vip_match['handicap_change_magnitude'] = _safe_float(custom_odds.get('handicap_change_magnitude'), 0)
                vip_match['score_odds'] = custom_odds.get('score_odds')
                vip_match['ou_line'] = custom_odds.get('ou_line') or custom_odds.get('over_under_line')
                vip_match['under_water'] = custom_odds.get('under_water')
                vip_match['over_water'] = custom_odds.get('over_water')
                vip_match['score_odds_other'] = custom_odds.get('score_odds_other')
                vip_match['rp_level'] = _safe_float(custom_odds.get('rp_level'), 0)
            else:
                # 从数据库获取赔率
                odds_data = self._get_odds_1x2(home_team, away_team)
                if not odds_data:
                    return None
                vip_match['odds_h'] = odds_data['home']
                vip_match['odds_d'] = odds_data['draw']
                vip_match['odds_a'] = odds_data['away']
                # 尝试获取亚盘和大/小球
                ah = self._get_odds_ah(home_team, away_team)
                if ah:
                    vip_match['asian_handicap'] = ah.get('line')
                totals = self._get_odds_totals(home_team, away_team)
                if totals:
                    vip_match['ou_line'] = totals.get('line')
                    vip_match['over_water'] = totals.get('over')
                    vip_match['under_water'] = totals.get('under')

            # 战术上下文
            if vip_context:
                vip_match['tactical_shift'] = _safe_float(vip_context.get('tactical_shift'), 0)
                vip_match['squad_change'] = _safe_float(vip_context.get('squad_change'), 0)
                vip_match['is_final'] = bool(vip_context.get('is_final', False))
                vip_match['match_type'] = vip_context.get('match_type', 'league')
                vip_match['strength_gap'] = vip_context.get('strength_gap', 'normal')
                vip_match['counter_threat_level'] = _safe_float(vip_context.get('counter_threat_level'), 0.5)
                vip_match['years_since_h2h'] = _safe_float(vip_context.get('years_since_h2h'), 0)
                vip_match['coach_changed'] = bool(vip_context.get('coach_changed', False))
                vip_match['core_player_lost'] = bool(vip_context.get('core_player_lost', False))
                vip_match['temporary_rotation'] = bool(vip_context.get('temporary_rotation', False))
                # 隐藏实力检测
                vip_match['opp_official_goals_scored'] = vip_context.get('opp_official_goals_scored')
                vip_match['opp_friendly_goals_scored'] = vip_context.get('opp_friendly_goals_scored')
                vip_match['opp_official_goals_conceded'] = vip_context.get('opp_official_goals_conceded')
                vip_match['opp_friendly_goals_conceded'] = vip_context.get('opp_friendly_goals_conceded')

            # ── Step 2: VIP-2 预测 ──
            vip_result = _vip_predictor.predict(vip_match)

            # ── Step 3: 转换为标准输出格式 ──
            probs = vip_result['probs']
            trap = vip_result['trap']
            scores = vip_result['scores']

            # 确定预测方向
            labels = ["H", "D", "A"]
            probs_list = [probs["H"], probs["D"], probs["A"]]
            pred_idx = max(range(3), key=lambda i: probs_list[i])
            confidence = probs_list[pred_idx]

            # 比分预测
            score_prediction = {
                "top_scores": [
                    {"score": s['score'], "probability": s['prob']}
                    for s in scores
                ],
                "lambda": {
                    "home": vip_result['fusion_λ'][0],
                    "away": vip_result['fusion_λ'][1],
                },
                "total_goals_expected": round(vip_result['fusion_λ'][0] + vip_result['fusion_λ'][1], 2),
            }

            # 大小球 (简化)
            total_exp = vip_result['fusion_λ'][0] + vip_result['fusion_λ'][1]
            over_prob = round(min(0.95, max(0.05, total_exp / 3.0)), 4)
            over_under = {
                "line": 2.5,
                "over_prob": over_prob,
                "under_prob": round(1.0 - over_prob, 4),
                "suggestion": "大" if over_prob > 0.5 else "小",
                "total_expected": round(total_exp, 2),
            }

            # 构建完整结果
            result = {
                "home_team": home_team,
                "away_team": away_team,
                "league": league,
                "match_date": None,
                "prediction": labels[pred_idx],
                "confidence": round(confidence, 4),
                "probabilities": {
                    "H": probs["H"],
                    "D": probs["D"],
                    "A": probs["A"],
                },
                "data_quality": {
                    "is_cold_start": False,
                    "feature_coverage_ratio": 1.0,
                },
                "prediction_mode": f"vip_2(trap={trap['score']:.1f})",
                "model_comparison": {
                    "v6_model": vip_result['model_probs_raw'],
                    "heuristic": None,
                    "odds_expert": None,
                    "odds_implied": None,
                    "fusion": {"H": probs["H"], "D": probs["D"], "A": probs["A"]},
                },
                "score_prediction": score_prediction,
                "over_under": over_under,
                "risk_assessment": None,
                # VIP v2 专属字段
                "vip_2": {
                    "trap": trap,
                    "bookmaker_view": vip_result['bookmaker_view'],
                    "recommendation": vip_result['recommendation'],
                    "fusion_λ": vip_result['fusion_λ'],
                    "book_λ": vip_result['book_λ'],
                    "model_λ": vip_result['model_λ'],
                    "model_probs_raw": vip_result['model_probs_raw'],
                    "corrected_probs": vip_result['corrected_probs'],
                    "all_scores": vip_result['all_scores'],
                    "max_rp": vip_result['max_rp'],
                    "model_version": vip_result['model_version'],
                },
            }

            if trap['score'] > 2.0:
                result["trap_detection"] = {
                    "score": round(trap['score'], 1),
                    "recommendation": vip_result['bookmaker_view'],
                    "adjusted_probs": None,
                    "signals": [
                        {"type": s['type'], "conf": round(s['confidence'], 2),
                         "direction": s['direction']}
                        for s in trap.get('signals', [])
                    ],
                    "features": trap.get('features', {}),
                }

            logger.info(
                f"[VIP-2] {home_team} vs {away_team} → "
                f"H={probs['H']:.3f} D={probs['D']:.3f} A={probs['A']:.3f} "
                f"trap={trap['score']:.1f} view={vip_result['bookmaker_view']}"
            )

            return result

        except Exception as e:
            logger.warning(f"[VIP-2] 预测失败 ({home_team} vs {away_team}): {e}", exc_info=True)
            return None

    def predict_next_match(self) -> Optional[Dict]:
        """预测下一场比赛"""
        try:
            from database.db_manager import get_db
            db = get_db()
            next_match = db.get_next_scheduled_match()
            if not next_match:
                return None

            return self.predict_single(
                next_match.get("home_team"),
                next_match.get("away_team"),
                next_match.get("league"),
            )
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
            logger.error(f"预测下一场比赛失败: {e}")
            return None

    def predict_single_v3(
        self, home_team: str, away_team: str, league: Optional[str] = None
    ) -> Optional[Dict]:
        """
        单场比赛预测 v3.0 — ToolPipeline 架构
        ======================================
        使用 Tool 化管线：FeatureBuilder → OddsAnalyzer → 降级链 → 后处理
        集成 Graceful Degradation + 全链路 Trace

        Args:
            home_team: 主队名
            away_team: 客队名
            league: 联赛名
        Returns:
            预测结果 dict (含 trace 字段)
        """
        try:
            from tools.tool_pipeline import ToolPipeline
            pipeline = ToolPipeline(prediction_service=self)
            result = pipeline.run(home_team, away_team, league)

            # 存储 tracer 引用供外部查看
            self._last_pipeline = pipeline

            logger.info(
                f"[V3] {home_team} vs {away_team} → "
                f"{result.get('prediction')} conf={result.get('confidence', 0):.1%} "
                f"mode={result.get('prediction_mode')} "
                f"level={result.get('degradation_level')}"
            )
            return result
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
            logger.error(f"V3预测失败 ({home_team} vs {away_team}): {e}", exc_info=True)
            # Fallback 到旧路径
            logger.warning("V3失败 → 回退到 V2 预测路径")
            return self.predict_single(home_team, away_team, league)

    def preview_trace(self) -> Optional[str]:
        """获取最近一次预测的全链路 Trace JSON"""
        pipeline = getattr(self, '_last_pipeline', None)
        if pipeline and pipeline.tracer:
            return pipeline.tracer.to_json()
        return None

    def print_trace(self) -> None:
        """打印最近一次预测的全链路 Trace"""
        pipeline = getattr(self, '_last_pipeline', None)
        if pipeline:
            pipeline.print_trace()
        else:
            logger.warning("无 Trace 数据")


    def predict_single(
        self, home_team: str, away_team: str, league: Optional[str] = None,
        custom_odds: Optional[Dict[str, float]] = None,
        vip_enabled: bool = False,
        vip_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        """
        单场比赛预测（v2.7 双路径: ModelBridge + 泊松比分 + 大小球 + 融合方案）

        重构说明：
        - 原函数 239 行 → 现在只有 50 行
        - 核心逻辑委托给多个单一职责函数

        v3.3: 支持 custom_odds — 当数据库没存赔率时(如未来比赛),
              调用方可显式传入 {home, draw, away} 赔率,
              SP crack 引擎会用它生成增强版隐含概率(冷启动救星)

        VIP v2: 支持 vip_enabled — 启用后用VIP-2三层融合(λ+陷阱+双通道投票)替换纯模型推理,
               提供操盘手意图分析和比分推荐。
        """
        try:
            # ── VIP v2 通道: 当 vip_enabled=True 且有赔率数据时, 使用VIP-2预测 ──
            if vip_enabled and _vip_available:
                vip_result = self._predict_with_vip(
                    home_team, away_team, league, custom_odds, vip_context
                )
                if vip_result is not None:
                    return vip_result
                logger.warning(f"[VIP-2] VIP预测失败, 降级到标准流程")

            # 1. 加载模型
            model = self.model
            if model is None:
                return None

            # 2. 准备特征
            features, quality_meta = self._prepare_features(home_team, away_team, league)
            if features is None:
                return None

            # 3. 运行模型预测
            h_prob, d_prob, a_prob = self._run_model_prediction(
                model, features, home_team, away_team, league
            )
            if h_prob is None:
                return None

            # 4. 获取赔率隐含概率
            odds_implied = self._get_odds_implied_probs(features, home_team, away_team)

            # 4.5 冷启动增强: 用 SP crack 引擎(R1/R6)生成/增强隐含概率
            #     优先顺序: custom_odds(显式传入) > 数据库赔率 > odds_implied 反推
            if quality_meta.get('is_cold_start'):
                enhanced = self._enhance_odds_with_sp_crack(
                    home_team, away_team, league, odds_implied,
                    custom_odds=custom_odds
                )
                if enhanced:
                    odds_implied = enhanced

            # 4.6 P0-2: HeuristicPredictor (冷启动救星 + 三路融合)
            #     即使 .joblib 里 heuristic=None, 这里用规则版补上
            h_heur = d_heur = a_heur = None
            three_way_done = False
            if _heuristic_available:
                try:
                    X, feat_names = self._features_to_vec(features)
                    odds_data = self._build_heuristic_odds(home_team, away_team, league, odds_implied, custom_odds)
                    proba = _heuristic_predictor.predict_proba(
                        X, feature_names=feat_names,
                        odds_data=odds_data, league_name=league
                    )[0]
                    h_heur, d_heur, a_heur = float(proba[0]), float(proba[1]), float(proba[2])
                    logger.info(
                        f"[Heuristic] {home_team} vs {away_team} → "
                        f"H={h_heur:.3f} D={d_heur:.3f} A={a_heur:.3f}"
                    )
                except Exception as _he:
                    logger.warning(f"[Heuristic] 调用失败: {_he}")

            # 4.7 P0-DGate: D通道专科替代 + spread自适应融合
            #     核心创新: meta-learner D被LGB/XGB噪声污染(F1_D≈0),
            #     用OE+Heuristic(D信号最强)的D概率外科式替换meta D通道
            #     spread越窄→D概率越高→D-specialist权重越大
            if h_heur is not None and odds_implied:
                p_h_o = odds_implied.get('H', 0.33)
                p_d_o = odds_implied.get('D', 0.34)
                p_a_o = odds_implied.get('A', 0.33)
                proba_spread = abs(p_h_o - p_a_o)

                # ── Step 1: 获取OE子模型独立输出 ──
                oe_out = model.get_oe_output()  # 从model_bridge缓存读取
                h_oe = d_oe = a_oe = None
                if oe_out:
                    h_oe = oe_out.get('home', 0.33)
                    d_oe = oe_out.get('draw', DEFAULT_DRAW_PROB)
                    a_oe = oe_out.get('away', 0.33)
                    # OE OOD检测: 近均匀分布→标记为无信号
                    oe_entropy = abs(max(h_oe, d_oe, a_oe) - min(h_oe, d_oe, a_oe))
                    if oe_entropy < 0.02:
                        h_oe = d_oe = a_oe = None  # OE无信号, 不参与D-specialist

                # ── Step 2: D-specialist概率 (OE+Heuristic加权, 权重按F1_D强度) ──
                # Heuristic F1_D=0.422 > OE F1_D=0.376 → 权重0.55/0.45
                # v4.0: DrawExpert可选参与 (若可用)
                de_pdraw = model.get_de_output()  # v4.0: DrawExpert P(Draw)
                
                if d_oe is not None and de_pdraw is not None:
                    # v4.0: 三信号源融合 (Heuristic + OE + DrawExpert)
                    d_spec = 0.40 * d_heur + 0.30 * d_oe + 0.30 * de_pdraw
                    h_spec = 0.55 * h_heur + 0.45 * h_oe  # H/A: 不含DrawExpert
                    a_spec = 0.55 * a_heur + 0.45 * a_oe
                    logger.debug(f"[D-Gate v4.0] DrawExpert参与D-specialist: P(D)={de_pdraw:.3f}")
                elif d_oe is not None:
                    d_spec = 0.55 * d_heur + 0.45 * d_oe
                    h_spec = 0.55 * h_heur + 0.45 * h_oe
                    a_spec = 0.55 * a_heur + 0.45 * a_oe
                elif de_pdraw is not None:
                    # OE无信号但DrawExpert可用: Heuristic + DrawExpert
                    d_spec = 0.55 * d_heur + 0.45 * de_pdraw
                    h_spec = h_heur
                    a_spec = a_heur
                    logger.debug(f"[D-Gate v4.0] OE无信号, DrawExpert备用: P(D)={de_pdraw:.3f}")
                else:
                    # OE无信号 → D-specialist退化为纯Heuristic
                    d_spec = d_heur
                    h_spec = h_heur
                    a_spec = a_heur

                # ── Step 3: D-gate置信度 (spread驱动 + D信号一致性调制) ──
                # 窄spread → D大概率 → 高gate置信度 → 信任D-specialist
                # 宽spread → D小概率 → 低gate置信度 → 信任meta-learner
                if proba_spread < 0.15:   # 极窄: D非常可能
                    d_gate = 0.65
                elif proba_spread < 0.25: # 中窄: D有可能
                    d_gate = 0.45
                elif proba_spread < 0.40: # 正常: 不确定
                    d_gate = 0.25
                elif proba_spread < 0.55: # 中宽: D不太可能
                    d_gate = 0.12
                else:                     # 极宽: D非常不可能
                    d_gate = 0.05

                # D信号一致性调制: OE和Heuristic对D判断越一致, gate越强
                if d_oe is not None:
                    d_agreement = 1.0 - abs(d_oe - d_heur) / max(d_oe + d_heur, 0.001)
                    d_gate *= (0.5 + 0.5 * d_agreement)
                else:
                    # 只有Heuristic: 降低gate置信度(单信号源不可靠)
                    d_gate *= 0.65

                # 冷启动时: D-specialist更可信(赔率驱动), 提高gate
                if quality_meta.get('is_cold_start'):
                    d_gate = min(d_gate * 1.3, 0.80)

                # ── Step 4: D通道外科替代 ──
                # D: blend meta D + specialist D
                d_final = d_prob * (1 - d_gate) + d_spec * d_gate
                # H/A: 按meta原始比例从剩余概率中重新分配
                remaining = 1.0 - d_final
                ha_sum = h_prob + a_prob
                if ha_sum > 0.001:
                    h_base = remaining * (h_prob / ha_sum)
                    a_base = remaining * (a_prob / ha_sum)
                else:
                    h_base = remaining * 0.5
                    a_base = remaining * 0.5

                # ── Step 5: Odds overlay (spread自适应权重) ──
                # 极端spread → odds主导; 窄spread → D-specialist主导
                if proba_spread > 0.70:
                    # 极端强弱: odds 80% + base 20%
                    w_base, w_odds = 0.20, 0.80
                elif proba_spread > 0.40:
                    # 宽spread: odds偏重
                    w_base, w_odds = 0.40, 0.60
                elif proba_spread > 0.25:
                    # 正常spread: 基本平衡
                    w_base, w_odds = 0.55, 0.45
                elif proba_spread > 0.15:
                    # 中窄spread: base偏重(D-specialist已在上步生效)
                    w_base, w_odds = 0.65, 0.35
                else:
                    # 极窄spread: D-specialist主导
                    w_base, w_odds = 0.75, 0.25

                h_out = h_base * w_base + p_h_o * w_odds
                d_out = d_final * w_base + p_d_o * w_odds
                a_out = a_base * w_base + p_a_o * w_odds
                tot = h_out + d_out + a_out or 1.0
                h_prob, d_prob, a_prob = h_out / tot, d_out / tot, a_out / tot

                logger.info(
                    f"[D-Gate Fusion] {home_team} vs {away_team} → "
                    f"H={h_prob:.3f} D={d_prob:.3f} A={a_prob:.3f} "
                    f"(spread={proba_spread:.2f} d_gate={d_gate:.2f} "
                    f"OE={'Y' if d_oe is not None else 'N'} "
                    f"w_base={w_base:.0%} w_odds={w_odds:.0%})"
                )
                three_way_done = True

            # 5. 计算融合概率（含冷启动降级） — 若 4.7 已做, 5 步透传
            if three_way_done:
                # P0-3: 4.7 三路融合是最终结果, 跳过 5 步 odds_degraded
                total = h_prob + d_prob + a_prob or 1.0
                fusion = {
                    "H": round(h_prob/total, 4),
                    "D": round(d_prob/total, 4),
                    "A": round(a_prob/total, 4),
                }
                prediction_mode = "d_gate_fusion"

                # P2 D-GATE 已被 D-Gate Fusion 内置 (D通道替代+spread自适应)
                # 不再需要独立D-GATE后处理
            else:
                fusion, prediction_mode = self._compute_fusion_with_cold_start(
                h_prob, d_prob, a_prob, odds_implied, quality_meta,
                home_team=home_team, away_team=away_team
            )

            # 5a. P1: 贝叶斯先验注入 (赔率→Dixon-Coles参数→融合校准)
            #     信号等级 F (噪声) 时自动跳过; S/A/B/C 按等级调整注入强度
            fusion, bayes_tag = self._apply_bayesian_calibration(
                fusion, home_team, away_team, league,
                custom_odds=custom_odds,
                injection_strength=0.30
            )
            if bayes_tag and not bayes_tag.startswith('bayes_unavailable'):
                prediction_mode = f"{prediction_mode}+{bayes_tag}"

            # 5b. P2: 操盘手陷阱检测 (反诱盘 — 从"拟合赔率"进化到"解码庄家意图")
            trap_report = None
            if _trap_available and custom_odds:
                try:
                    trap_report = _trap_detector.detect({
                        "home": home_team, "away": away_team,
                        "league": league or "其他",
                        "odds_h": custom_odds.get("home", fusion["H"]),
                        "odds_d": custom_odds.get("draw", fusion["D"]),
                        "odds_a": custom_odds.get("away", fusion["A"]),
                        "asian_handicap": custom_odds.get("asian_handicap"),
                        "water_level": custom_odds.get("water_level", 0.92),
                    })
                    if trap_report and trap_report.aggregate_score > 2.0:
                        # 陷阱信号注入：按评分强度调整概率
                        alpha = min(0.25, trap_report.aggregate_score * 0.03)
                        for k in ["H", "D", "A"]:
                            fusion[k] = (1 - alpha) * fusion[k] + alpha * trap_report.adjusted_probs.get(k, fusion[k])
                        total = sum(fusion.values())
                        for k in fusion:
                            fusion[k] /= max(total, 1e-6)
                        prediction_mode = f"{prediction_mode}+trap{trap_report.aggregate_score:.0f}"
                        logger.info(f"[TrapDetector] {home_team} vs {away_team}: "
                                    f"score={trap_report.aggregate_score:.1f} "
                                    f"D_adj={trap_report.adjusted_probs.get('D',0)*100:.1f}%")
                except Exception as _te:
                    logger.debug(f"[TrapDetector] 跳过 ({home_team} vs {away_team}): {_te}")

            # 6. 确定最终预测
            labels = ["H", "D", "A"]
            probs_list = [fusion["H"], fusion["D"], fusion["A"]]
            pred_idx = max(range(3), key=lambda i: probs_list[i])
            confidence = probs_list[pred_idx]

            # 7. 泊松比分预测
            score_prediction = self._compute_score_prediction(
                fusion["H"], fusion["D"], fusion["A"], league
            )

            # 8. 大小球分析
            over_under = self._compute_over_under(
                fusion["H"], fusion["D"], fusion["A"], league
            )

            # 9. 收割风险扫描
            risk_assessment = self._run_harvesting_guard_scan(
                home_team, away_team, league, fusion, score_prediction
            )

            # 10. 构建完整返回结果
            # P0: 读取OE子模型输出用于结果记录
            oe_probs = model.get_oe_output() if hasattr(model, 'get_oe_output') else None
            result = self._build_prediction_result(
                home_team, away_team, league, labels, pred_idx, confidence,
                h_prob, d_prob, a_prob, fusion, odds_implied,
                quality_meta, prediction_mode, score_prediction, over_under,
                risk_assessment,
                heuristic_probs=(h_heur, d_heur, a_heur),
                oe_probs=oe_probs,
                trap_report=trap_report
            )

            return result

        except ValueError as e:
            # P0-1: 数据泄露特征拦截，向上抛出
            logger.error(f"[DataLeakage] 预测被拦截 ({home_team} vs {away_team}): {e}")
            raise
        except TypeError as e:
            # Defense-in-depth: 捕获类型错误(如numpy数组shape问题), 优雅降级而非崩溃
            logger.error(f"[TypeError] 预测失败 ({home_team} vs {away_team}): {e}", exc_info=True)
            return None
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
            logger.error(f"预测失败 ({home_team} vs {away_team}): {e}")
            return None

    # ── 重构后的辅助函数 ──────────────────────────────────────────────────

    def _prepare_features(self, home_team: str, away_team: str, league: Optional[str]) -> Optional[Dict]:
        """准备预测所需的特征（提取自原函数）"""
        _build_result = self._build_features(home_team, away_team, league)
        if _build_result is None:
            return None, None

        if isinstance(_build_result, tuple):
            features, quality_meta = _build_result
        else:
            features = _build_result
            quality_meta = {
                'home_match_count': 0, 'away_match_count': 0,
                'feature_coverage_ratio': 0.0, 'is_cold_start': True,
                'home_fields_provided': 0, 'away_fields_provided': 0,
            }

        return features, quality_meta

    def _features_to_vec(self, features: Optional[Dict]):
        """
        把 features dict 转成 (X, feat_names) 给 HeuristicPredictor 用
        冷启动时 features 几乎是空 dict → 全部置 0
        """
        if not features:
            return np.zeros((1, 1)), []
        feat_names = list(features.keys())
        vec = np.array([float(features.get(n, 0) or 0) for n in feat_names], dtype=float)
        return vec.reshape(1, -1), feat_names

    def _build_heuristic_odds(
        self, home_team: str, away_team: str, league: Optional[str],
        odds_implied: Optional[Dict], custom_odds: Optional[Dict[str, float]]
    ) -> Optional[Dict]:
        """
        给 HeuristicPredictor._predict_by_odds 喂的赔率字典 {home, draw, away, over25, under25}
        优先顺序: custom_odds > odds_implied 反推(加5%抽水) > None
        """
        if custom_odds:
            h = custom_odds.get('home_win') or custom_odds.get('home')
            d = custom_odds.get('draw')
            a = custom_odds.get('away_win') or custom_odds.get('away')
            if h and d and a:
                return {
                    'home': float(h), 'draw': float(d), 'away': float(a),
                    'over25': float(custom_odds.get('over_2_5') or 0) or 0,
                    'under25': float(custom_odds.get('under_2_5') or 0) or 0,
                }
        if odds_implied and all(k in odds_implied for k in ('H', 'D', 'A')):
            # 隐含概率 → 赔率, 加 5% 抽水近似
            def _to_odd(p, margin=1.05):
                return round(margin / max(p, 0.01), 3) if p > 0 else 0
            return {
                'home': _to_odd(odds_implied['H']),
                'draw': _to_odd(odds_implied['D']),
                'away': _to_odd(odds_implied['A']),
            }
        return None

    def _run_model_prediction(self, model, features, home_team: str, away_team: str, 
                                  league: Optional[str]):
        """运行模型预测（提取自原函数）"""
        odds_features = self._build_odds_features(home_team, away_team, league)

        if odds_features is not None:
            model_result = model.predict(features, odds_data=odds_features)
        else:
            model_result = model.predict(features)

        if model_result is None:
            return None, None, None

        return (
            model_result.get("home", 0),
            model_result.get("draw", 0),
            model_result.get("away", 0)
        )

    def _compute_fusion_with_cold_start(
        self, h_prob: float, d_prob: float, a_prob: float,
        odds_implied: Optional[Dict], quality_meta: Dict,
        home_team: str = '', away_team: str = ''
    ):
        """计算融合概率，含冷启动自动降级（提取自原函数）"""
        is_cold_start = quality_meta.get('is_cold_start', False)
        feat_cov_ratio = quality_meta.get('feature_coverage_ratio', 0.0)

        if is_cold_start and odds_implied:
            # P1: 极端强弱(spread > 0.70) → 加权融合(odds 80% + model 20%)
            p_h_o = odds_implied.get('H', 0.33)
            p_d_o = odds_implied.get('D', 0.34)
            p_a_o = odds_implied.get('A', 0.33)
            proba_spread = abs(p_h_o - p_a_o)
            if proba_spread > 0.70:
                # P1: 不再纯 odds 覆盖, 保留 20% model 信号
                h_f = h_prob * 0.20 + p_h_o * 0.80
                d_f = d_prob * 0.20 + p_d_o * 0.80
                a_f = a_prob * 0.20 + p_a_o * 0.80
                tot = h_f + d_f + a_f or 1.0
                fusion = {"H": round(h_f/tot, 4), "D": round(d_f/tot, 4), "A": round(a_f/tot, 4)}
                prediction_mode = f"odds_override_fusion(spread={proba_spread:.2f})"
                logger.info(
                    f"[COLD-START-FUSION-OVERRIDE] {home_team} vs {away_team} | "
                    f"spread={proba_spread:.2f} → 加权融合(odds80%+model20%)"
                )
                return fusion, prediction_mode

            # P0-3: 4.7 已做三路融合 → 直接透传, 不要二次覆盖
            # 冷启动降级 (原逻辑, 只在 4.7 未做时才用)
            _odds_boost = max(0.0, (0.50 - feat_cov_ratio) * 2)
            _new_odds_w = min(0.95, self.FUSION_ODDS_WEIGHT + _odds_boost)
            _new_model_w = 1.0 - _new_odds_w

            h_o = odds_implied.get("H", h_prob)
            d_o = odds_implied.get("D", d_prob)
            a_o = odds_implied.get("A", a_prob)

            h_fused = h_o * _new_odds_w + h_prob * _new_model_w
            d_fused = d_o * _new_odds_w + d_prob * _new_model_w
            a_fused = a_o * _new_odds_w + a_prob * _new_model_w

            _total = h_fused + d_fused + a_fused or 1.0
            fusion = {
                "H": round(h_fused / _total, 4),
                "D": round(d_fused / _total, 4),
                "A": round(a_fused / _total, 4),
            }
            prediction_mode = f"odds_degraded(odds={_new_odds_w:.0%},model={_new_model_w:.0%})"

            logger.info(
                f"[COLD-START-FUSION] {home_team} vs {away_team} | "
                f"cov={feat_cov_ratio:.1%} | 权重→odds={_new_odds_w:.0%} model={_new_model_w:.0%}"
            )

            return fusion, prediction_mode
        else:
            # 正常路径
            model_probs = (h_prob, d_prob, a_prob)
            fusion = self._compute_fusion(model_probs, odds_implied)
            return fusion, "fusion"

    def _run_harvesting_guard_scan(
        self, home_team: str, away_team: str, league: Optional[str],
        fusion: Dict, score_prediction: Dict
    ):
        """运行 HarvestingGuard 风险扫描（提取自原函数）"""
        if not _guard_available or _guard is None:
            return None

        try:
            lam_dict = score_prediction.get('lambda', {})
            model_total_lambda = (
                lam_dict.get('home', 1.3) + lam_dict.get('away', 1.2)
            )

            odds_1x2 = self._get_odds_1x2(home_team, away_team)
            odds_totals = self._get_odds_totals(home_team, away_team)
            odds_ah = self._get_odds_ah(home_team, away_team)

            report = _guard.scan(
                odds_1x2=odds_1x2,
                odds_totals=odds_totals,
                odds_ah=odds_ah,
                league=league,
                model_total_lambda=model_total_lambda,
            )

            risk_assessment = {
                'hrs': report.hrs,
                'risk_level': report.risk_level,
                'confidence': report.confidence,
                'tail_risk': report.tail_risk_factor,
                'extreme_score_prob': report.extreme_score_prob,
                'signals': {
                    '1x2': round(report.signal_1x2, 4),
                    'totals': round(report.signal_totals, 4),
                    'ah': round(report.signal_ah, 4),
                },
                'anomalies': [
                    {'market': a.market, 'dimension': a.dimension}
                    for a in report.anomalies[:5]
                ],
            }

            # 高风险时生成调整预测
            if report.hrs > 0.45:
                adjusted = _guard.adjust_prediction(
                    {'H': fusion['H'], 'D': fusion['D'], 'A': fusion['A']},
                    report
                )
                risk_assessment['adjusted_probs'] = adjusted.adjusted_probs

            logger.info(
                f"[Guard] {home_team} vs {away_team}: "
                f"HRS={report.hrs:.2f} ({report.risk_level})"
            )

            return risk_assessment

        except Exception as e:
            logger.warning(f"收割风险扫描失败: {e}")
            return None

    # ── 贝叶斯先验注入 ──────────────────────────────────────────────────

    def _apply_bayesian_calibration(
        self, fusion: Dict[str, float],
        home_team: str, away_team: str, league: Optional[str],
        custom_odds: Optional[Dict[str, float]] = None,
        injection_strength: float = 0.30
    ) -> Tuple[Dict[str, float], str]:
        """
        P1: 赔率→贝叶斯先验注入 (BookmakerBayesInfer)

        将庄家赔率反推为 Dixon-Coles 贝叶斯参数 (λ_H, λ_A, ρ),
        然后以"先验注入"方式与模型融合后验校准:

          后验 ∝ 庄家先验^α × 模型似然^(1-α)
          α = injection_strength × signal_confidence

        信号等级 (S/A/B/C/F) 决定 α 的上限:
          S=0.9  A=0.7  B=0.5  C=0.3  F=0.0(跳过)

        条件:
          - BookmakerBayesInfer 导入成功
          - 有可用赔率数据 (custom_odds 或数据库)
          - 信号等级 ≥ 'C'

        Returns:
          (校准后的 fusion, 状态标签)
        """
        if not _bayes_available:
            return fusion, "bayes_unavailable"

        # ── Step 1: 获取原始赔率 (H/D/A) ──
        odds_h = odds_d = odds_a = None

        if custom_odds:
            h_raw = custom_odds.get('home_win') or custom_odds.get('home')
            d_raw = custom_odds.get('draw')
            a_raw = custom_odds.get('away_win') or custom_odds.get('away')
            if h_raw and d_raw and a_raw:
                odds_h, odds_d, odds_a = float(h_raw), float(d_raw), float(a_raw)

        if odds_h is None:
            raw_odds = self._get_odds_1x2(home_team, away_team)
            if raw_odds:
                odds_h, odds_d, odds_a = raw_odds['home'], raw_odds['draw'], raw_odds['away']

        if odds_h is None or odds_h <= 1.0:
            return fusion, "bayes_no_odds"

        # ── Step 2: 贝叶斯逆向推断 ──
        try:
            odds_1x2 = {'home': float(odds_h), 'draw': float(odds_d), 'away': float(odds_a)}
            result = _bayes_infer.infer_parameters(
                odds_1x2=odds_1x2,
                league_name=league or 'default',
            )

            if result.signal_grade == 'F':
                return fusion, f"bayes_noise(grade=F, or={result.overround_estimated:.3f})"

            # ── Step 3: 先验注入 ──
            model_probs = {
                'home': fusion.get('H', 0.33),
                'draw': fusion.get('D', DEFAULT_DRAW_PROB),
                'away': fusion.get('A', 0.33),
            }
            fused = _bayes_infer.inject_as_prior(result, model_probs, injection_strength)

            calibrated = {
                'H': round(fused.get('home', model_probs['home']), 4),
                'D': round(fused.get('draw', model_probs['draw']), 4),
                'A': round(fused.get('away', model_probs['away']), 4),
            }

            # 归一化兜底
            total = sum(calibrated.values()) or 1.0
            calibrated = {k: round(v / total, 4) for k, v in calibrated.items()}

            logger.info(
                f"[BayesCalib] {home_team} vs {away_team} → "
                f"H={calibrated['H']:.3f} D={calibrated['D']:.3f} A={calibrated['A']:.3f} "
                f"(grade={result.signal_grade} α={injection_strength:.2f} "
                f"λ_H={result.posterior_lambda_h:.3f} λ_A={result.posterior_lambda_a:.3f})"
            )

            return calibrated, f"bayes_{result.signal_grade}"

        except Exception as e:
            logger.warning(f"[BayesCalib] 推断失败 ({home_team} vs {away_team}): {e}")
            return fusion, "bayes_error"

    def _build_prediction_result(
        self, home_team: str, away_team: str, league: Optional[str],
        labels: List[str], pred_idx: int, confidence: float,
        h_prob: float, d_prob: float, a_prob: float,
        fusion: Dict, odds_implied: Optional[Dict],
        quality_meta: Dict, prediction_mode: str,
        score_prediction: Dict, over_under: Dict,
        risk_assessment: Optional[Dict],
        heuristic_probs: Optional[Tuple[Optional[float], Optional[float], Optional[float]]] = None,
        oe_probs: Optional[Dict] = None,
        trap_report = None
    ):
        """构建完整的预测结果字典（提取自原函数）"""
        h_h, d_h, a_h = (None, None, None)
        if heuristic_probs and all(v is not None for v in heuristic_probs):
            h_h, d_h, a_h = heuristic_probs
        result = {
            "home_team": home_team,
            "away_team": away_team,
            "league": league,
            "match_date": None,
            "prediction": labels[pred_idx],
            "confidence": round(confidence, 4),
            "probabilities": {
                "H": round(fusion["H"], 4),
                "D": round(fusion["D"], 4),
                "A": round(fusion["A"], 4),
            },
            "data_quality": quality_meta,
            "prediction_mode": prediction_mode,
            "model_comparison": {
                "v6_model": {"H": round(h_prob, 4), "D": round(d_prob, 4), "A": round(a_prob, 4)},
                "heuristic": ({"H": round(h_h, 4), "D": round(d_h, 4), "A": round(a_h, 4)}
                              if h_h is not None else None),
                "odds_expert": oe_probs,
                "odds_implied": odds_implied,
                "fusion": {"H": round(fusion["H"], 4), "D": round(fusion["D"], 4), "A": round(fusion["A"], 4)},
            },
            "score_prediction": score_prediction,
            "over_under": over_under,
            "risk_assessment": risk_assessment,
        }
        if trap_report and trap_report.aggregate_score > 2.0:
            result["trap_detection"] = {
                "score": round(trap_report.aggregate_score, 1),
                "recommendation": trap_report.recommendation,
                "adjusted_probs": trap_report.adjusted_probs,
                "signals": [{"type": s.trap_type.value, "conf": round(s.confidence, 2),
                             "direction": s.direction} for s in trap_report.signals],
                "features": trap_report.trap_features,
            }
        return result

    def _get_model_probs(self, model, features) -> Optional[Tuple[float, float, float]]:
        """从模型获取 H/D/A 概率"""
        try:
            # ModelBridge 路径
            if hasattr(model, 'predict'):
                result = model.predict(features)
                if result is not None:
                    return (
                        result.get("home", 0),
                        result.get("draw", 0),
                        result.get("away", 0),
                    )

            # 旧 EnsembleTrainer fallback
            if hasattr(model, 'ensemble_predict_proba'):
                proba = model.ensemble_predict_proba(features)
                return float(proba[0]), float(proba[1]), float(proba[2])

            return None
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError, ValueError, TypeError) as e:
            logger.error(f"模型预测失败: {e}")
            return None

    def _get_odds_implied_probs(self, features, home_team: str, away_team: str) -> Optional[Dict]:
        """获取赔率隐含概率（从特征或数据库）"""
        try:
            # 尝试从特征中获取赔率隐含概率
            p_implied = features.get("p_implied") or features.get("mkt_implied_home_prob")
            if p_implied:
                # 特征中有隐含概率相关字段
                odds_home = features.get("odds_home") or features.get("b365h")
                odds_draw = features.get("odds_draw") or features.get("b365d")
                odds_away = features.get("odds_away") or features.get("b365a")

                if odds_home and odds_draw and odds_away:
                    return self._odds_to_implied_probs(odds_home, odds_draw, odds_away)

            # 尝试从数据库获取赔率（通过 match_id 查询）
            from database.db_manager import get_db
            db = get_db()
            odds_data = self._query_odds_by_teams(db, home_team, away_team)
            if odds_data:
                return self._odds_to_implied_probs(
                    odds_data.get("home_odds", 0),
                    odds_data.get("draw_odds", 0),
                    odds_data.get("away_odds", 0),
                )

            return None
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
            logger.debug(f"赔率隐含概率获取失败: {e}")
            return None

    def _enhance_odds_with_sp_crack(
        self, home_team: str, away_team: str, league: Optional[str],
        odds_implied: Optional[Dict[str, float]],
        custom_odds: Optional[Dict[str, float]] = None
    ) -> Optional[Dict[str, float]]:
        """
        冷启动场景: 用 SP crack 引擎(R1/R6 规则)增强隐含概率

        作用: 在冷启动时，ML 模型学不到东西（feature 8.9% 覆盖），
              把"纯赔率隐含概率"升级为"SP crack 增强版"——
              内部已应用 SP 移植过来的 R1 平局最低、R6 最低波胆等规则。

        赔率来源优先级:
          1. custom_odds(显式传入, 最高优先)
          2. 数据库 matches+odds 表
          3. odds_implied 反推(加 5% 抽水)
        """
        try:
            h_odd = d_odd = a_odd = 0.0

            # 1) 优先: 显式传入的赔率 (支持 home_win/draw/away_win 格式)
            if custom_odds:
                h_odd = float(custom_odds.get('home_win') or custom_odds.get('home') or 0)
                d_odd = float(custom_odds.get('draw') or 0)
                a_odd = float(custom_odds.get('away_win') or custom_odds.get('away') or 0)
                if h_odd > 1.01 and d_odd > 1.01 and a_odd > 1.01:
                    logger.debug(f"[SP-CRACK] 使用 custom_odds: H={h_odd} D={d_odd} A={a_odd}")

            # 2) 次优: 数据库赔率
            if h_odd <= 1.01 or d_odd <= 1.01 or a_odd <= 1.01:
                from database.db_manager import get_db
                db = get_db()
                raw_odds = self._query_odds_by_teams(db, home_team, away_team)
                if raw_odds:
                    h_odd = float(raw_odds.get("home_odds", 0))
                    d_odd = float(raw_odds.get("draw_odds", 0))
                    a_odd = float(raw_odds.get("away_odds", 0))

            # 3) 兜底: 从 odds_implied 反推(加 5% 抽水)
            if h_odd <= 1.01 or d_odd <= 1.01 or a_odd <= 1.01:
                if odds_implied:
                    h_imp = float(odds_implied.get('H', 0.33))
                    d_imp = float(odds_implied.get('D', 0.34))
                    a_imp = float(odds_implied.get('A', 0.33))
                    if h_imp > 0.01 and d_imp > 0.01 and a_imp > 0.01:
                        margin = 1.05
                        h_odd = margin / h_imp
                        d_odd = margin / d_imp
                        a_odd = margin / a_imp
                        logger.debug(
                            f"[SP-CRACK] 从隐含概率反推: H={h_odd:.2f} D={d_odd:.2f} A={a_odd:.2f}"
                        )
                if h_odd <= 1.01:
                    return None

            # 4) 调 SP crack 引擎
            from rules.sp_core import crack
            result = crack(
                home=home_team, away=away_team,
                h=h_odd, d=d_odd, a=a_odd,
                league=league or 'unknown',
                version='v2',
            )

            # 5) 用 crack 的 implied_probs 替换
            sp_probs = result.get('implied_probs')
            if not sp_probs:
                return None

            orig_h = odds_implied.get('H', 0) if odds_implied else 0
            orig_d = odds_implied.get('D', 0) if odds_implied else 0
            orig_a = odds_implied.get('A', 0) if odds_implied else 0
            logger.info(
                f"[SP-CRACK-ENHANCE] {home_team} vs {away_team} | "
                f"原: H={orig_h:.3f} D={orig_d:.3f} A={orig_a:.3f} | "
                f"增强后: H={sp_probs['H']:.3f} D={sp_probs['D']:.3f} A={sp_probs['A']:.3f} | "
                f"判定: {result.get('direction')} ({result.get('direction_confidence')}%)"
            )

            return {
                'H': round(sp_probs['H'], 4),
                'D': round(sp_probs['D'], 4),
                'A': round(sp_probs['A'], 4),
            }
        except Exception as e:
            logger.debug(f"SP crack 增强失败，使用原 odds_implied: {e}")
            return None

    def _get_odds_1x2(self, home_team: str, away_team: str) -> Optional[Dict[str, float]]:
        """
        获取1X2赔率 (用于 HarvestingGuard 扫描)
        
        Returns:
            {'home': 2.10, 'draw': 3.20, 'away': 3.75} 或 None
        """
        try:
            from database.db_manager import get_db
            db = get_db()
            odds_data = self._query_odds_by_teams(db, home_team, away_team)
            if odds_data:
                h = odds_data.get("home_odds", 0)
                d = odds_data.get("draw_odds", 0)
                a = odds_data.get("away_odds", 0)
                if h > 0 and d > 0 and a > 0:
                    return {'home': h, 'draw': d, 'away': a}
            return None
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
            logger.debug(f"1X2赔率获取失败: {e}")
            return None

    def _get_odds_totals(self, home_team: str, away_team: str) -> Optional[Dict[str, float]]:
        """
        获取大小球赔率 (用于 HarvestingGuard 扫描)
        
        从 betting_markets 表查询最新 Totals 数据。
        betting_markets schema:
          market_type: 'Totals' | 'Over/Under' 等
          market_line: '2.5' | '3.0' 等
          outcome_name: 'Over' | 'Under'
          odds: 赔率值
        
        Returns:
            {'line': 2.5, 'over': 1.88, 'under': 2.85} 或 None
        """
        try:
            import sqlite3
            from database.db_manager import get_db
            db = get_db()
            
            # 获取 match_id
            match_id = db.get_match_id(home_team, away_team)
            if not match_id:
                return None
            
            # 查询 betting_markets (通过原始连接获取)
            conn = getattr(db, 'conn', None) or getattr(db, '_conn', None)
            own_conn = False
            if conn is None:
                # 尝试用 db 的路径
                db_path = getattr(db, 'db_path', None)
                if db_path:
                    conn = sqlite3.connect(db_path)
                    own_conn = True
                else:
                    return None
            
            try:
                c = conn.cursor()
                c.execute("""
                    SELECT market_line, outcome_name, odds
                    FROM betting_markets
                    WHERE match_id = ? 
                      AND market_type IN ('Totals', 'Over/Under', 'totals', 'over_under')
                    ORDER BY created_at DESC
                """, (match_id,))
                
                rows = c.fetchall()
                if not rows:
                    return None
                
                # 按 line 分组, 找最常见的 line
                from collections import defaultdict
                line_groups = defaultdict(dict)
                for line_str, outcome, odds_val in rows:
                    try:
                        line = float(line_str)
                    except (ValueError, TypeError):
                        continue
                    key = line
                    if 'over' in str(outcome).lower():
                        line_groups[key]['over'] = odds_val
                    elif 'under' in str(outcome).lower():
                        line_groups[key]['under'] = odds_val
                
                # 返回第一个完整的 line (有 over 和 under)
                for line, data in sorted(line_groups.items()):
                    if 'over' in data and 'under' in data:
                        return {
                            'line': line,
                            'over': data['over'],
                            'under': data['under'],
                        }
                
                return None
            finally:
                if own_conn:
                    conn.close()
        except (ValueError, TypeError) as e:
            logger.debug(f"Totals赔率获取失败: {e}")
            return None

    def _get_odds_ah(self, home_team: str, away_team: str) -> Optional[Dict[str, float]]:
        """
        获取亚盘赔率 (用于 HarvestingGuard 扫描)
        
        Returns:
            {'line': -0.5, 'home_cover': 2.12, 'away_cover': 1.81} 或 None
        """
        try:
            import sqlite3
            from database.db_manager import get_db
            db = get_db()
            
            match_id = db.get_match_id(home_team, away_team)
            if not match_id:
                return None
            
            conn = getattr(db, 'conn', None) or getattr(db, '_conn', None)
            own_conn = False
            if conn is None:
                db_path = getattr(db, 'db_path', None)
                if db_path:
                    conn = sqlite3.connect(db_path)
                    own_conn = True
                else:
                    return None
            
            try:
                c = conn.cursor()
                c.execute("""
                    SELECT market_line, outcome_name, odds
                    FROM betting_markets
                    WHERE match_id = ?
                      AND market_type IN ('Asian Handicap', 'AH', 'asian_handicap', 'handicap')
                    ORDER BY created_at DESC
                """, (match_id,))
                
                rows = c.fetchall()
                if not rows:
                    return None
                
                from collections import defaultdict
                line_groups = defaultdict(dict)
                for line_str, outcome, odds_val in rows:
                    try:
                        line = float(line_str)
                    except (ValueError, TypeError):
                        continue
                    key = line
                    outcome_l = str(outcome).lower()
                    if 'home' in outcome_l:
                        line_groups[key]['home_cover'] = odds_val
                    elif 'away' in outcome_l or 'guest' in outcome_l:
                        line_groups[key]['away_cover'] = odds_val
                
                for line, data in sorted(line_groups.items()):
                    if 'home_cover' in data and 'away_cover' in data:
                        return {
                            'line': line,
                            'home_cover': data['home_cover'],
                            'away_cover': data['away_cover'],
                        }
                
                return None
            finally:
                if own_conn:
                    conn.close()
        except (ValueError, TypeError) as e:
            logger.debug(f"AH赔率获取失败: {e}")
            return None

    @staticmethod
    def _odds_to_implied_probs(odds_h: float, odds_d: float, odds_a: float) -> Dict:
        """从赔率计算隐含概率（去抽水）"""
        try:
            if odds_h <= 0 or odds_d <= 0 or odds_a <= 0:
                return None
            raw_h = 1.0 / odds_h
            raw_d = 1.0 / odds_d
            raw_a = 1.0 / odds_a
            total = raw_h + raw_d + raw_a  # 包含抽水
            return {
                "H": round(raw_h / total, 4),
                "D": round(raw_d / total, 4),
                "A": round(raw_a / total, 4),
            }
        except (KeyError, TypeError, ZeroDivisionError) as e:
            logger.debug(f"计算赔率隐含概率失败: {e}")
            return None

    def _compute_fusion(
        self,
        model_probs: Tuple[float, float, float],
        odds_implied: Optional[Dict],
    ) -> Dict:
        """计算融合概率: 70% 赔率隐含 + 30% 模型"""
        h_m, d_m, a_m = model_probs

        if odds_implied is None:
            # 无赔率数据时，模型概率即为融合概率
            return {"H": h_m, "D": d_m, "A": a_m}

        h_o = odds_implied.get("H", h_m)
        d_o = odds_implied.get("D", d_m)
        a_o = odds_implied.get("A", a_m)

        w_o = self.FUSION_ODDS_WEIGHT
        w_m = self.FUSION_MODEL_WEIGHT

        h_f = h_o * w_o + h_m * w_m
        d_f = d_o * w_o + d_m * w_m
        a_f = a_o * w_o + a_m * w_m

        # 归一化
        total = h_f + d_f + a_f
        if total > 0:
            h_f /= total
            d_f /= total
            a_f /= total

        return {"H": h_f, "D": d_f, "A": a_f}

    def _compute_score_prediction(
        self, h_prob: float, d_prob: float, a_prob: float, league: Optional[str]
    ) -> Dict:
        """泊松比分预测"""
        try:
            from optimize.poisson_predictor import PoissonPredictor
            pp = PoissonPredictor()
            league_name = league or "default"
            scores = pp.predict_scores(h_prob, d_prob, a_prob, league_name, top_k=3)
            analysis = pp.full_analysis(h_prob, d_prob, a_prob, league_name)
            return {
                "top_scores": scores,
                "lambda": analysis.get("lambda", {}),
                "total_goals_expected": analysis.get("total_goals_expected", 0),
            }
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
            logger.warning(f"泊松比分预测失败: {e}")
            return {
                "top_scores": [],
                "lambda": {},
                "total_goals_expected": 0,
            }

    def _compute_over_under(
        self, h_prob: float, d_prob: float, a_prob: float, league: Optional[str]
    ) -> Dict:
        """大小球分析 (2.5 球盘)"""
        try:
            from optimize.poisson_predictor import PoissonPredictor
            pp = PoissonPredictor()
            league_name = league or "default"
            analysis = pp.full_analysis(h_prob, d_prob, a_prob, league_name)
            lam_h = analysis.get("lambda", {}).get("home", 1.0)
            lam_a = analysis.get("lambda", {}).get("away", 1.0)
            total_lambda = lam_h + lam_a

            # 泊松累积概率: P(总进球 ≤ 2) = under, P(总进球 ≥ 3) = over
            import math
            def poisson_pmf(k, lam) -> Optional[Dict]:
                if lam <= 0:
                    return 1.0 if k == 0 else 0.0
                return math.exp(-lam) * (lam ** k) / math.factorial(k)

            # 总进球服从 Poisson(total_lambda) 的近似
            # 更精确: 两个独立泊松之和仍为泊松
            under_prob = sum(poisson_pmf(k, total_lambda) for k in range(3))  # 0,1,2 球
            over_prob = 1.0 - under_prob

            return {
                "line": self.OVER_UNDER_LINE,
                "over_prob": round(over_prob, 4),
                "under_prob": round(under_prob, 4),
                "suggestion": "大" if over_prob > under_prob else "小",
                "total_expected": round(total_lambda, 2),
            }
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
            logger.warning(f"大小球分析失败: {e}")
            return {
                "line": self.OVER_UNDER_LINE,
                "over_prob": 0.5,
                "under_prob": 0.5,
                "suggestion": "—",
                "total_expected": 0,
            }

    def predict_batch(
        self, matches: List[Tuple[str, str, Optional[str]]]
    ) -> List[Dict]:
        """批量预测"""
        results = []
        for home, away, league in matches:
            result = self.predict_single(home, away, league)
            if result:
                results.append(result)
        return results

    def _build_features(
        self, home_team: str, away_team: str, league: Optional[str] = None
    ) -> Optional[Tuple[Any, Dict]]:
        """构造特征向量（复用 features 模块 + DB 数据）

        Returns:
            Tuple[features_dict, quality_meta] 或 None
            quality_meta 包含数据质量信息用于冷启动检测:
            - home_match_count / away_match_count: 历史比赛数
            - feature_coverage_ratio: 非默认特征比例 [0, 1]
            - is_cold_start: 是否冷启动(任一球队<3场或覆盖率<50%)
        """
        try:
            # backend/features/ 遮蔽了 footballAI/features/，用 importlib 直接从文件加载
            import importlib.util as _util
            import os as _os
            _project_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
            _fc_path = _os.path.join(_project_root, 'features', 'feature_calculator.py')
            _spec = _util.spec_from_file_location('features.feature_calculator', _fc_path)
            _fc_module = _util.module_from_spec(_spec)
            _spec.loader.exec_module(_fc_module)
            FeatureCalculator = _fc_module.FeatureCalculator

            from database.db_manager import get_db

            calc = FeatureCalculator()
            db = get_db()

            # 获取两队聚合特征
            home_data = db.get_team_features(home_team)
            away_data = db.get_team_features(away_team)

            # 注入 H2H 交锋优势
            home_data['h2h_advantage'] = db.get_h2h_advantage(home_team, away_team)
            away_data['h2h_advantage'] = -home_data['h2h_advantage']

            # 注入排名差
            home_data['rank_diff_from_db'] = db.get_rank_diff_factor(home_team, away_team)

            # ── 数据质量审计 ──
            home_mc = int(home_data.get('match_count') or 0) if home_data else 0
            away_mc = int(away_data.get('match_count') or 0) if away_data else 0

            # 计算非零/非默认字段比例
            _DEFAULT_SENTINELS = {0.0, 0.5, 1.0, None, '', 0, '0.0', '0.5'}
            home_non_default = sum(
                1 for v in (home_data or {}).values()
                if v not in _DEFAULT_SENTINELS
            )
            away_non_default = sum(
                1 for v in (away_data or {}).values()
                if v not in _DEFAULT_SENTINELS
            )
            total_fields = max(len(home_data or {}), len(away_data or {}), 1)
            feat_cov_ratio = (home_non_default + away_non_default) / (total_fields * 2)

            # 冷启动判定：任一球队历史<3场 OR 特征覆盖率<50%
            COLD_START_MATCH_THRESHOLD = 3
            COLD_START_COV_THRESHOLD = 0.50
            is_cold_start = (
                home_mc < COLD_START_MATCH_THRESHOLD
                or away_mc < COLD_START_MATCH_THRESHOLD
                or feat_cov_ratio < COLD_START_COV_THRESHOLD
            )

            if is_cold_start:
                logger.warning(
                    f"[COLD-START] {home_team}({home_mc}场) vs {away_team}({away_mc}场) | "
                    f"特征覆盖率={feat_cov_ratio:.1%} | "
                    f"{'主队无数据' if home_mc < COLD_START_MATCH_THRESHOLD else ''}"
                    f"{'客队无数据' if away_mc < COLD_START_MATCH_THRESHOLD else ''}"
                    f"{'特征坍缩' if feat_cov_ratio < COLD_START_COV_THRESHOLD else ''}".strip()
                )
            else:
                logger.info(
                    f"[特征构建] {home_team}({home_non_default}字段/{home_mc}场) "
                    f"vs {away_team}({away_non_default}字段/{away_mc}场) 覆盖率={feat_cov_ratio:.1%}"
                )

            # 构建特征向量
            features = calc.calculate_match_features(home_team, away_team, home_data, away_data)

            # ── v2.8 填充冷启动特征的实际值 ──
            import math as _math
            _home_mc = max(home_mc, 0)
            _away_mc = max(away_mc, 0)
            features['is_cold_start'] = 1.0 if is_cold_start else 0.0
            features['feat_coverage_ratio'] = round(feat_cov_ratio, 4)
            features['home_match_count_norm'] = round(_math.log1p(_home_mc) / _math.log1p(100), 4)  # 归一化到[0,1]
            features['away_match_count_norm'] = round(_math.log1p(_away_mc) / _math.log1p(100), 4)

            # ── v2.8 赔率驱动智能默认值 (冷启动补救) ──
            # 当特征坍缩(冷启动)且有赔率数据时，用赔率反推基本面因子替代中性默认值
            if is_cold_start:
                features = self._apply_smart_defaults(features, home_team, away_team)

            # 质量元数据
            quality_meta = {
                'home_match_count': int(home_mc or 0),
                'away_match_count': int(away_mc or 0),
                'feature_coverage_ratio': round(feat_cov_ratio, 4),
                'is_cold_start': is_cold_start,
                'home_fields_provided': home_non_default,
                'away_fields_provided': away_non_default,
            }

            return (features, quality_meta)
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
            logger.warning(f"特征构建失败: {e}", exc_info=True)
            return None

    def _query_odds_by_teams(self, db, home_team: str, away_team: str) -> Optional[Dict]:
        """
        通过队名查询赔率数据（内部辅助方法）

        get_latest_odds(match_id) 接受 match_id (int)，
        此方法先通过队名查找 match_id，再调用 get_latest_odds。
        """
        try:
            import sqlite3
            conn = sqlite3.connect(db.db_path)
            row = conn.execute(
                "SELECT match_id FROM matches WHERE home_team_name=? AND away_team_name=? LIMIT 1",
                (home_team, away_team)
            ).fetchone()
            conn.close()
            if row and row[0]:
                return db.get_latest_odds(row[0])
            return None
        except (sqlite3.Error, AttributeError, KeyError) as e:
            logger.debug(f"获取最新赔率失败: {e}")
            return None

    def _apply_smart_defaults(
        self, features: Dict[str, float], home_team: str, away_team: str
    ) -> Dict[str, float]:
        """
        v2.8: 赔率驱动的智能默认值 — 冷启动补救机制

        当新球队(如卡塔尔)无历史数据时，特征向量充满中性默认值(0.5/0.0)，
        导致模型输出接近均匀分布。此方法从赔率反推基本面因子，
        给模型至少一个有方向性的信号。

        核心映射 (赔率隐含概率 → 特征):
          - imp_a > 0.55 → 客队强 → a2(away视角)偏高, rank_factor偏低
          - imp_h > 0.55 → 主队强 → a2(home视角)偏高, rank_factor偏高
          - imp_d > 0.25 → 平局概率高 → a3(市场情绪)偏向中性

        Args:
            features: 原始特征字典（可能充满默认值）
            home_team / away队: 球队名（用于查赔率）
        Returns:
            修正后的特征字典
        """
        try:
            import sqlite3
            from database.db_manager import get_db
            db = get_db()

            # 通过队名查找 match_id，再获取赔率数据
            conn = sqlite3.connect(db.db_path)
            row = conn.execute(
                "SELECT match_id FROM matches WHERE home_team_name=? AND away_team_name=? LIMIT 1",
                (home_team, away_team)
            ).fetchone()
            conn.close()

            if not row or not row[0]:
                logger.debug(f"[SmartDefaults] {home_team} vs {away_team} 无比赛记录，跳过")
                return features

            odds_data = db.get_latest_odds(row[0])  # get_latest_odds 接受 match_id (int)
            if not odds_data:
                logger.debug(f"[SmartDefaults] {home_team} vs {away_team} 无赔率数据，跳过")
                return features

            o_h = odds_data.get("home_odds", 0)
            o_d = odds_data.get("draw_odds", 0)
            o_a = odds_data.get("away_odds", 0)

            if o_h <= 0 or o_d <= 0 or o_a <= 0:
                return features

            # 计算隐含概率
            raw_h = 1.0 / o_h
            raw_d = 1.0 / o_d
            raw_a = 1.0 / o_a
            total = raw_h + raw_d + raw_a
            imp_h = raw_h / total
            imp_d = raw_d / total
            imp_a = raw_a / total

            # ── 从赔率反推基本面因子 ──
            # a2 (基本面优势): 将隐含概率映射到 [0, 1]
            # imp_h 高 → 主队a2高; imp_a 高 → 客队a2高(即主队a2低)
            # 基准=0.5(均势), 偏移量与赔率偏离1/3的程度成正比
            odds_bias_home = imp_h - imp_a  # 正=主队热门, 负=客队热门

            # a2: 基本面优势因子 [0, 1], 0.5=均势
            smart_a2 = 0.5 + odds_bias_home * 0.6  # 缩放因子让信号更强但不过激
            smart_a2 = max(0.05, min(0.95, smart_a2))

            # rank_factor: 排名因子 [0, 1], 0.5=均势
            # 与a2同向但幅度略小(排名变化慢于实力差距感知)
            smart_rank = 0.5 + odds_bias_home * 0.4
            smart_rank = max(0.1, min(0.9, smart_rank))

            # form_factor: 表单因子 [0, 1], 0.5=平均水平
            smart_form = 0.5 + odds_bias_home * 0.3
            smart_form = max(0.15, min(0.85, smart_form))

            # a3: 市场情绪因子 — 平局隐含概率偏离1/3的程度
            d_deviation = abs(imp_d - 0.333) * 3  # 归一化到 [0, 1]
            if imp_d > 0.25:
                # 平局被看好 → 市场认为势均力敌 → a3趋向中性偏稳
                smart_a3 = 0.5 - d_deviation * 0.15  # 略偏保守
            else:
                # 平局不被看好 → 市场有明确倾向
                smart_a3 = 0.5 + odds_bias_home * 0.35
            smart_a3 = max(0.1, min(0.9, smart_a3))

            # a1: 盘口价值因子 — 用odds_spread近似
            odds_spread = abs(raw_h - raw_a) / total  # [0, ~0.8]
            smart_a1 = (imp_h - imp_a)  # 直接用隐含概率差作为盘口方向

            # home_strength / away_strength
            smart_home_str = smart_a2 * 0.4 + smart_rank * 0.3 + smart_form * 0.3
            smart_away_str = (1 - smart_a2) * 0.4 + (1 - smart_rank) * 0.3 + (1 - smart_form) * 0.3

            # ── 应用智能默认值（仅覆盖明显为中性默认值的特征）──
            _NEUTRAL_DEFAULTS = {0.0, 0.5, 1.0}
            patched = {}

            # 检测哪些特征是"坍缩"的(接近中性默认且没有有效数据源)
            collapsed_features = {
                'a2': ('a2', smart_a2),
                'rank_factor': ('rank_factor', smart_rank),
                'form_factor': ('form_factor', smart_form),
                'a3': ('a3', smart_a3),
                'a1': ('a1', smart_a1),
                'home_strength': ('home_strength', smart_home_str),
                'away_strength': ('away_strength', smart_away_str),
            }

            n_patched = 0
            for feat_key, (_, smart_val) in collapsed_features.items():
                current_val = features.get(feat_key)
                # 只在当前值接近默认值时才覆盖（避免覆盖真实计算的特征）
                if current_val is not None and abs(current_val - 0.5) < 0.08:
                    old_val = features[feat_key]
                    features[feat_key] = round(smart_val, 4)
                    patched[feat_key] = f"{old_val:.3f}→{smart_val:.3f}"
                    n_patched += 1

            if n_patched > 0:
                logger.info(
                    f"[SmartDefaults] {home_team} vs {away_team} | "
                    f"赔率H/D/A={imp_h:.1%}/{imp_d:.1%}/{imp_a:.1%} | "
                    f"修补{n_patched}个坍缩特征: {patched}"
                )

            return features
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
            logger.debug(f"[SmartDefaults] 智能默认值失败（非致命）: {e}")
            return features

    def _build_odds_features(
        self, home_team: str, away_team: str, league: Optional[str] = None
    ) -> Optional[Dict[str, float]]:
        """
        v2.7: 构造 OddsExpert 赔率专精特征 (training_extended 路径, 16 cols)

        从数据库 odds 表 / match_features 表提取纯赔率特征：
        - odds_imp_h/d/a: 赔率隐含概率
        - odds_spread/overround/draw_dev: 赔率结构特征
        - odds_confidence: 赔率置信度
        - drift_h/d/a: 赔率漂移
        - drift_magnitude/direction/sharp_signal: 漂移衍生特征
        - otsm_*: OTSM 状态机特征
        """
        try:
            from database.db_manager import get_db
            db = get_db()

            odds_features = {}

            # 1. 从 odds 表获取最新赔率
            odds_data = self._query_odds_by_teams(db, home_team, away_team)
            if odds_data:
                home_odds = odds_data.get("home_odds", 0)
                draw_odds = odds_data.get("draw_odds", 0)
                away_odds = odds_data.get("away_odds", 0)
                if home_odds > 0 and draw_odds > 0 and away_odds > 0:
                    # 计算隐含概率（去抽水）
                    raw_h = 1.0 / home_odds
                    raw_d = 1.0 / draw_odds
                    raw_a = 1.0 / away_odds
                    total = raw_h + raw_d + raw_a
                    odds_features['odds_imp_h'] = raw_h / total
                    odds_features['odds_imp_d'] = raw_d / total
                    odds_features['odds_imp_a'] = raw_a / total
                    odds_features['odds_spread'] = abs(raw_h - raw_a) / total
                    odds_features['odds_overround'] = total - 1.0
                    odds_features['odds_draw_dev'] = (1.0 / draw_odds) / 3 - odds_features['odds_imp_d']
                    odds_features['odds_confidence'] = (
                        (odds_features['odds_imp_h'] - 1/3)**2 +
                        (odds_features['odds_imp_d'] - 1/3)**2 +
                        (odds_features['odds_imp_a'] - 1/3)**2
                    ) ** 0.5 * 3.0

            # 2. 从 match_features 表获取 OTSM + drift 特征
            try:
                import sqlite3
                conn = sqlite3.connect(db.db_path)
                c = conn.cursor()
                # 查找该比赛的 match_features 记录
                c.execute("""
                    SELECT mf.otsm_lock_confidence, mf.otsm_entropy_drift,
                           mf.otsm_water_accel, mf.otsm_kelly_fluct,
                           mf.otsm_state_LOCKED, mf.otsm_state_ACTIVE, mf.otsm_state_NOISE,
                           mf.otsm_n_snapshots_norm, mf.otsm_entropy_rate,
                           mf.drift_magnitude, mf.drift_direction, mf.drift_sharp_signal,
                           mf.drift_h, mf.drift_d, mf.drift_a
                    FROM match_features mf
                    JOIN matches m ON mf.match_id = m.match_id
                    WHERE m.home_team_name = ? AND m.away_team_name = ?
                    ORDER BY m.match_date DESC LIMIT 1
                """, (home_team, away_team))
                row = c.fetchone()
                conn.close()
                if row:
                    field_names = [
                        'otsm_lock_confidence', 'otsm_entropy_drift',
                        'otsm_water_accel', 'otsm_kelly_fluct',
                        'otsm_state_LOCKED', 'otsm_state_ACTIVE', 'otsm_state_NOISE',
                        'otsm_n_snapshots_norm', 'otsm_entropy_rate',
                        'drift_magnitude', 'drift_direction', 'drift_sharp_signal',
                        'drift_h', 'drift_d', 'drift_a'
                    ]
                    for i, name in enumerate(field_names):
                        if row[i] is not None:
                            odds_features[name] = float(row[i])
            except (OSError, ValueError, KeyError) as e:
                logger.debug(f"操作失败: {e}")

            # 只有至少 5 个特征才算有效
            if len(odds_features) >= 5:
                return odds_features
            return None

        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError, ValueError, TypeError) as e:
            logger.debug(f"赔率特征构建失败: {e}")
            return None

    def get_history(
        self, limit: int = 50, league: Optional[str] = None
    ) -> List[Dict]:
        """获取历史预测 (v3.0: 规范化字段名)"""
        try:
            from database.db_manager import get_db
            db = get_db()
            rows = db.get_recent_predictions(limit=limit, league=league)
            # 规范化字段名: home_team_name → home_team, league_name → league
            result = []
            for r in rows:
                item = dict(r)
                item['home_team'] = item.pop('home_team_name', item.get('home_team'))
                item['away_team'] = item.pop('away_team_name', item.get('away_team'))
                item['league'] = item.pop('league_name', item.get('league'))
                # 确保 lock_confidence 字段存在
                if 'lock_confidence' not in item:
                    item['lock_confidence'] = 0.0
                result.append(item)
            return result
        except (sqlite3.Error, KeyError, AttributeError) as e:
            logger.debug(f"获取历史预测失败: {e}")
            return []

    def get_stats(self) -> Dict:
        """获取预测统计"""
        try:
            from database.db_manager import get_db
            db = get_db()
            return db.get_prediction_stats()
        except (sqlite3.Error, AttributeError) as e:
            logger.debug(f"获取预测统计失败: {e}")
            return {"total": 0, "accuracy": 0}
