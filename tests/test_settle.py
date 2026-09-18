"""pipeline/settle.py 赛果结算原语回归 (预测系统主指标链路的地基, 2026-09-18)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.settle import (line_from_sel, parse_score, result_1x2,
                             settle_ou, total_goals)


def test_parse_score():
    assert parse_score('2-1') == (2, 1)
    assert parse_score('0:0') == (0, 0)
    assert parse_score('2 - 1') == (2, 1)
    assert parse_score('') is None
    assert parse_score(None) is None
    assert parse_score('延期') is None


def test_total_goals_and_line():
    assert total_goals('2-1') == 3
    assert total_goals('垃圾') is None
    assert line_from_sel('under_2.5') == 2.5
    assert line_from_sel('over_1.75') == 1.75
    assert line_from_sel('no_line') is None


def test_result_1x2():
    assert result_1x2(2, 1) == 'home'
    assert result_1x2(1, 1) == 'draw'
    assert result_1x2(0, 3) == 'away'
    assert result_1x2(None, 1) is None
    assert result_1x2(2, None) is None


def test_settle_ou():
    assert settle_ou(3, 2.5) == 'over'
    assert settle_ou(2, 2.5) == 'under'
    assert settle_ou(2, 2.0) == 'push'
    assert settle_ou(None, 2.5) is None
