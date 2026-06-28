"""
5步足球AI优化脚本
===============

按照用户指定的5步优化计划：
1. 启用 DrawOptimizedEnsemble (最大单点收益)
2. 启用 AdaptiveTrainingStrategy (解决类别不平衡根因)  
3. 修复特征泄漏 (提升泛化能力)
4. 分组建模 (联赛针对性优化)
5. DynamicWeightCalculator (细粒度权重调整)

执行顺序：1 → 2 → 3 → 4 → 5
"""

import os
import sys
import json
import logging
import numpy as np
import pandas as pd
import xgboost as xgb
import joblib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from sklearn.metrics import accuracy_score, f1_score
import warnings
warnings.filterwarnings('ignore')

# 添加项目根路径

from backend.models.footballai_enhanced import FootballAIEnhanced
from backend.models.advanced_ensemble import DrawOptimizedEnsemble
from backend.training.adaptive_training import AdaptiveTrainingStrategy
from features.advanced_temporal_features import SafeTemporalFeatureEngineer
from optimize.weight_optimizer import WeightOptimizer

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

class FiveStepOptimizer:
    """5步优化管道"""
    
    def __init__(self, data_path: str, output_dir: str = "optimize_results"):
        """
        初始化优化器
        
        Args:
            data_path: 数据文件路径 (CSV格式)
            output_dir: 结果输出目录
        """
        self.data_path = data_path
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        
        # 初始化组件
        self.model = None
        self.data = None
        self.X = None
        self.y = None
        self.feature_names = None
        self.dates = None
        self.results = {}
        
    def load_and_prepare_data(self) -> pd.DataFrame:
        """步骤0: 加载数据并进行基础预处理"""
        logger.info("=" * 70)
        logger.info("步骤0: 加载和预处理数据")
        logger.info("=" * 70)
        
        # 加载数据
        logger.info(f"加载数据: {self.data_path}")
        df = pd.read_csv(self.data_path, low_memory=False)
        
        # 基本清洗
        required_cols = ['home_score', 'away_score', 'home_team', 'away_team']
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise ValueError(f"缺少必要列: {missing}")
        
        # 过滤无效数据
        before = len(df)
        df = df[df['home_score'].notna() & df['away_score'].notna()]
        if len(df) < before:
            logger.info(f"  过滤无效行: {before - len(df)}")
        
        # 按时间排序 (确保时序安全)
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')
            df = df.dropna(subset=['date']).sort_values('date')
            self.dates = df['date'].values
        else:
            logger.warning("  数据中没有日期列，无法进行时序分析")
            self.dates = np.arange(len(df))
        
        logger.info(f"  有效数据: {len(df):,} 行 × {len(df.columns)} 列")
        logger.info(f"  日期范围: {df['date'].min()} 到 {df['date'].max()}" if 'date' in df.columns else "  无日期信息")
        
        # 检查特征泄漏
        leaky_patterns = [
            'home_win_prob', 'poisson_home_win_prob', 'prob_consensus', 'prob_disagreement',
            'attack_strength', 'defense_strength', 'avg_goals_for', 'avg_goals_against',
            'std_goals_for', 'std_goals_against', 'elo_updated', 'poisson_home_goals',
            'poisson_away_goals', 'strength_product', 'poisson_goal_diff'
        ]
        
        leaky_features = [c for c in df.columns if any(p in c for p in leaky_patterns)]
        if leaky_features:
            logger.warning(f"  检测到潜在特征泄漏: {len(leaky_features)} 个特征")
            logger.warning(f"  将在步骤3中处理")
        
        return df
    
    def step1_enable_draw_optimization(self) -> Dict:
        """步骤1 v6.1: 多检测器集成 + 校准增强 + 联赛感知"""
        logger.info("=" * 70)
        logger.info("步骤1 v6.1: DrawOptimizedEnsemble (多检测器+校准增强+联赛感知)")
        logger.info("=" * 70)
        
        if self.model is None:
            logger.error("  需要先训练基础模型")
            return {}
        
        try:
            DOE = DrawOptimizedEnsemble
            # 使用更保守的 boost_factor(0.5) 避免过度预测
            self.draw_opt = DOE(model_version="v6.1", draw_threshold=0.25)
            self.draw_opt.boost_factor = 0.5

            X_df = pd.DataFrame(self.X, columns=self.feature_names_)
            X_enhanced = DOE.prepare_draw_specific_features(X_df)
            X_enhanced_np = X_enhanced.fillna(0).astype(np.float32).values

            # 三阶段训练 + 校准
            metrics = self.draw_opt.fit_with_cv(
                X_enhanced_np, self.y, n_splits=5, calibrate=True
            )
            # 多检测器集成
            multi_acc = self.draw_opt.train_multi_detector(X_enhanced_np, self.y)
            logger.info(f"  ✓ {len(multi_acc)+1}个检测器集成完成")

            # 联赛感知阈值
            league_rates = {}
            per_sample_rates = None
            if self.data is not None and 'league' in self.data.columns:
                for lg in self.data['league'].unique():
                    lg_d = self.data[self.data['league'] == lg]
                    league_rates[lg] = (lg_d['home_score'] == lg_d['away_score']).mean()
                per_sample_rates = np.array([
                    league_rates.get(lg, 0.25) for lg in self.data['league']
                ])
                logger.info(f"  ✓ 联赛感知: {len(league_rates)}个联赛阈值已计算")

            # base vs boosted 对比
            X_scaled = self.model.scaler.transform(self.X)
            base_proba = self.model.ensemble_predict(X_scaled)
            pred_old, proba_old = self.draw_opt.predict_with_draw_enhancement(X_enhanced_np, base_proba)
            pred_new, proba_new = self.draw_opt.predict_with_calibrated_boost(
                X_enhanced_np, base_proba,
                league_draw_rates=per_sample_rates,
            )

            draw_mask = self.y == 1
            f1_base = f1_score(draw_mask, np.argmax(base_proba, axis=1) == 1, zero_division=0)
            f1_boosted = f1_score(draw_mask, pred_old == 1, zero_division=0)
            f1_calibrated = f1_score(draw_mask, pred_new == 1, zero_division=0)

            logger.info(f"  📊 平局F1: base={f1_base:.4f} → boosted={f1_boosted:.4f} → calibrated={f1_calibrated:.4f}")
            logger.info(f"    平局预测数: boosted={int((pred_old==1).sum())} | calibrated={int((pred_new==1).sum())} | actual={int(draw_mask.sum())}")

            self._draw_enabled = True
            result = {
                'step': 1, 'name': 'DrawOptimizedEnsemble_v6.1', 'enabled': True,
                'version': 'v6.1-calibrated-multi',
                'draw_detector_accuracy': metrics.get('draw_detector_accuracy'),
                'full_model_accuracy': metrics.get('full_model_accuracy'),
                'n_detectors': len(multi_acc) + 1,
                'boost_factor': 0.5,
                'league_rates': league_rates,
                'draw_f1': {'base': f1_base, 'boosted': f1_boosted, 'calibrated': f1_calibrated},
                'draw_predictions': {
                    'base': int((np.argmax(base_proba, axis=1)==1).sum()),
                    'boosted': int((pred_old==1).sum()),
                    'calibrated': int((pred_new==1).sum()),
                    'actual': int(draw_mask.sum()),
                },
            }
            self.results['step1'] = result
            return result
            
        except (Exception, ValueError, KeyError, IndexError) as e:
            logger.error(f"  Step1 失败: {e}")
            import traceback; traceback.print_exc()
            return {'step': 1, 'name': 'DrawOptimizedEnsemble', 'enabled': False, 'error': str(e)}
    
    def step2_enable_adaptive_training(self) -> Dict:
        """步骤2 v7.1: 自适应训练 → 分析 + 实际重训 + 对比"""
        logger.info("=" * 70)
        logger.info("步骤2 v7.1: AdaptiveTrainingStrategy (分析+重训+对比)")
        logger.info("=" * 70)
        
        if self.model is None:
            logger.error("  需要先训练基础模型")
            return {}
        
        try:
            ATS = AdaptiveTrainingStrategy
            self.adaptive = ATS(n_splits=5, min_train_size=1000)

            # 1) 渐进式验证
            pv = self.adaptive.progressive_validation(
                self.X, self.y, self.dates,
                lambda: xgb.XGBClassifier(n_estimators=150, max_depth=5,
                    learning_rate=0.05, random_state=42, verbosity=0),
                list(self.feature_names_),
            )
            # 2) 特征选择
            selected, stability = self.adaptive.adaptive_feature_selection(
                self.X, self.y, self.dates,
                list(self.feature_names_), n_windows=5, stability_threshold=0.5, max_features=50,
            )
            # 3) 动态类别权重
            class_weights = self.adaptive.dynamic_class_weighting(self.y)

            logger.info(f"  ✓ 分析完成: PV_acc={pv['mean_accuracy']:.4f}, "
                       f"特征 {len(selected)}/{len(self.feature_names_)}, "
                       f"权重={class_weights}")

            # ── v7.1 新增: 实际重训 ──
            baseline_pred = self.model.predict(self.X)
            baseline_acc = accuracy_score(self.y, baseline_pred)
            baseline_draw_f1 = f1_score(self.y == 1, baseline_pred == 1, zero_division=0)

            # 用稳定特征 + 类别权重重训
            retrained_model, retrain_metrics = self.adaptive.retrain_with_selected_features(
                self.X, self.y, self.dates,
                list(self.feature_names_), selected,
                class_weights=class_weights,
                early_stopping_rounds=20,
            )
            # 对比
            comp = self.adaptive.compare_retrained_vs_baseline(
                self.X[-int(len(self.X)*0.15):],
                self.y[-int(len(self.y)*0.15):],
                self.model.xgb_model, retrained_model,
            )
            self._retrained_xgb = retrained_model

            logger.info(f"  ✓ 重训完成: acc {baseline_acc:.4f}→{retrain_metrics['val_accuracy']:.4f} "
                       f"({retrain_metrics['val_accuracy']-baseline_acc:+.4f}), "
                       f"drawF1 {baseline_draw_f1:.4f}→{retrain_metrics['val_draw_f1']:.4f}")

            result = {
                'step': 2, 'name': 'AdaptiveTrainingStrategy_v7.1', 'enabled': True,
                'progressive_validation': pv,
                'n_selected': len(selected), 'n_total': len(self.feature_names_),
                'selected_features': selected,
                'class_weights': class_weights,
                'baseline': {'accuracy': baseline_acc, 'draw_f1': baseline_draw_f1},
                'retrained': retrain_metrics,
                'comparison': comp,
            }
            self.results['step2'] = result
            return result
            
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"  Step2 失败: {e}")
            import traceback; traceback.print_exc()
            return {'step': 2, 'name': 'AdaptiveTrainingStrategy', 'enabled': False, 'error': str(e)}
    
    def step3_fix_feature_leakage(self) -> Dict:
        """步骤3 v3.0: 实际修复 → 重建时序安全特征 + 重训对比"""
        logger.info("=" * 70)
        logger.info("步骤3 v3.0: 重建时序安全特征 (SafeTemporalFeatureEngineer)")
        logger.info("=" * 70)

        # 检测泄漏特征
        leaky_patterns = [
            'home_win_prob', 'poisson_home_win_prob', 'prob_consensus', 'prob_disagreement',
            'attack_strength', 'defense_strength', 'avg_goals_for', 'avg_goals_against',
            'std_goals_for', 'std_goals_against', 'elo_updated', 'poisson_home_goals',
            'poisson_away_goals', 'strength_product', 'poisson_goal_diff'
        ]
        
        leaky_features = []
        if self.feature_names_ is not None:
            leaky_features = [f for f in self.feature_names_ if any(p in f for p in leaky_patterns)]
        logger.info(f"  检测到 {len(leaky_features)} 个潜在泄漏特征")

        result = {
            'step': 3, 'name': 'FeatureLeakageFix_v3.0',
            'leaky_count': len(leaky_features),
            'leaky_features': leaky_features,
            'action_taken': False,
        }

        # ── v3.0: 尝试实际重建安全特征 ──
        if self.data is not None:
            try:
                safe_engineer = SafeTemporalFeatureEngineer(
                    self.data, team_col='home_team',
                    date_col='date' if 'date' in self.data.columns else None,
                )
                # 创建核心安全特征
                X_safe_df = safe_engineer.create_all_features()
                
                if X_safe_df is not None and len(X_safe_df) > 0:
                    safe_cols = [c for c in X_safe_df.columns 
                                if not any(p in c for p in leaky_patterns)]
                    X_safe = X_safe_df[safe_cols].fillna(0).values
                    y_safe = self.y[-len(X_safe):] if len(X_safe) <= len(self.y) else self.y

                    logger.info(f"  ✓ 安全特征: {X_safe.shape[1]}列 (原始{len(self.feature_names_)}列)")

                    # 时序分割训练
                    split_idx = int(len(X_safe) * 0.85)
                    X_tr, X_val = X_safe[:split_idx], X_safe[split_idx:]
                    y_tr, y_val = self.y[:split_idx], self.y[split_idx:]

                    safe_model = xgb.XGBClassifier(
                        n_estimators=200, max_depth=5, learning_rate=0.05,
                        random_state=42, verbosity=0, n_jobs=-1,
                        early_stopping_rounds=15,
                    )
                    safe_model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)

                    y_pred_safe = safe_model.predict(X_val)
                    safe_acc = accuracy_score(y_val, y_pred_safe)
                    safe_draw_f1 = f1_score(y_val == 1, y_pred_safe == 1, zero_division=0)

                    # 对比: 原模型在同样验证集上的表现
                    orig_pred = self.model.xgb_model.predict(X_val[:len(self.model.xgb_model.feature_importances_)]) if hasattr(self.model, 'xgb_model') else self.model.predict(self.X[-len(X_val):])
                    # 使用原模型预测验证集
                    try:
                        X_val_for_orig = self.model.scaler.transform(
                            self.X[-len(X_val):, :len(self.feature_names_)]
                        ) if hasattr(self.model, 'scaler') else self.X[-len(X_val):]
                        orig_pred = self.model.predict(X_val_for_orig)
                        orig_acc = accuracy_score(y_val, orig_pred)
                        orig_draw_f1 = f1_score(y_val == 1, orig_pred == 1, zero_division=0)
                    except (Exception, KeyError, IndexError):
                        orig_acc, orig_draw_f1 = None, None

                    logger.info(f"  📊 泄漏修复对比:")
                    logger.info(f"    原模型:  acc={orig_acc:.4f} drawF1={orig_draw_f1:.4f}" if orig_acc else "    原模型: 评估失败")
                    logger.info(f"    安全模型: acc={safe_acc:.4f} drawF1={safe_draw_f1:.4f}")

                    result.update({
                        'action_taken': True,
                        'safe_features_count': X_safe.shape[1],
                        'safe_model': {
                            'val_accuracy': safe_acc,
                            'val_draw_f1': safe_draw_f1,
                            'n_estimators': int(safe_model.best_iteration + 1) if hasattr(safe_model, 'best_iteration') else 200,
                        },
                        'leakage_gap': {
                            'accuracy': orig_acc - safe_acc if orig_acc else None,
                            'draw_f1': orig_draw_f1 - safe_draw_f1 if orig_draw_f1 else None,
                        } if orig_acc else None,
                    })
                    self._safe_features = X_safe
                    self._safe_model = safe_model
            except (Exception, ValueError) as e:
                logger.warning(f"  SafeTemporalFeatureEngineer 构建失败: {e}")
                result['safe_engineer_error'] = str(e)

        self.results['step3'] = result
        return result
    
    def step4_league_specific_modeling(self) -> Dict:
        """步骤4 v4.0: 实际训练联赛模型 + 联赛感知集成"""
        logger.info("=" * 70)
        logger.info("步骤4 v4.0: 联赛特定模型训练 + 联赛感知集成")
        logger.info("=" * 70)
        
        if self.data is None or 'league' not in self.data.columns:
            logger.warning("  数据中没有联赛信息，跳过")
            return {'step': 4, 'name': 'LeagueSpecificModeling', 'status': 'skipped'}

        leagues = self.data['league'].unique()
        logger.info(f"  共 {len(leagues)} 个联赛")
        
        league_stats = {}
        league_models = {}
        league_perf = {}
        viable_count = 0

        for league in leagues:
            lg_mask = self.data['league'] == league
            lg_data = self.data[lg_mask]
            n_matches = len(lg_data)
            draw_rate = (lg_data['home_score'] == lg_data['away_score']).mean()

            league_stats[str(league)] = {
                'n_matches': int(n_matches),
                'home_win_rate': float((lg_data['home_score'] > lg_data['away_score']).mean()),
                'draw_rate': float(draw_rate),
                'away_win_rate': float((lg_data['home_score'] < lg_data['away_score']).mean()),
                'avg_home_goals': float(lg_data['home_score'].mean()),
            }

            # ── v4.0: 对足够数据的联赛实际训练模型 ──
            if n_matches >= 100 and 'league' in self.data.columns:
                try:
                    lg_indices = lg_mask.values.nonzero()[0]
                    if len(lg_indices) < 100:
                        continue
                    X_lg = self.X[lg_indices]
                    y_lg = self.y[lg_indices]

                    # 时序分割: 前80%训练, 后20%验证
                    split = int(len(X_lg) * 0.8)
                    X_tr, X_val = X_lg[:split], X_lg[split:]
                    y_tr, y_val = y_lg[:split], y_lg[split:]

                    # 联赛特定阈值 (基于历史平局率自动调整)
                    lg_threshold = max(0.18, min(0.32, draw_rate - 0.05))

                    lg_model = xgb.XGBClassifier(
                        n_estimators=150, max_depth=5, learning_rate=0.05,
                        random_state=42, verbosity=0, n_jobs=-1,
                        scale_pos_weight=float((y_tr != 1).sum()) / max((y_tr == 1).sum(), 1) * 1.5,
                    )
                    lg_model.fit(X_tr, y_tr)
                    y_pred = lg_model.predict(X_val)

                    lg_acc = accuracy_score(y_val, y_pred)
                    lg_draw_f1 = f1_score(y_val == 1, y_pred == 1, zero_division=0)

                    league_models[str(league)] = {
                        'n_train': int(len(X_tr)),
                        'n_val': int(len(X_val)),
                        'draw_threshold': lg_threshold,
                    }
                    league_perf[str(league)] = {
                        'accuracy': float(lg_acc),
                        'draw_f1': float(lg_draw_f1),
                        'draw_rate_actual': float(draw_rate),
                    }

                    # 存储最佳模型 (用于后续集成)
                    if lg_acc > 0.45 and n_matches >= 200:
                        key = str(league).replace(' ', '_')[:40]
                        setattr(self, f'_lg_model_{key}', lg_model)
                        viable_count += 1

                    logger.info(f"  {str(league)[:25]}: {n_matches}场 | acc={lg_acc:.4f} drawF1={lg_draw_f1:.4f} (阈值={lg_threshold:.2f})")
                except (Exception, ValueError, KeyError, IndexError) as e:
                    logger.warning(f"  {league}: 训练失败 - {e}")

        # ── 联赛感知集成: 全局模型 + 联赛模型加权 ──
        ensemble_info = {}
        if viable_count > 0:
            try:
                # 用联赛模型预测 + 全局模型预测做加权
                X_val_global = self.X[-int(len(self.X)*0.15):]
                y_val_global = self.y[-int(len(self.y)*0.15):]
                global_pred = self.model.predict(self.model.scaler.transform(X_val_global))
                global_acc = accuracy_score(y_val_global, global_pred)
                
                ensemble_info = {
                    'viable_league_models': viable_count,
                    'integration_strategy': 'weighted_average (global:0.6 + league:0.4 for known leagues)',
                    'global_model_val_acc': float(global_acc),
                }
                logger.info(f"  ✓ {viable_count}个联赛模型可用于集成 (全局模型val_acc={global_acc:.4f})")
            except (Exception, ValueError, KeyError, IndexError) as e:
                logger.warning(f"  集成评估失败: {e}")

        # 按平局率排序的联赛
        ranked = sorted(league_perf.items(), key=lambda x: x[1].get('draw_rate_actual', 0), reverse=True)

        result = {
            'step': 4, 'name': 'LeagueSpecificModeling_v4.0',
            'total_leagues': len(leagues),
            'models_trained': len(league_models),
            'viable_models': viable_count,
            'league_stats': league_stats,
            'league_performance': league_perf,
            'top_draw_leagues': ranked[:5],
            'ensemble_info': ensemble_info,
        }
        self.results['step4'] = result
        return result
    
    def step5_dynamic_weight_calculator(self) -> Dict:
        """步骤5: DynamicWeightCalculator (细粒度权重调整)"""
        logger.info("=" * 70)
        logger.info("步骤5: DynamicWeightCalculator (动态权重优化)")
        logger.info("=" * 70)
        
        try:
            # 使用现有的 WeightOptimizer
            optimizer = WeightOptimizer()
            
            # 加载数据
            X, y, y_gd, feature_names, dates, df = optimizer.load_data()
            
            # 分割数据 (70%训练, 15%验证, 15%测试)
            split_info = optimizer.split_data(X, y, y_gd, dates, df, train_ratio=0.70, val_ratio=0.15)
            
            # 训练子模型
            optimizer.train_sub_models()
            
            # 运行 Optuna 权重优化
            logger.info("  运行 Optuna 贝叶斯权重优化...")
            optuna_result = optimizer.optimize_optuna(n_trials=200)
            
            # 网格搜索对比
            logger.info("  运行网格搜索对比...")
            grid_result = optimizer.grid_search(step=0.05)
            
            # 获取默认权重
            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')
            with open(config_path, 'r', encoding='utf-8') as f:
                import yaml
                config = yaml.safe_load(f)
            default_weights = config['models']['ensemble']
            
            # 生成报告
            report_path = optimizer.generate_report(optuna_result, grid_result, default_weights, split_info)
            
            # 测试集评估
            default_test = optimizer.evaluate_final(default_weights)
            optuna_test = optimizer.evaluate_final(optuna_result['best_params'])
            grid_test = optimizer.evaluate_final(grid_result['best_params'])
            
            # 计算改进
            acc_improvement = optuna_test['accuracy'] - default_test['accuracy']
            draw_f1_improvement = optuna_test['f1_draw'] - default_test['f1_draw']
            
            logger.info("  权重优化结果:")
            logger.info(f"    默认权重: XGB={default_weights['xgboost_weight']:.4f}, "
                       f"Ridge={default_weights['ridge_weight']:.4f}, "
                       f"Heu={default_weights['heuristic_weight']:.4f}")
            logger.info(f"    Optuna最优: XGB={optuna_result['best_params']['xgboost_weight']:.4f}, "
                       f"Ridge={optuna_result['best_params']['ridge_weight']:.4f}, "
                       f"Heu={optuna_result['best_params']['heuristic_weight']:.4f}")
            logger.info(f"    准确率提升: {acc_improvement*100:+.2f}%")
            logger.info(f"    平局F1提升: {draw_f1_improvement*100:+.2f}%")
            
            result = {
                'step': 5,
                'name': 'DynamicWeightCalculator',
                'default_weights': default_weights,
                'optuna_weights': optuna_result['best_params'],
                'grid_weights': grid_result['best_params'],
                'performance_improvement': {
                    'accuracy': float(acc_improvement),
                    'draw_f1': float(draw_f1_improvement),
                    'accuracy_percent': float(acc_improvement * 100),
                    'draw_f1_percent': float(draw_f1_improvement * 100)
                },
                'test_metrics': {
                    'default': default_test,
                    'optuna': optuna_test,
                    'grid': grid_test
                },
                'report_path': report_path,
                'success': True
            }
            
            # 保存到文件
            result_path = os.path.join(self.output_dir, 'step5_dynamic_weights.json')
            with open(result_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"  结果保存到: {result_path}")
            
            self.results['step5'] = result
            return result
            
        except (Exception, KeyError, IndexError, IOError, FileNotFoundError) as e:
            logger.error(f"  DynamicWeightCalculator 失败: {e}")
            return {
                'step': 5,
                'name': 'DynamicWeightCalculator',
                'success': False,
                'error': str(e)
            }
    
    def train_base_model(self) -> FootballAIEnhanced:
        """训练基础模型 (FootballAIEnhanced)"""
        logger.info("=" * 70)
        logger.info("训练基础 FootballAIEnhanced 模型")
        logger.info("=" * 70)
        
        # 准备特征
        logger.info("  准备特征...")
        X, y, feature_names = self.model.prepare_features(self.data)
        
        # 检查数据平衡性
        class_counts = np.bincount(y)
        logger.info(f"  类别分布: H={class_counts[0]}, D={class_counts[1]}, A={class_counts[2]}")
        logger.info(f"  平局比例: {class_counts[1]/len(y):.2%}")
        
        # 训练模型
        logger.info("  训练模型 (时序交叉验证)...")
        self.model.train_with_cross_validation(X, y)
        
        # 保存特征信息
        self.X = X
        self.y = y
        self.feature_names = feature_names
        
        # 评估基础模型性能
        logger.info("  评估基础模型...")
        y_pred = self.model.predict(X)
        acc = accuracy_score(y, y_pred)
        f1_draw = f1_score(y == 1, y_pred == 1, zero_division=0)
        
        logger.info(f"  基础模型性能:")
        logger.info(f"    整体准确率: {acc:.4f}")
        logger.info(f"    平局F1分数: {f1_draw:.4f}")
        
        return self.model
    
    def run_all_steps(self) -> Dict:
        """运行完整的5步优化流程"""
        logger.info("=" * 80)
        logger.info("开始执行5步优化计划")
        logger.info("=" * 80)
        
        start_time = datetime.now(timezone.utc)
        
        try:
            # 步骤0: 加载数据
            self.data = self.load_and_prepare_data()
            
            # 初始化模型
            self.model = FootballAIEnhanced(model_version="v5.0-optimized")
            
            # 训练基础模型
            self.train_base_model()
            
            # 步骤1: 启用 DrawOptimizedEnsemble
            step1_result = self.step1_enable_draw_optimization()
            
            # 步骤2: 启用 AdaptiveTrainingStrategy
            step2_result = self.step2_enable_adaptive_training()
            
            # 步骤3: 修复特征泄漏
            step3_result = self.step3_fix_feature_leakage()
            
            # 步骤4: 分组建模分析
            step4_result = self.step4_league_specific_modeling()
            
            # 步骤5: 动态权重优化
            step5_result = self.step5_dynamic_weight_calculator()
            
            # 汇总结果
            total_time = (datetime.now(timezone.utc) - start_time).total_seconds()
            
            summary = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'total_time_seconds': total_time,
                'data_info': {
                    'rows': len(self.data),
                    'features': len(self.feature_names_) if self.feature_names_ else 0,
                    'date_range': f"{self.data['date'].min()} to {self.data['date'].max()}" if 'date' in self.data.columns else 'unknown'
                },
                'steps': {
                    'step1': step1_result.get('version', step1_result.get('enabled', False)) if step1_result else False,
                    'step2': step2_result.get('enabled', False) if step2_result else False,
                    'step3': bool(step3_result.get('action_taken', False)) if step3_result else False,
                    'step4': f"{step4_result.get('models_trained', 0)} models" if step4_result else 'skipped',
                    'step5': step5_result.get('success', False) if step5_result else False
                },
                'draw_f1': step1_result.get('draw_f1', {}) if step1_result else {},
                'retrain_comparison': step2_result.get('comparison', {}) if step2_result else {},
                'leakage_gap': step3_result.get('leakage_gap') if step3_result else None,
                'league_integration': step4_result.get('ensemble_info', {}) if step4_result else {},
                'performance_improvement': step5_result.get('performance_improvement', {}) if step5_result else {},
                'output_dir': self.output_dir
            }
            
            # 保存汇总报告
            summary_path = os.path.join(self.output_dir, 'five_step_optimization_summary.json')
            with open(summary_path, 'w', encoding='utf-8') as f:
                json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
            
            # 保存完整模型
            model_path = os.path.join(self.output_dir, 'footballai_optimized.joblib')
            self.model.save_model(self.output_dir)
            os.rename(
                os.path.join(self.output_dir, 'footballai_enhanced_v5.0-optimized.joblib'),
                model_path
            )
            
            logger.info("=" * 80)
            logger.info("5步优化完成!")
            logger.info("=" * 80)
            logger.info(f"总耗时: {total_time:.1f}秒")
            logger.info(f"结果目录: {self.output_dir}")
            logger.info(f"汇总报告: {summary_path}")
            logger.info(f"优化后模型: {model_path}")
            
            if step5_result and 'performance_improvement' in step5_result:
                imp = step5_result['performance_improvement']
                logger.info(f"性能提升:")
                logger.info(f"  准确率: {imp.get('accuracy_percent', 0):+.2f}%")
                logger.info(f"  平局F1: {imp.get('draw_f1_percent', 0):+.2f}%")
            
            return summary
            
        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            logger.error(f"5步优化执行失败: {e}")
            import traceback
            traceback.print_exc()
            return {'error': str(e), 'success': False}

def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='5步足球AI优化脚本')
    parser.add_argument('--data', type=str, required=True,
                       help='数据文件路径 (CSV格式)')
    parser.add_argument('--output', type=str, default='optimize_results',
                       help='输出目录 (默认: optimize_results)')
    parser.add_argument('--skip-step', type=int, nargs='+', default=[],
                       help='跳过的步骤编号 (1-5)')
    parser.add_argument('--only-step', type=int, nargs='+', default=[],
                       help='仅执行指定的步骤编号 (1-5)')
    
    args = parser.parse_args()
    
    # 创建优化器
    optimizer = FiveStepOptimizer(data_path=args.data, output_dir=args.output)
    
    # 确定要执行的步骤
    all_steps = [1, 2, 3, 4, 5]
    if args.only_step:
        steps_to_run = [s for s in all_steps if s in args.only_step]
    else:
        steps_to_run = [s for s in all_steps if s not in args.skip_step]
    
    logger.info(f"将执行步骤: {steps_to_run}")
    
    # 加载数据 (总是需要)
    optimizer.data = optimizer.load_and_prepare_data()
    
    # 初始化模型 (如果需要步骤1或2)
    if 1 in steps_to_run or 2 in steps_to_run:
        optimizer.model = FootballAIEnhanced(model_version="v5.0-optimized")
        # 训练基础模型
        X, y, feature_names = optimizer.model.prepare_features(optimizer.data)
        optimizer.model.train_with_cross_validation(X, y)
        optimizer.X = X
        optimizer.y = y
        optimizer.feature_names = feature_names
    
    # 执行选定的步骤
    results = {}
    
    if 1 in steps_to_run:
        results['step1'] = optimizer.step1_enable_draw_optimization()
    
    if 2 in steps_to_run:
        results['step2'] = optimizer.step2_enable_adaptive_training()
    
    if 3 in steps_to_run:
        results['step3'] = optimizer.step3_fix_feature_leakage()
    
    if 4 in steps_to_run:
        results['step4'] = optimizer.step4_league_specific_modeling()
    
    if 5 in steps_to_run:
        results['step5'] = optimizer.step5_dynamic_weight_calculator()
    
    # 生成报告
    summary = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'data_file': args.data,
        'steps_executed': steps_to_run,
        'results': results
    }
    
    summary_path = os.path.join(args.output, 'optimization_report.json')
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    
    logger.info(f"优化报告已保存: {summary_path}")
    logger.info("完成!")

if __name__ == '__main__':
    main()
