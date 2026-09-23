"""盈利验证台 — 常量与配置 (T01).

集中定义模型源集合、门禁阈值、免责声明, 以及 IR-32 永久禁区词表。
注意: FORBIDDEN_TOKENS 字面量本身仅出现在本文件 (词表定义处) 与 _ir32.py
(引用该列表名, 不含字面量); test_ir32_guard 扫描时豁免这两个守卫文件。
"""
from __future__ import annotations

# 模型源规范集 (主理人假设②)
MODEL_SOURCES: tuple = ("candles_ensemble", "market_baseline", "KNN")
KNN_OUTCOME_MAP: dict = {"H": "home", "D": "draw", "A": "away"}

# 五道门禁阈值
MIN_SAMPLE: int = 2500          # 验收线 (按 model_source 各自计数, 设计默认口径)
CI_ALPHA: float = 0.05          # 95% 置信区间显著性水平
ECE_MAX: float = 0.06           # 校准误差上限
SLOPE_LO: float = 0.5           # 校准斜率下限 (≈1 为佳)
SLOPE_HI: float = 1.5           # 校准斜率上限
DIR_P0: float = 1.0 / 3.0       # 1X2 方向二项检验零假设 (随机猜测准确率)

# 纸盘参数
PAPER_STAKE: float = 1.0        # 纸盘单位注 (恒为 1, 仅作对照, 不喊单)

# 免责声明 (每份报告必附)
DISCLAIMER: str = "本系统不提供下注建议、仅解释概率偏差。"

# IR-32 永久禁区词表 (不得出现在任何新生产代码/报告/测试)
FORBIDDEN_TOKENS: list = [
    "cross_book_edge",
    "multibook_consensus",
    "leyu_value_signal",
    "bet_split_source",
    "compute_value_layer",
    "bet_core",
]

__all__ = [
    "MODEL_SOURCES",
    "KNN_OUTCOME_MAP",
    "MIN_SAMPLE",
    "CI_ALPHA",
    "ECE_MAX",
    "SLOPE_LO",
    "SLOPE_HI",
    "DIR_P0",
    "PAPER_STAKE",
    "DISCLAIMER",
    "FORBIDDEN_TOKENS",
]
