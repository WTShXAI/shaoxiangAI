"""盈利验证台 (walk-forward) — 诚实验证预测能否产生可证伪的统计边缘。

门禁: 六道关 (G1 样本 / G2 ROI CI / G3 预测质量 / G4 校准 / G5 方向二项 /
      G6 零信息机械对照)。G6 为 2026-09-23 新增: 拦截"去水偏差 + 买热门"伪 EDGE。
三态判定: EDGE / NO EDGE / INCONCLUSIVE。
"""
from __future__ import annotations

__all__ = ["schema", "ledger", "ingest", "metrics", "gates", "report"]
