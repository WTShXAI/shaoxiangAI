"""OU 闸门·市场锚定天花板 (2026-08-31)

目的: 判定 poisson_goals OU 信号是否值得接入。
硬约束(分析): 混合后 OU 概率的 AUC ≤ max(分量AUC) —— poisson 与 市场均源于赔率, 正相关,
混合 AUC 不可能超过两者中较强者。故只需比较「市场隐含 P(over) AUC」与用户阈值 0.70:
  - 市场 AUC < 0.70  → 混合必 < 0.70 → 过不了 70 → 不接入 (零回归)
  - 市场 AUC >= 0.70 → 才值得进一步算 poisson 混合 AUC

数据源 (SSoT, IR-01):
  - OU 盘口线+over/under价格: pipeline.opening_line.build_opening_lines(market="OU")  (events.db)
  - 实际总球: pipeline.clean_outcomes.load_clean_outcomes()  (events.db match_outcomes)
  - 市场隐含 P(over) = devig(over, under)[0]
"""
import os
import sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline.opening_line import build_opening_lines
from pipeline.clean_outcomes import load_clean_outcomes
from sklearn.metrics import roc_auc_score

THRESH = 0.70  # 用户: 过了70就接入

# ── OU 盘口主线 (events.db) ──
op = build_opening_lines(market="OU", full_time_only=True)
print(f"[opening_line] OU 主线条数: {len(op)}")
print(f"  线分布: 中位 {np.median(op['line']):.2f}, 常见 {op['line'].round(2).value_counts().head(4).to_dict()}")

# ── 干净赛果 (events.db) ──
oc = load_clean_outcomes()
oc["match_key"] = oc["home"].astype(str) + " vs " + oc["away"].astype(str)
oc["tot"] = oc["score_home"].fillna(0) + oc["score_away"].fillna(0)
print(f"[clean_outcomes] 干净赛果: {len(oc)} 场")

# ──  Join ──
df = op.merge(oc[["match_key", "tot", "league"]], on="match_key", how="inner").drop_duplicates("match_key")
print(f"[join] OU+赛果 匹配: {len(df)} 场")

line = df["line"].values
tot = df["tot"].values
over = df["over"].values.astype(float)
under = df["under"].values.astype(float)
# 市场隐含 P(over) = devig
inv = 1.0 / over + 1.0 / under
p_over = (1.0 / over) / inv
yo = (tot > line).astype(int)

auc = roc_auc_score(yo, p_over)
print(f"\n实盘 OU 市场隐含 P(over) AUC = {auc:.4f}")
print(f"  实际大球率(>线): {yo.mean()*100:.1f}% | 庄家隐含大球率 {p_over.mean()*100:.1f}%")
print(f"  中位线 {np.median(line):.2f}; 真实均总球 {tot.mean():.2f}")

print(f"\n[判定 vs 阈值 {THRESH}]")
if auc >= THRESH:
    print(f"  市场 AUC {auc:.4f} >= {THRESH} → 值得进一步算 poisson 混合 AUC")
else:
    print(f"  市场 AUC {auc:.4f} < {THRESH} → 混合 AUC ≤ max(poisson 0.5843, 市场 {auc:.4f}) < {THRESH}")
    print(f"  → 过不了 70, 按零回归铁律【不接入】 poisson OU。")
