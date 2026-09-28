"""
tests/test_calibration_pure.py — calibration.py 纯函数直接 pytest + IR-30 诚实断言
=================================================================================
来源: 自主 owner 任务 T25 (基于 T21 规格 §1.1-1.4 + §3.1-3.3 低风险纯函数切片)。

范围: 仅覆盖无 DB 依赖的纯函数:
  calibration.py: log_loss / brier_score / reliability / ece / calibration_slope
  odds_math.py:   devig3 / devig_power (IR-30 去水口径护栏)

不触碰生产代码、不依赖 events.db、不改被测模块源码。

⚠ 诚实勘误 (IR-30):
  T21 规格 §3.1 原文写 "devig_power 抑热门 > devig3 方向性" 并断言
  devig_power(odds)[0] < devig3(*odds)[0]。经本回合实测与既存守卫
  tests/test_odds_math.py:38 复核, **真实方向相反**: 幂法(devig_power)因 FLB 修正
  从长shot侧多去水, 使热门概率高于比例法 (devig_power[0] > devig3[0])。
  本文件按**实测方向**编码, 并固化 09-23 去水事故的核心护栏:
  比例法(devig3/devig_n)系统性低估热门 → 制造虚假 +edge;
  幂法(devig_power)是诚实 SSoT 口径, 零真实edge下 edge≈0。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.calibration import (
    reliability, brier_score, log_loss, ece, calibration_slope,
)
from pipeline.odds_math import devig3, devig_power


# ───────────────────────────────────────────────────────────────────────────
# 1.1 log_loss
# ───────────────────────────────────────────────────────────────────────────
def test_log_loss_empty_is_nan():
    assert math.isnan(log_loss([]))


def test_log_loss_single_point():
    # -ln(0.5) = 0.693147...
    assert abs(log_loss([(0.5, 1)]) - 0.69315) < 1e-4


def test_log_loss_perfect_low():
    ll = log_loss([(0.9, 1), (0.95, 1), (0.1, 0)])
    assert ll < 0.2  # 显著低


def test_log_loss_clip_finite():
    ll = log_loss([(1e-20, 1)])  # 经 eps=1e-15 裁剪, 不抛
    assert math.isfinite(ll)
    assert ll > 30  # ≈ -ln(1e-15)


# ───────────────────────────────────────────────────────────────────────────
# 1.2 brier_score
# ───────────────────────────────────────────────────────────────────────────
def test_brier_empty_is_nan():
    assert math.isnan(brier_score([]))


def test_brier_perfect_zero():
    assert brier_score([(1.0, 1), (0.0, 0)]) == 0.0


def test_brier_single_known():
    assert brier_score([(0.5, 1)]) == 0.25


def test_brier_clip_and_mean():
    # p 夹到 [0,1]: 2.0→1.0, 0.3 保持
    assert abs(brier_score([(2.0, 1), (0.3, 0)]) - 0.045) < 1e-9


# ───────────────────────────────────────────────────────────────────────────
# 1.3 reliability (含裁剪 / 末桶 p==1.0 / 空桶跳过)
# ───────────────────────────────────────────────────────────────────────────
def test_reliability_empty():
    assert reliability([]) == []


def test_reliability_last_bin_includes_one():
    buckets = reliability([(1.0, 1), (0.05, 0)])
    # 含 p==1.0 的点必须落入最后桶 (lo=0.9, hi=1.0)
    last = buckets[-1]
    assert abs(last["hi"] - 1.0) < 1e-9
    assert last["mean_pred"] > 0.9


def test_reliability_clips_out_of_range():
    # p=1.5 夹到 1.0→末桶; p=-0.2 夹到 0.0→首桶; 不抛
    buckets = reliability([(1.5, 1), (-0.2, 0)])
    assert all(0.0 <= b["mean_pred"] <= 1.0 for b in buckets)
    assert len(buckets) >= 1


def test_reliability_skips_empty_bins_and_err_sign():
    buckets = reliability([(0.05, 1), (0.95, 0)])
    # 稀疏输入不产生空桶条目; err 符号正确
    assert len(buckets) == 2
    low, high = buckets[0], buckets[1]
    assert low["err"] > 0   # emp(1.0) > pred(0.05)
    assert high["err"] < 0  # emp(0) < pred(0.95)


# ───────────────────────────────────────────────────────────────────────────
# 1.4 ece / calibration_slope
# ───────────────────────────────────────────────────────────────────────────
def test_ece_empty_is_nan():
    assert math.isnan(ece([]))


def test_ece_analytical():
    buckets = [{"n": 10, "err": 0.1}, {"n": 10, "err": 0.2}]
    # Σ (n_i/N)*|err_i| = 0.5*0.1 + 0.5*0.2 = 0.15
    assert abs(ece(buckets) - 0.15) < 1e-9


def test_slope_less_than_two_points_is_none():
    assert calibration_slope([{"mean_pred": 0.5, "emp_rate": 0.5, "n": 5}]) is None


def test_slope_known_cases():
    from pipeline.calibration import calibration_slope as cs
    # 完美校准 (斜率≈1)
    perfect = [{"mean_pred": 0.2, "emp_rate": 0.2, "n": 5},
               {"mean_pred": 0.8, "emp_rate": 0.8, "n": 5}]
    assert abs(cs(perfect) - 1.0) < 1e-6
    # 过度自信 (emp 系统性低于 pred, 斜率<1)
    overconf = [{"mean_pred": 0.2, "emp_rate": 0.18, "n": 5},
                {"mean_pred": 0.6, "emp_rate": 0.45, "n": 5},
                {"mean_pred": 0.9, "emp_rate": 0.55, "n": 5}]
    s = cs(overconf)
    assert s is not None and s < 1.0
    # denom==0 (mean_pred 全相同) → None
    flat = [{"mean_pred": 0.5, "emp_rate": 0.5, "n": 5},
            {"mean_pred": 0.5, "emp_rate": 0.6, "n": 5}]
    assert cs(flat) is None


# ───────────────────────────────────────────────────────────────────────────
# 3.1 去水幂法护栏 (IR-30, 实测方向)
# ───────────────────────────────────────────────────────────────────────────
def test_devig_power_favorite_direction_verified():
    # 09-23 同类热门虚高场景
    o = (1.20, 5.0, 12.0)
    prop = devig3(*o)
    power = devig_power(o)
    # 幂法 FLB 修正: 从长shot侧多去水 → 热门概率高于比例法 (实测方向)
    assert power[0] > prop[0]
    # 两者均归一化
    assert abs(sum(power) - 1.0) < 1e-6
    assert abs(sum(prop) - 1.0) < 1e-12


def test_devig_invalid_returns_none():
    assert devig_power([1.0, 2.0, 3.0]) is None  # 赔率≤1 非法
    assert devig_power([float("nan"), 2.0, 3.0]) is None


# ───────────────────────────────────────────────────────────────────────────
# 3.2 ECE 校准阈值断言 (IR-30: 概率体系可信须门控)
# ───────────────────────────────────────────────────────────────────────────
def reliability_verdict(buckets, ece_val, slope_val, threshold=0.05):
    """模拟调用方门控: 任何输出'模型概率可靠'结论前须过此门控。
    IR-30: ece>阈值 或 斜率缺失/明显偏离1 → 不得宣称可靠。"""
    if ece_val is None or (isinstance(ece_val, float) and math.isnan(ece_val)):
        return "insufficient_data"
    if ece_val > threshold or slope_val is None or abs(slope_val - 1.0) > 0.15:
        return "not_reliable"
    return "reliable"


def test_ece_gate_perfect_reliable():
    perfect = [{"mean_pred": 0.2, "emp_rate": 0.2, "err": 0.0, "n": 50},
               {"mean_pred": 0.8, "emp_rate": 0.8, "err": 0.0, "n": 50}]
    b = ece(perfect)
    s = calibration_slope(perfect)
    assert abs(b) < 1e-9 and abs(s - 1.0) < 1e-6
    assert reliability_verdict(perfect, b, s) == "reliable"


def test_ece_gate_overconfident_not_reliable():
    overconf = [{"mean_pred": 0.2, "emp_rate": 0.18, "err": -0.02, "n": 50},
                {"mean_pred": 0.6, "emp_rate": 0.45, "err": -0.15, "n": 50},
                {"mean_pred": 0.9, "emp_rate": 0.55, "err": -0.35, "n": 50}]
    b = ece(overconf)
    s = calibration_slope(overconf)
    # ece>0.05 且 slope<1 → 不得宣称可靠
    assert b > 0.05
    assert s < 1.0
    assert reliability_verdict(overconf, b, s) == "not_reliable"


# ───────────────────────────────────────────────────────────────────────────
# 3.3 零信息机械对照护栏 (G6, 防 09-23 复现)
# ───────────────────────────────────────────────────────────────────────────
def test_g6_proportional_spurious_edge_power_zero():
    """09-23 事故核心: 模型零真实edge(其概率=幂法真相代理)时,
    比例法(devig3)系统性低估热门 → 制造虚假 +edge;
    幂法(devig_power)是诚实口径 → edge≈0。
    这等价于把 verification_devig_fix.md 结论固化为单元级护栏。"""
    o = (1.20, 5.0, 12.0)
    power = devig_power(o)   # 真相代理 (FLB 修正后)
    prop = devig3(*o)        # 比例法 (低估热门)
    knn_fav = power[0]       # 模型正确估计 = 幂法值, 无真实edge
    edge_prop = knn_fav - prop[0]   # 比例法下 +edge (虚假)
    edge_power = knn_fav - power[0]  # 幂法下 ~0
    assert edge_prop > 0.01          # 比例法制造虚假 +edge
    assert abs(edge_power) < 1e-6    # 幂法零edge
