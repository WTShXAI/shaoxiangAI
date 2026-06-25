"""
全局常量 — v5.2.14
集中管理所有 fallback 值, 消除散落硬编码。

铁律:
- 任何模块需要默认值时, 从此文件导入, 严禁再写裸数字。
- 实际预测中应优先使用 league_d_rates 等上下文数据, 此文件仅在无数据时兜底。

注意: DEFAULT_DRAW_PROB=0.34 是 DrawExpert 的历史基线值。
      这个值在降级/fallback 场景中是合理的保守默认（略高于联赛平均, 避免低估平局）。
      DrawExpert 的有害偏置根因在 unified_predictor.py:565 的假信号注入（已修复）,
      而非 0.34 这个数字本身。
"""
# ── 概率 fallback（降级兜底用，保守偏高） ──
DEFAULT_DRAW_PROB = 0.34   # DrawExpert 基线, fallback 保守默认
DEFAULT_HOME_PROB = 0.33   # 均匀三等分基线
DEFAULT_AWAY_PROB = 0.33   # 均匀三等分基线

# ── 统计参考值（分析/报表用，非 fallback） ──
LEAGUE_AVG_DRAW = 0.25   # 联赛平均平局率
CUP_AVG_DRAW = 0.35      # 杯赛平均平局率

# ── 其他 fallback ──
DEFAULT_CONFIDENCE = 0.1
