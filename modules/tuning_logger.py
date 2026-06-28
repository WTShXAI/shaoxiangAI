"""
哨响AI v4.0 — 轻量调参日志 + 参数追踪 (Tuning Logger)
===========================================================
单人维护版: 不改核心模型, 只做非侵入式包装。
每次调参自动记录参数组合+指标对比, 支持回溯复盘。

这比「盲目暴力搜索」高效100倍——因为你知道上次改了什么, 效果如何。

核心能力:
  1. 参数快照: 记录每次实验的完整参数组合
  2. 指标对比: 自动对比基线, 计算增量
  3. 重要性追踪: 记录每个参数变动带来的指标变化
  4. 消融标记: 标记每个参数是独立验证还是组合验证
  5. 简单查询: "上次Draw阈值调到0.48效果如何?"

输出格式:
  logs/tuning/YYYY-MM-DD_HHMMSS.json — 每次实验一个快照
  logs/tuning/summary.json — 汇总索引

用法:
  from modules.tuning_logger import TuningLogger
  tl = TuningLogger()
  tl.log_experiment(name="Draw阈值测试", params={"draw_threshold": 0.48},
                     metrics={"acc": 0.43, "draw_f1": 0.55}, baseline={"acc": 0.40})

作者: Architecture v4.0 · Tuning Phase
日期: 2026-06-19
"""
from __future__ import annotations
import os, json, logging, time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict

logger = logging.getLogger('TuningLogger')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs', 'tuning')

# ═══════════════════════════════════════════════════════════════
# 1. 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class TuningRecord:
    """单次调参实验记录"""
    id: str                                    # 唯一ID
    timestamp: str                             # ISO时间
    experiment_name: str                       # 实验名称

    # 改动参数
    params_changed: Dict[str, Any] = field(default_factory=dict)
    params_full: Dict[str, Any] = field(default_factory=dict)   # 完整参数快照

    # 实验结果
    metrics: Dict[str, float] = field(default_factory=dict)     # 本轮指标
    baseline: Dict[str, float] = field(default_factory=dict)    # 基线指标

    # 元信息
    dataset_version: str = "2026-06"           # 数据集版本
    validation_type: str = "walk_forward"      # walk_forward / holdout / oof
    data_split: str = ""                       # 训练/验证/测试描述
    is_ablation: bool = True                   # 是否为消融验证(单参数改动)
    notes: str = ""                            # 调参思路/踩坑记录
    runtime_seconds: float = 0.0

    @property
    def delta(self) -> Dict[str, float]:
        """指标变化量"""
        return {k: self.metrics.get(k, 0) - self.baseline.get(k, 0)
                for k in set(list(self.metrics.keys()) + list(self.baseline.keys()))}

    @property
    def improved(self) -> bool:
        """是否有正向提升 (综合分)"""
        d = self.delta
        improvement = d.get('composite', 0) > 0
        # 或者: Acc不降 + 至少一项提升
        acc_ok = d.get('accuracy', -1) >= -0.01
        any_up = any(v > 0.005 for v in d.values())
        return improvement or (acc_ok and any_up)

    def summary(self) -> str:
        parts = [f"[{self.id[:8]}] {self.experiment_name}"]
        for k, v in self.params_changed.items():
            parts.append(f"{k}={v}")
        for k, v in self.delta.items():
            sign = '+' if v > 0 else ''
            parts.append(f"{k}: {sign}{v:.4f}")
        status = '✅' if self.improved else '❌'
        return f"{status} " + ' | '.join(parts)

# ═══════════════════════════════════════════════════════════════
# 2. 调参日志器
# ═══════════════════════════════════════════════════════════════

class TuningLogger:
    """
    轻量调参日志 — 不改任何模型代码, 纯记录

    每次调参前后调用 log_experiment(), 自动存档到 logs/tuning/
    """

    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self._records: List[TuningRecord] = []
        self._load_history()

    def set_baseline(self, metrics: Dict[str, float], params: Dict = None):
        """设置基准线 (通常为v3.2全默认)"""
        self._baseline_metrics = metrics.copy()
        self._baseline_params = (params or {}).copy()
        baseline_path = os.path.join(LOG_DIR, 'baseline.json')
        with open(baseline_path, 'w', encoding='utf-8') as f:
            json.dump({'metrics': metrics, 'params': params, 'timestamp': datetime.now(timezone.utc).isoformat()},
                      f, ensure_ascii=False, indent=2)
        logger.info(f"[Tuning] 基线已设置: Acc={metrics.get('accuracy',0):.2%} "
                   f"DrawF1={metrics.get('draw_f1',0):.3f} Brier={metrics.get('brier',0):.4f}")

    def log_experiment(self, name: str, params: Dict, metrics: Dict,
                       baseline: Dict = None, notes: str = "",
                       is_ablation: bool = True, runtime: float = 0.0) -> TuningRecord:
        """
        记录一次调参实验

        Args:
            name: 实验名称 (如 "Draw阈值搜索-世界杯")
            params: 本组参数变动
            metrics: 本轮指标 {accuracy, draw_f1, brier, ...}
            baseline: 对比基线 (不传则用上次设置的基线)
            notes: 备注
            is_ablation: 是否为单参数消融
            runtime: 运行耗时
        """
        baseline = baseline or getattr(self, '_baseline_metrics', {})
        full_params = (getattr(self, '_baseline_params', {}).copy() or {})
        full_params.update(params)

        record = TuningRecord(
            id=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S'),
            timestamp=datetime.now(timezone.utc).isoformat(),
            experiment_name=name,
            params_changed=params,
            params_full=full_params,
            metrics=metrics,
            baseline=baseline,
            is_ablation=is_ablation,
            notes=notes,
            runtime_seconds=runtime,
        )

        # 保存单文件
        fname = f"{record.id}.json"
        with open(os.path.join(LOG_DIR, fname), 'w', encoding='utf-8') as f:
            json.dump(asdict(record), f, ensure_ascii=False, indent=2)

        self._records.append(record)
        self._update_index()

        status = '✅' if record.improved else '❌'
        logger.info(f"[Tuning] {status} {name}: delta={record.delta}")
        return record

    def query(self, keyword: str = "", limit: int = 10) -> List[TuningRecord]:
        """查询历史实验 (按关键词)"""
        results = self._records
        if keyword:
            results = [r for r in results
                       if keyword in r.experiment_name
                       or keyword in str(r.params_changed)]
        return results[-limit:]

    def get_best(self, metric: str = 'composite') -> Optional[TuningRecord]:
        """获取某指标最优的实验"""
        if not self._records:
            return None
        return max(self._records, key=lambda r: r.metrics.get(metric, -999))

    def get_improvements(self) -> List[TuningRecord]:
        """所有有提升的实验"""
        return [r for r in self._records if r.improved]

    def print_history(self, limit: int = 20):
        """打印最近实验记录"""
        for r in self._records[-limit:]:
            print(r.summary())

    def _load_history(self):
        """加载历史记录"""
        if not os.path.exists(LOG_DIR):
            return
        for fname in sorted(os.listdir(LOG_DIR)):
            if fname.endswith('.json') and fname not in ('summary.json', 'baseline.json'):
                try:
                    with open(os.path.join(LOG_DIR, fname), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._records.append(TuningRecord(**data))
                except (json.JSONDecodeError, TypeError, KeyError) as e:
                    logger.warning("解析调优日志 %s 失败: %s", fname, e)

    def _update_index(self):
        """更新汇总索引"""
        summary = {
            'total_experiments': len(self._records),
            'improved_count': len([r for r in self._records if r.improved]),
            'last_updated': datetime.now(timezone.utc).isoformat(),
            'best': {},
        }
        for m in ['accuracy', 'draw_f1', 'brier', 'composite']:
            best = self.get_best(m)
            if best:
                summary['best'][m] = {'value': best.metrics.get(m), 'experiment': best.experiment_name}
        with open(os.path.join(LOG_DIR, 'summary.json'), 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

# ═══════════════════════════════════════════════════════════════
# 3. 综合得分计算器 (统一调参目标)
# ═══════════════════════════════════════════════════════════════

class CompositeScorer:
    """
    综合得分 — 避免单指标调参导致其他指标崩盘

    公式: composite = w_brier*(-Brier) + w_acc*Acc + w_draw*DrawF1 + w_auc*AUC
    权重可自定义, 默认足球预测最优配置
    """

    def __init__(self, w_brier: float = 0.5, w_acc: float = 0.3,
                 w_draw: float = 0.2, w_auc: float = 0.0):
        self.weights = {'brier': w_brier, 'accuracy': w_acc, 'draw_f1': w_draw, 'auc': w_auc}

    def score(self, metrics: Dict[str, float]) -> float:
        """计算综合得分"""
        s = 0.0
        if 'brier' in metrics:
            s += self.weights['brier'] * (-metrics['brier'])  # Brier越小越好, 取负
        if 'accuracy' in metrics:
            s += self.weights['accuracy'] * metrics['accuracy']
        if 'draw_f1' in metrics:
            s += self.weights['draw_f1'] * metrics['draw_f1']
        if 'auc' in metrics and self.weights.get('auc', 0) > 0:
            s += self.weights['auc'] * metrics['auc']
        return s

    def compare(self, metrics: Dict, baseline: Dict) -> Dict[str, float]:
        """对比基线的变化"""
        return {
            'composite': self.score(metrics) - self.score(baseline),
            **{k: metrics.get(k, 0) - baseline.get(k, 0) for k in metrics}
        }

# ═══════════════════════════════════════════════════════════════
# 4. 参数重要性追踪 (简易版 — 不依赖Optuna)
# ═══════════════════════════════════════════════════════════════

class ParamImportance:
    """
    参数重要性追踪 — 从调参日志中自动提取

    原理: 对比同一参数不同取值下的指标差异, 差异越大=越重要
    """

    def __init__(self, logger: TuningLogger):
        self.logger = logger

    def compute(self, metric: str = 'composite') -> Dict[str, float]:
        """
        计算各参数对指标的影响程度 [0,1]

        Returns:
            {'draw_threshold': 0.35, 'ha_gap': 0.12, ...}
        """
        records = self.logger._records
        if len(records) < 2:
            return {}

        importance = {}
        for r in records:
            for param, value in r.params_changed.items():
                if param not in importance:
                    importance[param] = []
                importance[param].append((value, r.metrics.get(metric, 0)))

        # 计算变异系数
        scores = {}
        for param, values in importance.items():
            if len(set(v[0] for v in values)) <= 1:
                continue  # 参数没变化过, 无法评估
            vals = sorted(set(v[0] for v in values))
            best = max(values, key=lambda x: x[1])
            worst = min(values, key=lambda x: x[1])
            spread = best[1] - worst[1]
            scores[param] = abs(spread)

        # 归一化
        max_score = max(scores.values()) if scores else 1.0
        return {k: v / max_score for k, v in scores.items()}

    def report(self) -> str:
        """生成重要性报告"""
        importance = self.compute()
        if not importance:
            return "样本不足, 无法计算参数重要性 (需要≥2组实验)"

        lines = ["参数重要性分析 (基于调参日志):"]
        for param, score in sorted(importance.items(), key=lambda x: -x[1]):
            bar = '█' * int(score * 20)
            recommendation = "🔴 重点优化" if score > 0.5 else ("🟡 次优先级" if score > 0.2 else "🟢 可用默认")
            lines.append(f"  {param:<25} {bar:<20} {score:.2f}  {recommendation}")
        return '\n'.join(lines)
