"""
pipeline.evaluation — 哨响AI 模型评估与回测单一事实源 (SSoT)

本模块集中实现 优化1.docx 规格书要求的量化评估体系：
  - 概率质量指标: LogLoss / Brier
  - 区分能力: AUC (one-vs-rest, 宏平均)
  - 校准: 校准曲线 (隐含概率 vs 实际频率)
  - 价值识别: Value Bet 检测 (model_prob > implied_prob)
  - 策略回测: Kelly / 平注 模拟 -> ROI / Sharpe

设计原则:
  - 纯标准库, 不依赖 sklearn (避免 managed 环境装包坑).
  - 评估逻辑只此一处, 任何模型(规则/梯度提升/深度学习)都复用本模块, 杜绝平行重造.
  - 数据集加载与指标计算分离, 便于接入不同数据源 (football_data.db / events.db / WC).
"""
from .metrics import (
    devig,
    log_loss,
    brier_score,
    accuracy,
    auc_ovr,
    calibration_curve,
    sharpe_ratio,
    simulate_strategy,
)
from .backtest import (
    load_dataset,
    run_backtest,
    BASELINE_REPORT_PATH,
)
from .ou_eval import (
    ou_settle,
    ou_settle_fractional,
    ou_devig,
    grade_direction,
    binary_log_loss,
    binary_brier,
    binary_accuracy,
    ou_direction_stats,
    run_ou_eval,
)

__all__ = [
    "devig", "log_loss", "brier_score", "accuracy", "auc_ovr",
    "calibration_curve", "sharpe_ratio", "simulate_strategy",
    "load_dataset", "run_backtest", "BASELINE_REPORT_PATH",
    "ou_settle", "ou_settle_fractional", "ou_devig", "grade_direction",
    "binary_log_loss", "binary_brier", "binary_accuracy",
    "ou_direction_stats", "run_ou_eval",
]
