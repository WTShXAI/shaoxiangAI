"""
模型层 — 足球AI预测模型
=======================
- footballai_enhanced.py : FootballAIEnhanced 集成模型
- advanced_ensemble.py   : DrawOptimizedEnsemble 平局优化模型
- train_enhanced.py      : 训练CLI
- evaluate_enhanced.py   : 评估CLI
"""
from .footballai_enhanced import FootballAIEnhanced
from .advanced_ensemble import DrawOptimizedEnsemble
from backend.training import AdaptiveTrainingStrategy

__all__ = ["FootballAIEnhanced", "DrawOptimizedEnsemble", "AdaptiveTrainingStrategy"]
