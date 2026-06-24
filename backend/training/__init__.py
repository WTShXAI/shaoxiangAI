"""
训练策略层 — 自适应训练管道
=============================
- adaptive_training.py : AdaptiveTrainingStrategy 渐进式验证/特征选择/动态权重
"""
from .adaptive_training import AdaptiveTrainingStrategy

__all__ = ["AdaptiveTrainingStrategy"]
