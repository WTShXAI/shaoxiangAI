# -*- coding: utf-8 -*-
"""量化自动投注模拟引擎.

复用项目 SSoT (单一事实源), 绝不重造数学:
  - pipeline.deep_report.compute_value_layer   (1X2 价值层)
  - pipeline.deep_report.compute_submarket_value (子市场价值层)
  - pipeline.score_model.predict_score          (OIP 波胆概率矩阵)
  - scripts.bet_core.safe_stake                 (凯利注码, 含封顶/护栏)

真实数据源:
  - data/football_data.db::live_odds_raw   (13269行, 多庄实时赔率)
  - data/football_data.db::odds_features   (302966行, 双庄历史可结算)
"""
from .auto_trader import QuantEngine, get_engine

__all__ = ["QuantEngine", "get_engine"]
