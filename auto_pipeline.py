"""
哨响AI - 自动预测+回测+入库闭环管道 v1.0
=======================================
流程: API拉取比赛 → 特征计算 → 预测生成 → 结果回测 → 数据库更新 → 触发迭代训练

使用方法:
    python auto_pipeline.py              # 运行一次完整管道
    python auto_pipeline.py --daemon      # 守护模式，每30分钟自动运行
    python auto_pipeline.py --backtest    # 仅回测已结束的比赛
    python auto_pipeline.py --report      # 生成准确率报告
"""

from __future__ import annotations

import os
import sys
import json
import math
import time
import logging
import argparse
from datetime import datetime, timedelta
from typing import Any

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.api_config import API_CONFIG, LEAGUES, SYSTEM_PARAMS  # pyright: ignore[reportImplicitRelativeImport]
from data_collector.main import FootballDataCollector  # pyright: ignore[reportImplicitRelativeImport]
from database.db_manager import DatabaseManager  # pyright: ignore[reportImplicitRelativeImport]
from features.feature_calculator import FeatureCalculator  # pyright: ignore[reportImplicitRelativeImport]

# ModelBridge — 真实ML模型预测入口（可选，缺失时退化到启发式）
try:
    from agents.model_bridge import get_model_bridge
    _HAVE_MODEL_BRIDGE = True
except (Exception, KeyError, IndexError):  # pyright: ignore[reportBroadException]
    _HAVE_MODEL_BRIDGE = False

# 日志配置
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'auto_pipeline.log'), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class AutoPipeline:
    """自动预测-回测管道"""

    def __init__(self):
        self.api_key: str = str(API_CONFIG['primary']['api_key'])
        self.base_url: str = str(API_CONFIG['primary']['base_url'])
        self.leagues: dict[str, Any] = LEAGUES
        self.params: dict[str, Any] = SYSTEM_PARAMS
        self.db: DatabaseManager = DatabaseManager()
        self.collector: FootballDataCollector | None = (
            FootballDataCollector(self.api_key, self.base_url) if self.api_key else None
        )
        self.feature_calc: FeatureCalculator = FeatureCalculator()

        # ModelBridge — 真实ML模型（缺失时降级到启发式）
        self.model_bridge = None
        if _HAVE_MODEL_BRIDGE:
            try:
                self.model_bridge = get_model_bridge()  # 可能抛 ModelNotAvailableError
                if self.model_bridge and self.model_bridge.available:
                    logger.info(f"[AutoPipeline] ModelBridge 已加载: {self.model_bridge.model_name} v{self.model_bridge.model_version}")
                else:
                    self.model_bridge = None
                    logger.warning("[AutoPipeline] ModelBridge 加载但未就绪，将使用启发式预测")
            except (Exception, KeyError, IndexError) as e:
                self.model_bridge = None
                logger.warning(f"[AutoPipeline] ModelBridge 不可用，将使用启发式预测: {e}")
        else:
            logger.info("[AutoPipeline] ModelBridge 模块未安装，使用启发式预测")

        # 统计
        self.stats: dict[str, Any] = {
            'matches_fetched': 0,
            'predictions_made': 0,
            'backtests_done': 0,
            'correct': 0,
            'total_evaluated': 0,
            'errors': [],
            'start_time': datetime.now().isoformat(),
        }

    # ==================== 步骤1: 数据采集 ====================

    def fetch_live_and_upcoming(self) -> list[dict[str, Any]]:
        """
        拉取所有配置联赛的实时比赛 + 未来赛程。
        优先使用 API，API 不可用时使用数据库已有数据。
        """
        all_matches: list[dict[str, Any]] = []
        priority_leagues = ['premier_league', 'la_liga', 'serie_a', 'bundesliga', 'ligue_1', 'champions_league',
                            'brasileirao', 'eredivisie', 'primeira_liga', 'championship', 'mls', 'csl',
                            'world_cup', 'european_championship']

        for league_key in priority_leagues:
            league = self.leagues.get(league_key)
            if not league:
                continue

            league_id = int(league['id'])
            league_name = league.get('name_cn', league.get('name', ''))

            try:
                if self.collector and self.api_key:
                    # 拉取最近7天 + 未来7天的比赛
                    today = datetime.now()
                    date_from = (today - timedelta(days=7)).strftime('%Y-%m-%d')
                    date_to = (today + timedelta(days=7)).strftime('%Y-%m-%d')

                    logger.info(f"[采集] {league_name} ({league_id}): {date_from} ~ {date_to}")
                    matches = self.collector.get_matches(league_id, date_from, date_to)

                    if matches:
                        # 入库
                        with self.db.transaction() as conn:
                            for m in matches:
                                try:
                                    m['league_id'] = league_id
                                    m['league_name'] = league_name
                                    self.db.add_match_tx(conn, m)
                                except (Exception, KeyError, IndexError) as e:
                                    logger.debug(f"入库跳过: {m.get('home_team_name')} vs {m.get('away_team_name')}: {e}")

                        all_matches.extend(matches)
                        self.stats['matches_fetched'] += len(matches)
                        logger.info(f"  ✓ {league_name}: {len(matches)} 场比赛")
                    else:
                        logger.warning(f"  ✗ {league_name}: API 无数据，回退数据库")
                        db_matches = self._fallback_db_matches(league_id)
                        all_matches.extend(db_matches)

                else:
                    logger.info(f"[本地缓存] {league_name}: 无 API Key，读取数据库已有数据")
                    db_matches = self._fallback_db_matches(league_id)
                    all_matches.extend(db_matches)

            except (Exception, KeyError, IndexError) as e:
                logger.error(f"[采集失败] {league_name}: {e}")
                self.stats['errors'].append(f"采集失败 {league_name}: {str(e)[:100]}")
                db_matches = self._fallback_db_matches(league_id)
                all_matches.extend(db_matches)

            # 速率限制
            time.sleep(3)

        return all_matches

    def _fallback_db_matches(self, league_id: int) -> list[dict[str, Any]]:
        """API不可用时从数据库获取比赛"""
        try:
            db_matches = self.db.get_matches(league_id=league_id, limit=50)
            result: list[dict[str, Any]] = []
            for m in db_matches:
                result.append({
                    'match_id': m.get('match_id'),
                    'match_date': m.get('match_date', ''),
                    'league_id': league_id,
                    'league_name': m.get('league_name', ''),
                    'home_team_id': m.get('home_team_id'),
                    'home_team_name': m.get('home_team_name'),
                    'away_team_id': m.get('away_team_id'),
                    'away_team_name': m.get('away_team_name'),
                    'status': m.get('status', 'scheduled'),
                    'home_score': m.get('home_score'),
                    'away_score': m.get('away_score'),
                    'match_time': m.get('match_time'),
                })
            if result:
                logger.info(f"  ✓ 数据库回退: {len(result)} 场比赛")
            return result
        except (Exception, requests.exceptions.RequestException) as e:
            logger.error(f"数据库回退失败: {e}")
            return []

    # ==================== 步骤2: 特征计算 ====================

    def compute_features(self, match_id: int, match_data: dict[str, Any]) -> dict[str, Any] | None:
        """为单场比赛计算特征"""
        try:
            # 检查是否已有特征
            existing = self.db.get_features(match_id)
            if existing:
                return dict(existing)

            # 使用特征计算器
            features = self.feature_calc.calculate_all_features(match_data)

            if features:
                # 保存到数据库
                features['match_id'] = match_id
                with self.db.get_connection() as conn:
                    self.db.save_features_tx(conn, features)
                return features

            return None
        except (Exception, KeyError, IndexError) as e:
            logger.debug(f"特征计算失败 match_id={match_id}: {e}")
            return None

    # ==================== 步骤3: 预测生成 ====================

    def predict_match(self, match_id: int, features: dict[str, Any], match_data: dict[str, Any]) -> dict[str, Any] | None:
        """
        基于特征进行预测。
        优先级: ML模型(XGBoost) → 赔率反推 → 修正版启发式(兜底)

        v3.1 修复:
            - ML模型不可用时，优先使用赔率反推（准确率~49%，远优于旧heuristic的38%）
            - 修复旧softmax偏差（home_score=adv*2.0导致exp爆炸全预测H）
            - 启发式改用线性缩放+平局基线，不再用softmax
        """
        try:
            # 检查是否已有预测
            with self.db.get_connection() as check_conn:
                existing = check_conn.execute(
                    'SELECT * FROM predictions WHERE match_id=? ORDER BY prediction_time DESC LIMIT 1',
                    (match_id,)
                ).fetchone()
            if existing:
                return dict(existing)

            # --- 三级预测优先级 ---
            home_prob = None       # 百分比 0-100
            draw_prob_pct = None   # 百分比 0-100
            away_prob = None       # 百分比 0-100
            model_version = 'unknown'
            advantage_score = 0.0
            best_outcome = 'home'
            prediction_label = 'H'

            # ═══════ 优先级1: ML模型 (XGBoost) ═══════
            if self.model_bridge and self.model_bridge.available:
                try:
                    mb_result = self.model_bridge.predict(features)
                    if mb_result:
                        # ── DomainKB 修正 ──
                        try:
                            from rules.domain_rules import apply_domain_knowledge
                            domain_features = {
                                **features,
                                'home_team_name': match_data.get('home_team_name') or features.get('home_team_name'),
                                'away_team_name': match_data.get('away_team_name') or features.get('away_team_name'),
                            }
                            kb_result = apply_domain_knowledge(domain_features, mb_result)
                            home_prob = round(kb_result['home'] * 100, 1)
                            draw_prob_pct = round(kb_result['draw'] * 100, 1)
                            away_prob = round(kb_result['away'] * 100, 1)
                        except (Exception, KeyError, IndexError, requests.exceptions.RequestException):
                            home_prob = round(mb_result['home'] * 100, 1)
                            draw_prob_pct = round(mb_result['draw'] * 100, 1)
                            away_prob = round(mb_result['away'] * 100, 1)
                        model_version = mb_result.get('_model', 'xgb_production')
                        logger.debug(f"[predict] ML模型预测 match_id={match_id}: H={home_prob} D={draw_prob_pct} A={away_prob}")
                except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
                    logger.warning(f"[predict] ML模型预测失败 match_id={match_id}: {e}")

            # ═══════ 优先级2: 赔率反推 (准确率~49%) ═══════
            if home_prob is None:
                odds = self.db.get_latest_odds(match_id) or {}
                ho = odds.get('home_odds')
                do = odds.get('draw_odds')
                ao = odds.get('away_odds')
                if ho and do and ao and ho > 1 and do > 1 and ao > 1:
                    ret = odds.get('return_rate', 0.95) or 0.95
                    imp_h = 1 / ho / ret
                    imp_d = 1 / do / ret
                    imp_a = 1 / ao / ret
                    total_imp = imp_h + imp_d + imp_a
                    # 归一化到100%
                    home_prob = round(imp_h / total_imp * 100, 1)
                    draw_prob_pct = round(imp_d / total_imp * 100, 1)
                    away_prob = round(imp_a / total_imp * 100, 1)
                    model_version = 'odds_implied_v1'
                    logger.debug(f"[predict] 赔率反推 match_id={match_id}: H={home_prob} D={draw_prob_pct} A={away_prob}")

            # ═══════ 优先级3: 修正版启发式 (兜底) ═══════
            # ★ v3.1 修复: 不再使用旧softmax（exp(adv*2)导致A≈0.009）
            # 改用线性缩放 + 平局基线，确保 H/D/A 都有合理概率
            if home_prob is None:
                a1 = features.get('a1', 0)
                a2 = features.get('a2', 0.5)
                a3 = features.get('a3', 0.5)
                rank_diff = features.get('rank_diff_factor', 0)
                form_momentum = features.get('form_momentum', 0)
                h2h = features.get('h2h_factor', 0)

                advantage_score = (
                    0.25 * a1 +
                    0.30 * a2 +
                    0.20 * a3 +
                    0.10 * rank_diff +
                    0.10 * form_momentum +
                    0.05 * h2h
                )

                # ★ 修复: 线性缩放代替softmax
                # advantage_score 范围约 [-2, +2]
                # 正值→主队优势，负值→客队优势
                # 基线: H=40%, D=28%, A=32% (参考历史平均分布)
                # 缩放: advantage_score * 12% 调整主/客概率，平局保持稳定
                base_h = 40.0
                base_d = 28.0
                base_a = 32.0
                shift = advantage_score * 12.0  # ±24% 范围

                home_prob = round(max(5.0, min(90.0, base_h + shift)), 1)
                away_prob = round(max(5.0, min(90.0, base_a - shift)), 1)
                draw_prob_pct = round(max(10.0, min(40.0, base_d - abs(shift) * 0.3)), 1)

                # 归一化到100%
                total_pct = home_prob + draw_prob_pct + away_prob
                if total_pct > 0:
                    home_prob = round(home_prob / total_pct * 100, 1)
                    draw_prob_pct = round(draw_prob_pct / total_pct * 100, 1)
                    away_prob = round(away_prob / total_pct * 100, 1)

                # 四舍五入误差修正
                total_pct = home_prob + draw_prob_pct + away_prob
                if abs(total_pct - 100) > 0.05:
                    home_prob = round(100 - draw_prob_pct - away_prob, 1)

                model_version = 'heuristic_v3.1'

            # --- 确定预测方向 ---
            probs_01: dict[str, float] = {'home': home_prob / 100, 'draw': draw_prob_pct / 100, 'away': away_prob / 100}
            best_outcome = max(probs_01, key=lambda k: probs_01[k])
            prediction_label = {'home': 'H', 'draw': 'D', 'away': 'A'}[best_outcome]

            # --- 特征可靠性检测 ---
            _feat_unreliable = (
                features.get('form_momentum', 0) == 0 and
                features.get('h2h_factor', 0) == 0 and
                features.get('rank_diff_factor', 0) == 0
            )
            _force_skip = False
            if _feat_unreliable and model_version.startswith('heuristic'):
                logger.warning(f"[predict] 特征不可靠且无ML/赔率预测，强制SKIP match_id={match_id}")
                _force_skip = True

            # --- 计算价值指标 ---
            value_gap = 0.0
            confidence = 0.0
            kelly = 0.0
            decision = 'PASS'
            market_home_prob = 1.0 / 3
            market_draw_prob = 1.0 / 3
            market_away_prob = 1.0 / 3

            if _force_skip:
                decision = 'SKIP'
                confidence = 0.0
                value_gap = -1.0
            else:
                odds = self.db.get_latest_odds(match_id) or {}
                if odds.get('home_odds') and odds.get('draw_odds') and odds.get('away_odds'):
                    market_home = 1 / odds['home_odds']
                    market_draw = 1 / odds['draw_odds']
                    market_away = 1 / odds['away_odds']
                    market_total = market_home + market_draw + market_away
                    market_home_prob = market_home / market_total
                    market_draw_prob = market_draw / market_total
                    market_away_prob = market_away / market_total
                else:
                    market_home_prob = 1.0 / 3
                    market_draw_prob = 1.0 / 3
                    market_away_prob = 1.0 / 3

                market_prob_map = {
                    'home': market_home_prob,
                    'draw': market_draw_prob,
                    'away': market_away_prob,
                }
                value_gap = probs_01[best_outcome] - market_prob_map[best_outcome]

                # 凯利指数
                kelly_odds = odds.get('home_odds' if best_outcome == 'home' else ('draw_odds' if best_outcome == 'draw' else 'away_odds'), 2.0)
                if kelly_odds and kelly_odds > 0:
                    kelly = (probs_01[best_outcome] * kelly_odds - 1) / (kelly_odds - 1)
                    kelly = max(0, min(kelly * 0.5, 0.1))
                else:
                    kelly = 0

                # 置信度 — 0-1 小数格式
                max_prob_decimal = max(home_prob, draw_prob_pct, away_prob) / 100.0
                confidence = round(0.40 + max_prob_decimal * 0.60, 4)

                # ── 决策门控 v2.0 (多信号独立门控) ──
                # 旧方案: value_gap + confidence 自证循环 (INVEST 49.73% < PASS 63.25%)
                # 新方案: 模型-赔率一致 + 市场稳定 + 历史先验 三道独立门
                odds_probs = {
                    'home': market_home_prob,
                    'draw': market_draw_prob,
                    'away': market_away_prob,
                }
                gate_result = self._invest_gate_v2(
                    probs_01, odds_probs, features,
                    match_data.get('league_name', ''), match_id
                )
                decision = gate_result.decision
                value_gap = gate_result.value_gap
                kelly = gate_result.kelly
                _gate_info = {
                    'g1': int(gate_result.gate1_passed),
                    'g2': int(gate_result.gate2_passed),
                    'g3': int(gate_result.gate3_passed),
                    'model_dir': gate_result.model_direction,
                    'odds_dir': gate_result.odds_direction,
                    'g1_detail': gate_result.gate1_detail,
                    'g2_detail': gate_result.gate2_detail,
                    'g3_detail': gate_result.gate3_detail,
                }

            prediction = {
                'match_id': match_id,
                'model_version': model_version,
                'prediction_time': datetime.now().isoformat(),
                'prediction': prediction_label,
                'home_prob': home_prob,
                'draw_prob': draw_prob_pct,
                'away_prob': away_prob,
                'value_gap': round(value_gap, 4),
                'kelly_percentage': round(kelly, 4),
                'expected_value': round(value_gap * 1.2, 4),
                'decision': decision,
                'confidence_level': confidence,
                'investment_amount': round(kelly * 1000, 2) if decision == 'INVEST' else 0,
                'gate1_pass': _gate_info.get('g1', 0) if '_gate_info' in dir() else (1 if value_gap > 0.02 else 0),
                'gate2_pass': _gate_info.get('g2', 0) if '_gate_info' in dir() else (1 if confidence >= 0.55 else 0),
                'gate3_pass': _gate_info.get('g3', 0) if '_gate_info' in dir() else (1 if kelly > 0 else 0),
                'score_predictions': json.dumps({
                    'advantage_score': round(advantage_score, 4),
                    'predicted_winner': best_outcome,
                    'model_source': model_version,
                    'market_implied': {
                        'home': round(market_home_prob * 100, 1),
                        'draw': round(market_draw_prob * 100, 1),
                        'away': round(market_away_prob * 100, 1),
                    },
                    'gate_v2': _gate_info if '_gate_info' in dir() else {},
                }),
            }

            # 入库
            with self.db.get_connection() as conn:
                self.db.save_prediction_tx(conn, prediction)

            self.stats['predictions_made'] += 1
            return prediction

        except (Exception, KeyError, IndexError) as e:
            logger.error(f"预测失败 match_id={match_id}: {e}")
            return None

    # ==================== 步骤3.5: INVEST门控v2 ====================

    def _invest_gate_v2(self, model_probs: dict, odds_probs: dict,
                         features: dict, league_name: str,
                         match_id: int = None):
        """
        INVEST 决策门控 v2.0 — 多信号独立门控

        替代旧的自证循环逻辑 (value_gap + confidence, INVEST 49.73% < PASS 63.25%)
        三道独立信号门:
            Gate1: 模型-赔率方向一致 (赔率是独立信号)
            Gate2: 市场低波动 (盘口波动是独立信号)
            Gate3: 历史先验支撑 (历史数据是独立信号)
        """
        try:
            from features.invest_gate import InvestGateV2, GateResult
            db_path = self.db.db_path if hasattr(self.db, 'db_path') else None
            gate = InvestGateV2(db_path=db_path)
            result = gate.screen(
                model_probs=model_probs,
                odds_probs=odds_probs,
                features=features,
                league_name=league_name,
                match_id=match_id,
            )
            logger.debug(
                f"[GateV2] match_id={match_id}: "
                f"decision={result.decision} "
                f"G1={result.gate1_passed} G2={result.gate2_passed} G3={result.gate3_passed} "
                f"| model={result.model_direction} odds={result.odds_direction}"
            )
            return result
        except ImportError:
            logger.warning("[GateV2] invest_gate 模块未找到, 回退旧逻辑")
            # 回退: 用旧逻辑
            best = max(model_probs, key=model_probs.get)
            vg = model_probs[best] - odds_probs.get(best, 0.33)
            from features.invest_gate import GateResult
            if vg > 0.03:
                return GateResult('INVEST', 'HIGH', True, True, True,
                                  'fallback', 'fallback', 'fallback',
                                  best.upper(), best.upper(), vg, 0.05)
            elif vg > 0.01:
                return GateResult('WATCH', 'MEDIUM', True, False, False,
                                  'fallback', 'fallback', 'fallback',
                                  best.upper(), best.upper(), vg, 0.0)
            else:
                return GateResult('PASS', 'LOW', False, False, False,
                                  'fallback', 'fallback', 'fallback',
                                  best.upper(), best.upper(), vg, 0.0)

    # ==================== 步骤4: 回测验证 ====================

    def backtest_finished_matches(self) -> dict[str, Any]:
        """
        回测所有已结束且有预测的比赛。
        对比预测结果 vs 实际结果，更新 is_correct 和 profit_loss。
        """
        try:
            with self.db.get_connection() as conn:
                # 找到已结束但未标记预测结果的比赛
                rows = conn.execute('''
                    SELECT m.match_id, m.home_team_name, m.away_team_name,
                           m.home_score, m.away_score, m.final_result,
                           p.prediction_id, p.home_prob, p.draw_prob, p.away_prob,
                           p.decision, p.value_gap, p.kelly_percentage,
                           p.home_odds, p.draw_odds, p.away_odds
                    FROM matches m
                    JOIN predictions p ON m.match_id = p.match_id
                    WHERE m.status = 'finished'
                      AND m.home_score IS NOT NULL
                      AND m.away_score IS NOT NULL
                      AND p.is_correct IS NULL
                ''').fetchall()

                results = []
                correct = 0
                total = len(rows)

                for row in rows:
                    row_dict = dict(row)
                    match_id = row_dict['match_id']
                    pred_id = row_dict['prediction_id']

                    # 确定实际结果
                    home_s = row_dict['home_score']
                    away_s = row_dict['away_score']
                    if home_s > away_s:
                        actual = 'H'
                    elif home_s < away_s:
                        actual = 'A'
                    else:
                        actual = 'D'

                    # 确定预测结果
                    home_p = row_dict['home_prob'] or 33
                    draw_p = row_dict['draw_prob'] or 34
                    away_p = row_dict['away_prob'] or 33
                    pred_map: dict[str, float] = {'H': float(home_p), 'D': float(draw_p), 'A': float(away_p)}
                    predicted = max(pred_map, key=lambda k: pred_map[k])

                    is_correct = 1 if predicted == actual else 0
                    if is_correct:
                        correct += 1

                    # 计算盈亏（模拟投注100元，使用实际赔率）
                    decision = row_dict['decision']
                    if decision == 'INVEST':
                        # 根据预测方向获取对应赔率
                        odds_map = {
                            'H': row_dict.get('home_odds'),
                            'D': row_dict.get('draw_odds'),
                            'A': row_dict.get('away_odds'),
                        }
                        actual_odds = odds_map.get(predicted)
                        if is_correct:
                            if actual_odds and actual_odds > 0:
                                profit_loss = round(100 * (actual_odds - 1), 2)
                            else:
                                # 赔率缺失时回退到默认 2.0
                                profit_loss = round(100 * 0.95, 2)
                        else:
                            profit_loss = -100.0
                    else:
                        profit_loss = 0.0

                    # 更新数据库
                    conn.execute('''
                        UPDATE predictions
                        SET actual_result = ?, is_correct = ?, profit_loss = ?
                        WHERE prediction_id = ?
                    ''', (actual, is_correct, profit_loss, pred_id))

                    # 更新比赛的 final_result
                    if not row_dict.get('final_result'):
                        conn.execute('''
                            UPDATE matches SET final_result = ? WHERE match_id = ?
                        ''', (actual, match_id))

                    results.append({
                        'match_id': match_id,
                        'match': f"{row_dict['home_team_name']} vs {row_dict['away_team_name']}",
                        'score': f"{home_s}-{away_s}",
                        'actual': actual,
                        'predicted': predicted,
                        'is_correct': bool(is_correct),
                        'home_prob': home_p,
                        'draw_prob': draw_p,
                        'away_prob': away_p,
                    })

                if total > 0:
                    accuracy = round(correct / total * 100, 2)
                    logger.info(f"[回测] {total} 场比赛, 正确 {correct}, 准确率 {accuracy}%")
                else:
                    accuracy = None

                self.stats['backtests_done'] = total
                self.stats['correct'] = correct
                self.stats['total_evaluated'] = total

                return {
                    'total': total,
                    'correct': correct,
                    'accuracy': accuracy,
                    'results': results,
                }

        except (Exception, KeyError, IndexError) as e:
            logger.error(f"回测失败: {e}")
            self.stats['errors'].append(f"回测失败: {str(e)[:100]}")
            return {'total': 0, 'correct': 0, 'accuracy': None, 'results': [], 'error': str(e)}

    # ==================== 步骤5: 自动迭代训练 ====================

    def auto_retrain_if_needed(self, backtest_result: dict[str, Any]) -> dict[str, Any] | None:
        """
        当回测样本足够时触发自动重训练。
        使用 EnsembleTrainer v3.0 的标准化 pipeline 格式。
        [修复 v3.1]: 时序分割替代随机分割 + 全19特征 + joblib pipeline 序列化
        """
        total = backtest_result.get('total', 0)
        if total < 200:
            logger.info(f"[训练跳过] 样本不足 ({total} < 200，需要至少200条回测记录)")
            return None

        try:
            # 使用统一的 config.yaml 特征列表
            import yaml
            config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            feature_cols = config['data']['feature_columns']  # 19个特征
            defaults = config['data']['default_values']

            # 从DB加载数据（按时间排序以确保时序分割正确）
            with self.db.get_connection() as conn:
                cols_sql = ", ".join([f"mf.{c}" for c in feature_cols])
                rows = conn.execute(f'''
                    SELECT m.match_id, m.match_date, m.home_score, m.away_score,
                           m.league_name, m.home_team_name, m.away_team_name,
                           {cols_sql}
                    FROM matches m
                    JOIN match_features mf ON m.match_id = mf.match_id
                    WHERE m.status = 'finished'
                      AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                    ORDER BY m.match_date ASC
                ''').fetchall()

            if len(rows) < 50:
                logger.info(f"[训练跳过] 训练数据不足 ({len(rows)} < 50)")
                return None

            # 准备数据
            X_data = []
            y_data = []
            match_ids = []

            for row in rows:
                r = dict(row)
                features = []
                for col in feature_cols:
                    val = r.get(col)
                    if val is None or (isinstance(val, float) and val != val):  # None or NaN
                        val = defaults.get(col, 0.0)
                    features.append(float(val))
                X_data.append(features)
                y_data.append(r['home_score'] - r['away_score'])
                match_ids.append(r['match_id'])

            if len(X_data) < 50:
                return None

            logger.info(f"[训练] 准备数据: {len(X_data)} 样本, {len(feature_cols)} 特征")

            # [修复] 使用 EnsembleTrainer 标准化训练流程，替代内联训练代码
            import pandas as pd
            from ensemble_trainer import EnsembleTrainer

            # 构造 DataFrame（兼容 EnsembleTrainer 接口）
            train_df = pd.DataFrame(X_data, columns=feature_cols)
            raw_rows = [dict(row) for row in rows]
            train_df['home_score'] = [r['home_score'] for r in raw_rows]
            train_df['away_score'] = [r['away_score'] for r in raw_rows]
            train_df['match_date'] = [r['match_date'] for r in raw_rows]

            trainer = EnsembleTrainer(config_path=config_path)
            result = trainer.train(df=train_df)

            # 提取评估指标
            eval_metrics = result.get('evaluation', {})
            acc = eval_metrics.get('accuracy', 0)
            auc = eval_metrics.get('auc', 0)
            model_path = result['pipeline_path']

            logger.info(f"[训练] EnsembleTrainer: 准确率={acc:.4f}, AUC={auc:.4f}, "
                        f"样本={result['n_samples']}")

            # 保存训练记录（分类指标）
            self.db.save_training_record({
                'training_date': datetime.now().strftime('%Y-%m-%d'),
                'model_name': 'ensemble_auto',
                'algorithm': 'ensemble',
                'training_samples': result['n_samples'],
                'test_samples': eval_metrics.get('n_test_samples', 0),
                'feature_count': result['n_features'],
                'accuracy': acc,
                'auc': auc,
                'model_path': model_path,
            })

            return {
                'model': 'ensemble',
                'accuracy': acc,
                'auc': auc,
                'samples': result['n_samples'],
                'model_path': model_path,
            }

        except (Exception, KeyError, IndexError) as e:
            logger.error(f"自动训练失败: {e}", exc_info=True)
            return None

    # ==================== 主管道 ====================

    def run_full_pipeline(self) -> dict[str, Any]:
        """运行完整的自动管道"""
        logger.info("=" * 60)
        logger.info(f"🚀 哨响AI 自动管道启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        # Step 1: 数据采集
        logger.info("\n📡 [步骤1/5] 数据采集...")
        matches = self.fetch_live_and_upcoming()
        logger.info(f"  共计获取 {len(matches)} 场比赛")

        # Step 2: 特征计算 + 预测生成
        logger.info("\n🧮 [步骤2/5] 特征计算 & [步骤3/5] 预测生成...")
        for match in matches[:500]:  # 限制单次处理数量
            mid = match.get('match_id')
            if not mid:
                continue

            # 特征计算
            features = self.compute_features(mid, match)
            if features:
                # 预测生成
                self.predict_match(mid, features, match)

        logger.info(f"  共计生成 {self.stats['predictions_made']} 条预测")

        # Step 4: 回测验证
        logger.info("\n📊 [步骤4/5] 回测验证...")
        backtest_result = self.backtest_finished_matches()
        if int(backtest_result.get('total', 0)) > 0:
            logger.info(f"  回测 {backtest_result['total']} 场, 正确 {backtest_result['correct']}, "
                       f"准确率 {backtest_result['accuracy']}%")
        else:
            logger.info("  无待回测的比赛")

        # Step 5: 自动迭代训练
        logger.info("\n🔄 [步骤5/5] 自动迭代训练...")
        training_result = self.auto_retrain_if_needed(backtest_result)
        if training_result:
            logger.info(f"  训练完成: {training_result['model']} 准确率={training_result.get('accuracy', 'N/A'):.2f}%")

        # Step 6: 自动评估管道 (E1-E7)
        if int(backtest_result.get('total', 0)) > 0:
            logger.info("\n🧪 [步骤6/6] 自动评估管道 (E1-E7)...")
            try:
                from agents.evaluator.evaluation_pipeline import EvaluationPipeline
                eval_pipeline = EvaluationPipeline(db_path=self.db.db_path)
                eval_result = eval_pipeline.run(trigger_type="auto", skip_if_unchanged=False)
                self.stats['evaluation_score'] = eval_result.get('overall_score')
                self.stats['evaluation_rating'] = eval_result.get('overall_rating')
                self.stats['evaluation_urgency'] = eval_result.get('urgency')
                logger.info(f"  评估完成: 综合评分 {eval_result['overall_score']}/100 {eval_result['overall_rating']}")
                if eval_result.get('action_items'):
                    for item in eval_result['action_items'][:3]:
                        logger.warning(f"    ⚠️ {item}")
            except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
                logger.warning(f"  评估管道执行异常(非致命): {e}")

        # 汇总报告
        self.stats['end_time'] = datetime.now().isoformat()
        self.stats['backtest_accuracy'] = backtest_result.get('accuracy')

        logger.info("\n" + "=" * 60)
        logger.info("📋 管道执行汇总")
        logger.info(f"  比赛拉取: {self.stats['matches_fetched']}")
        logger.info(f"  预测生成: {self.stats['predictions_made']}")
        logger.info(f"  回测验证: {self.stats['backtests_done']} (正确 {self.stats['correct']}, "
                   f"准确率 {self.stats.get('backtest_accuracy', 'N/A')}%)")
        if self.stats.get('evaluation_score') is not None:
            logger.info(f"  评估评分: {self.stats.get('evaluation_score')}/100 {self.stats.get('evaluation_rating', '')}")
        logger.info(f"  错误数: {len(self.stats.get('errors', []))}")
        logger.info("=" * 60)

        return self.stats

    def run_daemon(self, interval_minutes: int = 30):
        """守护模式：定期自动运行管道"""
        logger.info(f"🔄 守护模式启动，间隔 {interval_minutes} 分钟")
        while True:
            try:
                self.run_full_pipeline()
            except (Exception) as e:
                logger.error(f"管道执行异常: {e}", exc_info=True)

            next_run = datetime.now() + timedelta(minutes=interval_minutes)
            logger.info(f"\n⏰ 下次运行: {next_run.strftime('%H:%M:%S')}")
            time.sleep(interval_minutes * 60)

    def generate_report(self) -> dict[str, Any]:
        """生成全面的准确率分析报告"""
        # 数据库统计
        stats = self.db.get_stats()

        # 按联赛分组准确率
        with self.db.get_connection() as conn:
            league_accuracy = conn.execute('''
                SELECT m.league_name,
                       COUNT(*) as total,
                       SUM(p.is_correct) as correct,
                       ROUND(SUM(p.is_correct) * 100.0 / COUNT(*), 1) as accuracy
                FROM predictions p
                JOIN matches m ON p.match_id = m.match_id
                WHERE p.is_correct IS NOT NULL
                GROUP BY m.league_name
                ORDER BY accuracy DESC
            ''').fetchall()

            # 按决策分组
            decision_accuracy = conn.execute('''
                SELECT p.decision,
                       COUNT(*) as total,
                       SUM(p.is_correct) as correct,
                       ROUND(SUM(p.is_correct) * 100.0 / COUNT(*), 1) as accuracy
                FROM predictions p
                WHERE p.is_correct IS NOT NULL
                GROUP BY p.decision
            ''').fetchall()

            # 近期趋势（按周）
            weekly_trend = conn.execute('''
                SELECT substr(p.prediction_time, 1, 10) as week_start,
                       COUNT(*) as total,
                       SUM(p.is_correct) as correct,
                       ROUND(SUM(p.is_correct) * 100.0 / COUNT(*), 1) as accuracy
                FROM predictions p
                WHERE p.is_correct IS NOT NULL
                GROUP BY week_start
                ORDER BY week_start DESC
                LIMIT 14
            ''').fetchall()

        report = {
            'generated_at': datetime.now().isoformat(),
            'overall': stats,
            'by_league': [dict(r) for r in league_accuracy],
            'by_decision': [dict(r) for r in decision_accuracy],
            'weekly_trend': [dict(r) for r in weekly_trend],
            'recommendations': self._generate_recommendations(stats),
        }

        logger.info(f"\n📊 准确率报告: 总评估 {stats.get('evaluated_predictions', 0)} 场, "
                   f"准确率 {stats.get('accuracy', 'N/A')}%")
        for l in league_accuracy:
            logger.info(f"  {l['league_name']}: {l['accuracy']}% ({l['total']}场)")

        return report

    def _generate_recommendations(self, stats: dict[str, Any]) -> list[str]:
        """基于当前数据生成改进建议"""
        recs = []

        accuracy = stats.get('accuracy', 0)
        evaluated = stats.get('evaluated_predictions', 0)

        if accuracy < 45 and evaluated > 20:
            recs.append("🔴 准确率偏低（<45%），建议升级模型算法：安装 XGBoost 并重训练")
        elif accuracy < 55 and evaluated > 20:
            recs.append("🟡 准确率中等（45-55%），建议增加趋势特征（近期5场形态、主客场胜率）")
        elif accuracy >= 55:
            recs.append("🟢 准确率良好（≥55%），建议增加联赛覆盖量，扩大样本")

        if evaluated < 100:
            recs.append("📊 评估样本不足（<100场），建议增加回测数据，确保统计显著性")

        recs.append("💡 通用建议：接入 xG 数据和球员级统计数据可提升 5-10 个百分点")
        recs.append("💡 通用建议：使用集成方法（Ridge + XGBoost + LightGBM 加权投票）")

        return recs


# ==================== CLI 入口 ====================

def main():
    parser = argparse.ArgumentParser(description='哨响AI 自动预测-回测管道')
    parser.add_argument('--daemon', action='store_true', help='守护模式，定期自动运行')
    parser.add_argument('--interval', type=int, default=30, help='守护模式运行间隔（分钟）')
    parser.add_argument('--backtest', action='store_true', help='仅回测已结束的比赛')
    parser.add_argument('--report', action='store_true', help='生成准确率分析报告')
    parser.add_argument('--fetch-only', action='store_true', help='仅拉取数据不入库')

    args = parser.parse_args()

    pipeline = AutoPipeline()

    if args.daemon:
        pipeline.run_daemon(interval_minutes=args.interval)
    elif args.backtest:
        result = pipeline.backtest_finished_matches()
        _safe_print_json(result)
    elif args.report:
        report = pipeline.generate_report()
        _safe_print_json(report)
    elif args.fetch_only:
        matches = pipeline.fetch_live_and_upcoming()
        print(f"拉取到 {len(matches)} 场比赛")
    else:
        # 默认：运行完整管道
        result = pipeline.run_full_pipeline()
        _safe_print_json(result)


def _safe_print_json(data: object) -> None:
    """安全打印JSON，处理Windows GBK编码问题"""
    import sys
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(
            sys.stdout.encoding or 'utf-8', errors='replace'))


if __name__ == '__main__':
    main()
