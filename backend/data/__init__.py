"""
数据增强模块 — 基于3万场比赛的优化策略
==========================================
阶段一：数据增强与质量检查

子模块:
- enhancement.py   : DataEnhancer 主增强器 (含 CLI)
- split_temporal.py : 时序数据分割 (含 CLI)
- loader.py         : 数据库 → DataFrame 桥接
"""

from .enhancement import DataEnhancer
from .loader import load_matches_from_db

__all__ = ["DataEnhancer", "load_matches_from_db"]
