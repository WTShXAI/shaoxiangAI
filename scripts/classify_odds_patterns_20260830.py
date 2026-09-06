# -*- coding: utf-8 -*-
"""
classify_odds_patterns_20260830.py
═══════════════════════════════════════════════════════════════════
全量盘口分类 + 走势选边规则 (Phase E, 用户 2026-08-30 指令)

任务
────
1. 从 data/rollball_training.db (rb_matches, 全量 ~31.9万场) 按**开盘赔率形态**
   把每场分成 3 类盘口:
       热门盘 (hot)    : 市场热门(最低赔方)开盘赔率 ≤ 1.80
       均衡盘 (bal)    : 1.80 < 热门 ≤ 2.50
       冷门盘 (cold)   : 热门 > 2.50  (无明确热门 / 双方都长)
2. 按 盘口类 × 开盘→收盘漂移方向 建 3×3 胜平负分布表, 推导每格
   "正确选边" 规则 (选实际频率最高的方) 与方向准确率 / 真实 ROI.
3. 诚实校验: 任何格子若按"选最高频方 + 收盘赔率下注" ROI>0, 才是真 edge.
   (依据 IR-20 分析非预测 / IR-30 宁 PASS 不伪造)

漂移方向定义 (基于热门方 收盘−开盘):
       优盘 (smart)   : 热门被压低 (赔率下降, 聪明钱站热门)   rel ≤ -2%
       逆盘 (reverse) : 热门被拉高 (赔率上升)                 rel ≥ +2%
       稳盘 (stable)  : |rel| < 2%

输出: scripts/classify_odds_patterns_out.json
"""
from __future__ import annotations
import sqlite3, json, numpy as np
from collections import defaultdict

DB = "data/rollball_training.db"
OUT = "scripts/classify_odds_patterns_out.json"

PLATE_NAMES = {"hot": "热门盘", "bal": "均衡盘", "cold": "冷门盘"}
DRIFT_NAMES = {"smart": "优盘(热门压低)", "reverse": "逆盘(热门拉高)", "stable": "稳盘"}
OUTCOME = ["H", "D", "A"]
RES_MAP = {"H": 0, "D": 1, "A": 2}

REL_TH = 0.02  # 漂移显著阈值 (相对 2%)

# ── 1. 取数 ──────────────────────────────────────────────────────
con = sqlite3.connect(DB)
cur = con.cursor()
rows = cur.execute(
    """
    SELECT op_h, op_d, op_a, cl_h, cl_d, cl_a,
           drift_h, drift_d, drift_a, result
    FROM rb_matches
    WHERE result IN ('H','D','A')
      AND op_h>1.01 AND op_d>1.01 AND op_a>1.01
      AND cl_h>1.01 AND cl_d>1.01 AND cl_a>1.01
    """
).fetchall()
con.close()
N = len(rows)
print(f"[load] {N} 场有效 (result∈H/D/A 且开盘/收盘赔率合法)")

op = np.array([[r[0], r[1], r[2]] for r in rows], dtype=float)
cl = np.array([[r[3], r[4], r[5]] for r in rows], dtype=float)
drift_col = np.array([[r[6], r[7], r[8]] for r in rows], dtype=float)
res = np.array([RES_MAP[r[9]] for r in rows], dtype=int)

# ── 2. 盘口分类 (按开盘最低赔方 = 市场热门) ──────────────────────
fav = np.argmin(op, axis=1)              # 0=H 1=D 2=A
fav_op = op[np.arange(N), fav]
plate = np.where(fav_op <= 1.80, "hot", np.where(fav_op <= 2.50, "bal", "cold"))

# 热门方收盘−开盘 漂移 (相对)
fav_cl = cl[np.arange(N), fav]
fav_drift_abs = fav_cl - fav_op
fav_drift_rel = fav_drift_abs / fav_op
dclass = np.where(fav_drift_rel <= -REL_TH, "smart",
                  np.where(fav_drift_rel >= REL_TH, "reverse", "stable"))

# 漂移列符号校验 (drift 列应 = cl−op 还是 op−cl ?)
fav_drift_col = drift_col[np.arange(N), fav]
corr = np.corrcoef(fav_drift_abs, fav_drift_col)[0, 1]
print(f"[drift] fav_drift(cl-op) vs drift_col corr={corr:.3f} "
      f"(≈+1 ⇒ drift列=cl-op; ≈-1 ⇒ drift列=op-cl)")
# 用 cl-op 作为权威 (不依赖 drift 列符号约定)

# ── 3. Part A: 盘口类整体分布 ──────────────────────────────────
part_a = {}
for pc in ["hot", "bal", "cold"]:
    m = plate == pc
    n = int(m.sum())
    dist = [(res[m] == k).mean() for k in range(3)]
    fav_kind = [int((fav[m] == k).sum()) for k in range(3)]
    # 热门方实际胜率 (热门=最低赔方)
    fav_win = (res[m] == fav[m]).mean()
    # 跟热门 ROI (收盘赔率)
    odds_fav = fav_cl[m]
    roi_fav = (res[m] == fav[m]) * (odds_fav - 1.0)
    roi_fav = roi_fav.mean() - (1.0 - (res[m] == fav[m]).mean())  # 简化: mean(win*(o-1)) - mean(loss*1)
    # 准确 ROI: stake=1, 赢回收 o, 输失 1
    stake_ret = np.where(res[m] == fav[m], odds_fav, 0.0)
    roi_fav = stake_ret.mean() - 1.0
    part_a[pc] = {
        "n": n, "pct": round(100 * n / N, 2),
        "dist_HDA": [round(100 * x, 2) for x in dist],
        "fav_is_HDA": [fav_kind[0], fav_kind[1], fav_kind[2]],
        "fav_win_rate": round(100 * fav_win, 2),
        "roi_back_fav_closing": round(100 * roi_fav, 2),
    }
print("\n[Part A] 盘口类整体 胜平负% / 跟热门收盘ROI:")
for pc in ["hot", "bal", "cold"]:
    a = part_a[pc]
    print(f"  {PLATE_NAMES[pc]:<6} n={a['n']:>7} ({a['pct']:>5}%)  "
          f"H/D/A={a['dist_HDA']}  跟热门ROI={a['roi_back_fav_closing']:>6}%")

# ── 4. Part B: 3×3 盘口×漂移 表 ────────────────────────────────
def bootstrap_ci(vals_win: np.ndarray, odds: np.ndarray, n_boot=2000, seed=7):
    """返回 (hit_rate, hit_CI_lo, hit_CI_hi, roi, roi_CI_lo, roi_CI_hi)."""
    rng = np.random.default_rng(seed)
    b = rng.integers(0, len(vals_win), size=(n_boot, len(vals_win)))
    wins = vals_win[b].mean(axis=1)
    rets = np.where(vals_win[b], odds[b] - 1.0, -1.0)
    rois = rets.mean(axis=1)
    return (wins.mean(), np.percentile(wins, 2.5), np.percentile(wins, 97.5),
            rois.mean(), np.percentile(rois, 2.5), np.percentile(rois, 97.5))

part_b = {}
print("\n[Part B] 3×3 盘口×漂移 表 (modal=实际最高频方; 真实 ROI=收盘赔率下注 modal)")
for pc in ["hot", "bal", "cold"]:
    for dc in ["smart", "reverse", "stable"]:
        m = (plate == pc) & (dclass == dc)
        n = int(m.sum())
        if n < 50:
            part_b[f"{pc}|{dc}"] = {"n": n, "skip": True}
            continue
        # 每方实际频率
        dist = np.array([(res[m] == k).mean() for k in range(3)])
        modal = int(np.argmax(dist))
        # modal 方收盘赔率
        modal_odds = cl[np.arange(N), :][m][:, modal]
        win_flag = (res[m] == modal).astype(float)
        hit = win_flag.mean()
        # ROI
        stake_ret = np.where(res[m] == modal, modal_odds, 0.0)
        roi = stake_ret.mean() - 1.0
        hr_lo, hr_hi, roi_lo, roi_hi = (np.nan, np.nan, np.nan, np.nan)
        if n >= 200:
            _, hr_lo, hr_hi, _, roi_lo, roi_hi = bootstrap_ci(win_flag, modal_odds)
        part_b[f"{pc}|{dc}"] = {
            "n": n,
            "dist_HDA": [round(100 * x, 2) for x in dist],
            "modal": OUTCOME[modal],
            "modal_hit": round(100 * hit, 2),
            "modal_hit_CI": [round(100 * hr_lo, 2), round(100 * hr_hi, 2)] if n >= 200 else None,
            "roi_back_modal_closing": round(100 * roi, 2),
            "roi_CI": [round(100 * roi_lo, 2), round(100 * roi_hi, 2)] if n >= 200 else None,
            "profitable": bool(roi > 0),
        }
        tag = "✅盈利" if roi > 0 else "❌亏损"
        print(f"  {PLATE_NAMES[pc]:<6}×{DRIFT_NAMES[dc]:<14} n={n:>6}  "
              f"H/D/A={part_b[f'{pc}|{dc}']['dist_HDA']}  "
              f"选{OUTCOME[modal]} 命中{hit*100:>5.1f}%  ROI={roi*100:>6.2f}% {tag}")

# ── 5. 诚实汇总: 是否存在真 edge 格子 ──────────────────────────
profitable_cells = [k for k, v in part_b.items() if isinstance(v, dict) and v.get("profitable")]
print(f"\n[诚实校验] 全 {len([k for k in part_b if not part_b[k].get('skip')])} 格中 "
      f"ROI>0 的格子数 = {len(profitable_cells)}")
if profitable_cells:
    print("  盈利格:", profitable_cells)
else:
    print("  ⚠️ 无任何格子按'选最高频方+收盘赔率'盈利 —— 市场效率吸收, 无简单打穿策略.")

# ── 6. 写出 JSON ─────────────────────────────────────────────────
out = {
    "meta": {
        "db": DB, "table": "rb_matches", "n_total": N,
        "plate_rule": "热门方=开盘最低赔方; hot≤1.80, bal(1.80,2.50], cold>2.50",
        "drift_rule": f"热门方 (cl-op)/op; smart≤-{REL_TH}, reverse≥+{REL_TH}, else stable",
        "roi_def": "stake=1, 赢回收收盘赔率, 输失1; ROI=mean(回收)-1",
    },
    "part_a_plate": part_a,
    "part_b_plate_x_drift": part_b,
    "profitable_cells": profitable_cells,
}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"\n[done] 写出 {OUT}")
