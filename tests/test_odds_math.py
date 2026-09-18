"""pipeline/odds_math.py devig SSoT 回归 (2026-09-19 量化系统删除后唯一去水实现)."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.odds_math import devig2, devig3, devig_n, devig_power, overround


def test_devig3_proportional():
    p = devig3(2.0, 3.0, 4.0)
    assert abs(sum(p) - 1.0) < 1e-12
    assert all(x > 0 for x in p)
    # 比例法: 与手算一致
    inv = [0.5, 1 / 3, 0.25]
    s = sum(inv)
    assert all(abs(a - b / s) < 1e-12 for a, b in zip(p, inv))
    assert devig3(0, 2.0, 3.0) is None
    assert devig3(-1.5, 2.0, 3.0) is None


def test_devig_n_and_devig2():
    p = devig_n([1.5, 2.5])
    assert abs(sum(p) - 1.0) < 1e-12
    p2 = devig2(1.5, 2.5)
    assert abs(p2[0] - p[0]) < 1e-12 and abs(p2[1] - p[1]) < 1e-12
    assert devig_n([1.0, 2.0]) is None  # 赔率≤1 非法
    assert devig_n(['x', 2.0]) is None


def test_devig_power_sums_to_one_and_finite():
    p = devig_power([2.0, 3.5, 4.0])
    assert abs(sum(p) - 1.0) < 1e-6
    assert all(math.isfinite(x) and x > 0 for x in p)
    # FLB 修正方向: 幂法从长shot侧多去水 → 热门概率高于比例法
    prop = devig3(2.0, 3.5, 4.0)
    assert p[0] > prop[0]
    assert devig_power([1.0]) is None


def test_overround():
    assert abs(overround([2.0, 3.0, 4.0]) - (0.5 + 1 / 3 + 0.25 - 1)) < 1e-12
    assert overround([0]) == 0.0
