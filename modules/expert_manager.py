#!/usr/bin/env python3
"""
哨响AI - 分阶段专家管理器
=========================
插拔式专家集成、独立评估、阶段推进。

设计原则:
    1. 先建立基准 → 验证显著优于基准 (>40%准确率)
    2. 分阶段启用: 先2-3个已验证专家 → 跑通 → 逐个追加
    3. 每个新专家必须通过独立评估才加入集成
    4. 加入后自动A/B测试 → 仅保留有正向贡献的专家

阶段定义:
    Phase 0: 基准模型 (AlwaysHome / LogisticRegression) → 建立基线
    Phase 1: 核心3专家 (Trend + H2H + Ensemble) → 目标 >40%
    Phase 2: +3专家 (Alpha + Quant + Referee) → 目标 +3~5pp
    Phase 3: +3专家 (KeeperGoal + AttackEff + TimeSpace) → 目标 +2~3pp
    Phase 4: 剩余专家 (Arbitrage + Upset + Media) → 最终调优

用法:
    from modules.expert_manager import ExpertManager
    mgr = ExpertManager(db_path='data/football_data.db')
    
    # 注册专家
    mgr.register('trend_analyzer', TrendAnalyzer(), phase=1)
    mgr.register('alpha_decision', AlphaDetector(), phase=2)
    
    # 运行当前阶段
    mgr.run_phase(1)
    
    # 评估后推进
    if mgr.current_accuracy > 0.40:
        mgr.advance_phase()

    # 查看状态
    print(mgr.status_report())
"""
import json
import logging
import sqlite3
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, classification_report

logger = logging.getLogger(__name__)


# ============================================================
# 数据类
# ============================================================
class ExpertStatus(Enum):
    PENDING = "pending"        # 待验证
    TESTING = "testing"        # 评估中
    ACTIVE = "active"          # 已启用
    INACTIVE = "inactive"      # 暂时停用
    DEPRECATED = "deprecated"  # 已淘汰


@dataclass
class ExpertRecord:
    """专家记录"""
    name: str
    phase: int                     # 所属阶段
    status: ExpertStatus = ExpertStatus.PENDING
    accuracy: float = 0.0          # 历史准确率
    n_evaluations: int = 0         # 评估次数
    contribution: float = 0.0      # 集成贡献度
    last_evaluated: Optional[str] = None
    perf_history: List[float] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)


@dataclass
class PhaseConfig:
    """阶段配置"""
    number: int
    name: str
    min_accuracy: float            # 最低准确率阈值
    target_accuracy: float         # 目标准确率
    experts: List[str]             # 该阶段专家列表
    description: str = ""


# ============================================================
# 专家管理器
# ============================================================
class ExpertManager:
    """
    分阶段专家管理器

    核心功能:
    - register(): 注册专家到指定阶段
    - run_phase(): 运行当前阶段所有专家
    - evaluate_expert(): 独立评估单一专家
    - advance_phase(): 满足条件时推进到下一阶段
    - compare_with_baseline(): 对比基准模型
    """

    # 预定义阶段
    PHASES = {
        0: PhaseConfig(0, "基准线", 0.33, 0.40,
                       ["always_home", "logistic_baseline"],
                       "建立朴素基准，验证数据管道"),
        1: PhaseConfig(1, "核心专家", 0.40, 0.45,
                       ["trend_analyzer", "h2h_analyzer", "ensemble_v3"],
                       "集成2-3个已验证专家，达成>40%"),
        2: PhaseConfig(2, "扩展专家", 0.43, 0.48,
                       ["alpha_decision", "quant_trader", "referee_model"],
                       "加入Alpha检测+赔率+裁判分析"),
        3: PhaseConfig(3, "新模块", 0.46, 0.50,
                       ["keeper_goal", "attack_efficiency", "timespace_detector"],
                       "新增门将状态+进攻效率+时空断裂"),
        4: PhaseConfig(4, "全量专家", 0.48, 0.55,
                       ["arbitrage_detector", "upset_detector", "media_intelligence"],
                       "所有专家上线，最终调优"),
    }

    def __init__(self, db_path: str = None, config_path: str = None):
        self.db_path = db_path or 'data/football_data.db'
        self.current_phase = 1
        self.experts: Dict[str, ExpertRecord] = {}
        self.expert_modules: Dict[str, Any] = {}
        self.expert_agents: Dict[str, Any] = {}
        self.baseline_results: Dict = {}
        self.phase_history: List[Dict] = []

        # 配置文件 (可选)
        self.config_path = config_path
        if config_path:
            self._load_config()

        # 初始化基准
        self._init_baselines()

    def _init_baselines(self):
        """初始化基准模型"""
        from modules.baseline import AlwaysHomeBaseline, LogisticBaseline
        self.baseline_modules = {
            'always_home': AlwaysHomeBaseline(),
            'logistic': LogisticBaseline(),
        }

    def _load_config(self):
        """从配置文件加载"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            if 'phases' in config:
                for p_data in config['phases']:
                    num = p_data.get('number', 0)
                    self.PHASES[num] = PhaseConfig(**p_data)
            if 'current_phase' in config:
                self.current_phase = config['current_phase']
        except (Exception, KeyError, IndexError, IOError, FileNotFoundError, requests.exceptions.RequestException) as e:
            logger.warning(f"配置加载失败: {e}")

    def register(self, name: str, module=None, phase: int = None,
                 status: ExpertStatus = ExpertStatus.PENDING,
                 metadata: Dict = None) -> ExpertRecord:
        """
        注册专家

        Args:
            name: 专家名称
            module: 专家模块实例 (实现 predict() 方法)
            phase: 所属阶段 (None=自动分配)
            status: 初始状态
            metadata: 额外元数据

        Returns:
            ExpertRecord
        """
        if phase is None:
            phase = self.current_phase

        record = ExpertRecord(
            name=name,
            phase=phase,
            status=status,
            metadata=metadata or {}
        )
        self.experts[name] = record

        if module is not None:
            self.expert_modules[name] = module

        logger.info(f"注册专家: {name} (Phase {phase}, {status.value})")
        return record

    def register_agent(self, name: str, agent, phase: int = None):
        """注册智能体包装器"""
        from agents.base_agent import ExpertAgent
        if not isinstance(agent, ExpertAgent):
            logger.warning(f"{name} 不是 ExpertAgent 实例")

        self.expert_agents[name] = agent
        self.register(name, phase=phase, metadata={'type': 'agent'})

    def get_active_experts(self, phase: int = None) -> List[str]:
        """获取当前阶段活跃专家"""
        phase = phase or self.current_phase
        return [
            name for name, rec in self.experts.items()
            if rec.phase == phase and rec.status == ExpertStatus.ACTIVE
        ]

    def get_phase_experts(self, phase: int) -> List[str]:
        """获取某阶段所有专家"""
        return [n for n, r in self.experts.items() if r.phase == phase]

    def evaluate_expert(self, name: str, X: np.ndarray, y: np.ndarray,
                        n_folds: int = 5) -> Dict:
        """
        独立评估单一专家

        使用时序交叉验证，返回完整评估指标。

        Args:
            name: 专家名称
            X: 特征矩阵
            y: 标签
            n_folds: 交叉验证折数

        Returns:
            评估结果字典
        """
        from sklearn.model_selection import TimeSeriesSplit

        expert = self.experts.get(name)
        if not expert:
            return {'error': f'专家 {name} 未注册'}

        module = self.expert_modules.get(name)
        if module is None:
            return {'error': f'专家 {name} 无模块实例'}

        tscv = TimeSeriesSplit(n_splits=n_folds)
        fold_accs = []
        y_true_all = []
        y_pred_all = []

        for train_idx, test_idx in tscv.split(X):
            X_test = X[test_idx]
            y_test = y[test_idx]

            # 尝试 fit (如果模块支持)
            if hasattr(module, 'fit'):
                try:
                    module.fit(X[train_idx], y[train_idx])
                except (Exception, KeyError, IndexError):
                    pass

            # 预测
            if hasattr(module, 'predict_proba'):
                try:
                    probs = module.predict_proba(X_test)
                    y_pred = np.argmax(probs, axis=1)
                except (Exception, KeyError, IndexError):
                    y_pred = np.full(len(y_test), 0)
            elif hasattr(module, 'predict'):
                try:
                    result = module.predict(X_test)
                    if isinstance(result, np.ndarray):
                        y_pred = result
                    else:
                        y_pred = np.full(len(y_test), 0)
                except (Exception):
                    y_pred = np.full(len(y_test), 0)
            else:
                y_pred = np.full(len(y_test), 0)

            acc = accuracy_score(y_test, y_pred)
            fold_accs.append(acc)
            y_true_all.extend(y_test)
            y_pred_all.extend(y_pred)

        overall_acc = np.mean(fold_accs)

        # 更新记录
        expert.accuracy = float(overall_acc)
        expert.n_evaluations += 1
        expert.last_evaluated = datetime.now().isoformat()
        expert.perf_history.append(float(overall_acc))

        report = classification_report(
            y_true_all, y_pred_all,
            target_names=['home', 'draw', 'away'],
            output_dict=True, zero_division=0
        )

        return {
            'expert': name,
            'accuracy': float(overall_acc),
            'std': float(np.std(fold_accs)),
            'fold_accuracies': [float(a) for a in fold_accs],
            'per_class': report,
            'n_samples': len(y_true_all),
            'n_folds': n_folds,
        }

    def evaluate_agent_expert(self, name: str, match_data_list: List[Dict],
                              actual_results: List[str]) -> Dict:
        """
        评估智能体包装的专家 (使用实际比赛数据)
        """
        agent = self.expert_agents.get(name)
        if agent is None:
            return {'error': f'Agent 专家 {name} 未注册'}

        correct = 0
        predictions = []
        for match_data, actual in zip(match_data_list, actual_results):
            pred_result = agent.predict(match_data)
            pred = pred_result.get('prediction', {})
            if isinstance(pred, dict):
                pred_label = max(pred, key=pred.get)
            else:
                pred_label = 'home'
            predictions.append(pred_label)
            if pred_label == actual:
                correct += 1

        acc = correct / len(actual_results) if actual_results else 0

        expert = self.experts.get(name)
        if expert:
            expert.accuracy = acc
            expert.n_evaluations += 1
            expert.last_evaluated = datetime.now().isoformat()
            expert.perf_history.append(acc)

        return {
            'expert': name,
            'accuracy': acc,
            'n_samples': len(actual_results),
            'predictions': predictions,
        }

    def run_phase(self, phase: int = None, X: np.ndarray = None,
                  y: np.ndarray = None) -> Dict:
        """
        运行一个阶段: 评估→启用/停用→输出报告

        Args:
            phase: 阶段编号 (None=当前阶段)
            X: 特征矩阵 (None=从DB加载)
            y: 标签 (None=从DB加载)
        """
        phase = phase or self.current_phase
        config = self.PHASES.get(phase)
        if not config:
            return {'error': f'阶段 {phase} 未定义'}

        logger.info(f"🚀 运行 Phase {phase}: {config.name}")
        logger.info(f"   目标准确率: {config.target_accuracy:.0%}, "
                    f"最低阈值: {config.min_accuracy:.0%}")

        # 加载数据
        if X is None or y is None:
            X, y = self._load_data()

        # 评估所有该阶段专家
        results = {}
        phase_accuracies = []

        for name in config.experts:
            if name not in self.experts:
                self.register(name, phase=phase)

            expert = self.experts[name]
            expert.status = ExpertStatus.TESTING

            eval_result = self.evaluate_expert(name, X, y)
            results[name] = eval_result

            if 'accuracy' in eval_result:
                acc = eval_result['accuracy']
                phase_accuracies.append(acc)

                # 判断是否启用
                if acc > config.min_accuracy:
                    expert.status = ExpertStatus.ACTIVE
                    expert.contribution = acc
                    logger.info(f"   ✅ {name}: {acc:.4f} → ACTIVE (>{config.min_accuracy:.0%})")
                else:
                    expert.status = ExpertStatus.INACTIVE
                    logger.info(f"   ❌ {name}: {acc:.4f} → INACTIVE (<{config.min_accuracy:.0%} 阈值)")

        # 阶段平均准确率
        phase_acc = np.mean(phase_accuracies) if phase_accuracies else 0
        self.current_phase = phase

        phase_result = {
            'phase': phase,
            'phase_name': config.name,
            'phase_accuracy': float(phase_acc),
            'min_threshold': config.min_accuracy,
            'target': config.target_accuracy,
            'passed': phase_acc > config.min_accuracy,
            'active_experts': self.get_active_experts(phase),
            'expert_results': results,
            'timestamp': datetime.now().isoformat(),
        }
        self.phase_history.append(phase_result)

        return phase_result

    def advance_phase(self, force: bool = False) -> Dict:
        """
        推进到下一阶段

        条件:
        - 当前阶段通过最低阈值
        - 或 force=True
        """
        current = self.PHASES.get(self.current_phase)
        if not current:
            return {'error': f'阶段 {self.current_phase} 未定义'}

        next_phase = self.current_phase + 1
        next_config = self.PHASES.get(next_phase)
        if not next_config:
            return {'error': '已达到最终阶段', 'phase': self.current_phase}

        # 检查条件
        if not force:
            phase_result = self.phase_history[-1] if self.phase_history else None
            if phase_result and not phase_result.get('passed', False):
                # 计算当前集成准确率
                current_acc = self._evaluate_current_ensemble()
                if current_acc < current.min_accuracy:
                    return {
                        'error': '当前阶段未达标',
                        'current_accuracy': current_acc,
                        'required': current.min_accuracy,
                        'advice': '继续优化当前阶段专家',
                    }

        old_phase = self.current_phase
        self.current_phase = next_phase

        logger.info(f"⬆️ Phase {old_phase} → Phase {next_phase}: {next_config.name}")
        logger.info(f"   新阶段目标: {next_config.target_accuracy:.0%}")
        logger.info(f"   新专家: {next_config.experts}")

        return {
            'from_phase': old_phase,
            'to_phase': next_phase,
            'next_config': {
                'name': next_config.name,
                'target': next_config.target_accuracy,
                'experts': next_config.experts,
            },
            'status': 'advanced',
        }

    def compare_with_baseline(self) -> Dict:
        """
        对比当前集成与基准模型
        如果集成优于基准 >3pp，视为"显著优于"
        """
        from modules.baseline import BaselineComparator

        comparator = BaselineComparator(self.db_path)
        baseline_results = comparator.evaluate_all_baselines()
        comparison = comparator.compare_with_ensemble(
            ensemble_accuracy=self._estimate_current_accuracy()
        )

        self.baseline_results = comparison
        return comparison

    def _load_data(self) -> Tuple[np.ndarray, np.ndarray]:
        """从数据库加载可训练数据"""
        conn = sqlite3.connect(self.db_path)
        try:
            df = pd.read_sql_query("""
                SELECT m.match_id, m.home_team, m.away_team,
                       m.home_score, m.away_score, m.competition_name,
                       mf.*
                FROM matches m
                JOIN match_features mf ON m.match_id = mf.match_id
                WHERE m.status = 'finished'
                  AND m.home_score IS NOT NULL
                  AND m.away_score IS NOT NULL
                ORDER BY m.match_date ASC
            """, conn)
        finally:
            conn.close()

        df['result'] = df.apply(
            lambda r: 0 if r['home_score'] > r['away_score']
            else (1 if r['home_score'] == r['away_score'] else 2),
            axis=1
        )

        # 选择可用特征
        feature_cols = [
            c for c in [
                'rank_diff_factor', 'form_momentum', 'h2h_factor',
                'a1', 'a2', 'a3', 'sigma_trap', 'lambda_crush',
                'epsilon_senti', 'delta_fatigue', 'beta_dev',
                'card_risk', 'aerial_advantage', 'press_intensity',
                'home_advantage', 'power_gap', 'market_consensus',
                'value_gap', 'league_strength'
            ] if c in df.columns
        ]

        if len(feature_cols) < 3:
            feature_cols = ['rank_diff_factor', 'form_momentum', 'h2h_factor']

        X = df[feature_cols].fillna(0).values
        y = df['result'].values

        logger.info(f"加载 {len(df)} 样本, {len(feature_cols)} 特征")
        return X, y

    def _estimate_current_accuracy(self) -> float:
        """估算当前集成准确率"""
        active = self.get_active_experts()
        if not active:
            return 0.0

        accs = [
            self.experts[n].accuracy for n in active
            if self.experts[n].accuracy > 0
        ]
        return float(np.mean(accs)) if accs else 0.0

    def _evaluate_current_ensemble(self) -> float:
        """评估当前集成准确率"""
        X, y = self._load_data()
        if len(X) == 0:
            return 0.0

        active = self.get_active_experts()
        if not active:
            return 0.0

        # 简单投票: 每个专家投一票
        n = len(y)
        vote_counts = np.zeros((n, 3))
        n_active = 0

        for name in active:
            module = self.expert_modules.get(name)
            if module is None:
                continue
            try:
                if hasattr(module, 'predict_proba'):
                    probs = module.predict_proba(X)
                    vote_counts += probs
                    n_active += 1
                elif hasattr(module, 'predict'):
                    preds = module.predict(X)
                    for i, p in enumerate(preds):
                        vote_counts[i, int(p)] += 1
                    n_active += 1
            except (Exception, ValueError, KeyError, IndexError):
                continue

        if n_active == 0:
            return 0.0

        y_pred = np.argmax(vote_counts, axis=1)
        return float(accuracy_score(y, y_pred))

    def status_report(self) -> Dict:
        """生成完整状态报告"""
        active = self.get_active_experts()
        all_experts = list(self.experts.keys())

        phase_config = self.PHASES.get(self.current_phase)
        current_acc = self._estimate_current_accuracy()

        report = {
            'current_phase': self.current_phase,
            'phase_name': phase_config.name if phase_config else '未知',
            'phase_target': phase_config.target_accuracy if phase_config else 0,
            'current_accuracy': round(current_acc, 4),
            'total_experts': len(all_experts),
            'active_experts': len(active),
            'active_names': active,
            'can_advance': bool(current_acc > (phase_config.min_accuracy if phase_config else 0.40)),
            'phase_history': self.phase_history[-3:],
            'expert_details': {},
        }

        for name, rec in self.experts.items():
            report['expert_details'][name] = {
                'phase': rec.phase,
                'status': rec.status.value,
                'accuracy': round(rec.accuracy, 4),
                'contribution': round(rec.contribution, 4),
                'n_evaluations': rec.n_evaluations,
                'last_evaluated': rec.last_evaluated,
            }

        # 基准对比
        if self.baseline_results:
            report['baseline_comparison'] = self.baseline_results

        return report

    def print_report(self):
        """打印格式化状态报告到控制台"""
        report = self.status_report()

        print("=" * 65)
        print(f"  哨响AI 专家管理器 — Phase {report['current_phase']}: {report['phase_name']}")
        print("=" * 65)
        print(f"  当前准确率: {report['current_accuracy']:.2%}  "
              f"(目标: {report['phase_target']:.0%})")
        print(f"  活跃专家: {report['active_experts']}/{report['total_experts']}")
        print(f"  可推进: {'✅ 是' if report['can_advance'] else '❌ 否'}")

        print(f"\n  {'专家':<25} {'阶段':<6} {'状态':<12} {'准确率':<10}")
        print(f"  {'-'*53}")
        for name, detail in report['expert_details'].items():
            status_icon = {'active': '✅', 'testing': '🔬', 'inactive': '❌',
                          'pending': '⏳', 'deprecated': '🗑️'}.get(detail['status'], '❓')
            print(f"  {status_icon} {name:<22} Phase{detail['phase']:<3} "
                  f"{detail['status']:<10} {detail['accuracy']:.4f}")

        if report.get('baseline_comparison'):
            bc = report['baseline_comparison']
            best_name = bc.get('best_baseline', '')
            best_acc = bc.get('best_baseline_accuracy', 0)
            delta = bc.get('delta_vs_best', 0)
            print(f"\n  📊 基准对比:")
            print(f"     最佳基准 ({best_name}): {best_acc:.4f}")
            print(f"     集成模型: {bc.get('ensemble_accuracy', 0):.4f}")
            print(f"     差值: {delta:+.4f} {'✅ 显著' if delta > 0.03 else '⚠️ 不显著'}")

        print("=" * 65)

    def save_state(self, filepath: str = None):
        """保存管理器状态"""
        if filepath is None:
            filepath = "data/expert_manager_state.json"

        state = {
            'current_phase': self.current_phase,
            'experts': {
                name: {
                    'name': rec.name,
                    'phase': rec.phase,
                    'status': rec.status.value,
                    'accuracy': rec.accuracy,
                    'n_evaluations': rec.n_evaluations,
                    'contribution': rec.contribution,
                    'last_evaluated': rec.last_evaluated,
                    'perf_history': rec.perf_history,
                    'metadata': rec.metadata,
                }
                for name, rec in self.experts.items()
            },
            'phase_history': self.phase_history,
            'baseline_results': self.baseline_results,
            'saved_at': datetime.now().isoformat(),
        }

        import os
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=str)

        logger.info(f"状态已保存: {filepath}")

    def load_state(self, filepath: str = "data/expert_manager_state.json"):
        """加载之前保存的状态"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                state = json.load(f)

            self.current_phase = state.get('current_phase', 1)
            for name, data in state.get('experts', {}).items():
                rec = ExpertRecord(
                    name=data['name'],
                    phase=data['phase'],
                    status=ExpertStatus(data['status']),
                    accuracy=data['accuracy'],
                    n_evaluations=data['n_evaluations'],
                    contribution=data.get('contribution', 0),
                    last_evaluated=data.get('last_evaluated'),
                    perf_history=data.get('perf_history', []),
                    metadata=data.get('metadata', {}),
                )
                self.experts[name] = rec

            self.phase_history = state.get('phase_history', [])
            self.baseline_results = state.get('baseline_results', {})
            logger.info(f"状态已加载: {filepath}")
            return True
        except FileNotFoundError:
            logger.info(f"无状态文件: {filepath}")
            return False
        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            logger.warning(f"加载状态失败: {e}")
            return False


# ============================================================
# 辅助: 专家注册工厂
# ============================================================
def create_expert_registry(db_path: str = None) -> ExpertManager:
    """
    创建预设专家注册表

    自动注册所有已知模块到对应阶段。
    """
    mgr = ExpertManager(db_path=db_path)

    # ---- Phase 1: 核心专家 ----
    mgr.register('trend_analyzer', phase=1, status=ExpertStatus.PENDING)
    mgr.register('h2h_analyzer', phase=1, status=ExpertStatus.PENDING)
    mgr.register('ensemble_v3', phase=1, status=ExpertStatus.PENDING)

    # ---- Phase 2: 扩展专家 ----
    mgr.register('alpha_decision', phase=2, status=ExpertStatus.PENDING)
    mgr.register('quant_trader', phase=2, status=ExpertStatus.PENDING)
    mgr.register('referee_model', phase=2, status=ExpertStatus.PENDING)

    # ---- Phase 3: 新增模块 ----
    mgr.register('keeper_goal', phase=3, status=ExpertStatus.PENDING)
    mgr.register('attack_efficiency', phase=3, status=ExpertStatus.PENDING)
    mgr.register('timespace_detector', phase=3, status=ExpertStatus.PENDING)

    # ---- Phase 4: 全量专家 ----
    mgr.register('arbitrage_detector', phase=4, status=ExpertStatus.PENDING)
    mgr.register('upset_detector', phase=4, status=ExpertStatus.PENDING)
    mgr.register('media_intelligence', phase=4, status=ExpertStatus.PENDING)

    return mgr


# ============================================================
# CLI
# ============================================================
if __name__ == '__main__':
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )

    parser = argparse.ArgumentParser(description='哨响AI 专家管理器')
    parser.add_argument('--db', default='data/football_data.db', help='数据库路径')
    parser.add_argument('--phase', type=int, default=1, help='运行阶段')
    parser.add_argument('--report', action='store_true', help='打印状态报告')
    parser.add_argument('--advance', action='store_true', help='尝试推进阶段')
    parser.add_argument('--save', default=None, help='保存状态')
    parser.add_argument('--load', default=None, help='加载状态')

    args = parser.parse_args()

    mgr = create_expert_registry(args.db)

    if args.load:
        mgr.load_state(args.load)

    if args.report:
        mgr.print_report()
    elif args.advance:
        result = mgr.advance_phase()
        if 'error' in result:
            print(f"❌ {result['error']}")
        else:
            print(f"✅ 已推进到 Phase {result['to_phase']}")
    else:
        # 运行指定阶段
        result = mgr.run_phase(args.phase)
        if 'error' in result:
            print(f"❌ {result['error']}")
        else:
            print(f"\n✅ Phase {result['phase']} 完成")
            print(f"   准确率: {result['phase_accuracy']:.4f}")
            print(f"   活跃专家: {result['active_experts']}")
            mgr.print_report()

    if args.save:
        mgr.save_state(args.save)
