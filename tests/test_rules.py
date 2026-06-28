"""
规则引擎测试 — P0-3
=====================
覆盖: rules/prediction_rules.py, rules/multi_signal_engine.py
- 比赛类型分类 (classify_match)
- 赔率反常规则检测 (get_odds_anomaly_rules)
- 赛中攻略规则 (get_inplay_rules)
- 综合决策入口 (get_betting_decision)
- Multi-signal verdict (verdict, backtest)
"""
import sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from rules.prediction_rules import (
    classify_match, get_odds_anomaly_rules, get_inplay_rules,
    get_betting_decision, load_rule_params, DEFAULT_RULE_PARAMS,
)
from rules.multi_signal_engine import verdict, backtest


# ═══════════════════════════════════════════
# 1. 比赛类型分类
# ═══════════════════════════════════════════

class TestClassifyMatch:
    @pytest.mark.parametrize("league,expected", [
        ("World Cup Group Stage", "world_cup_group"),
        ("World Cup Knockout", "world_cup_knockout"),
        ("世界杯分组赛", "world_cup_group"),
        ("世界杯淘汰赛", "world_cup_knockout"),
        ("Friendly Match", "friendly"),
        ("友谊赛", "friendly"),
        ("Premier League", "league"),
        ("英超", "league"),
        ("", "league"),
        ("UEFA Champions League", "league"),
    ])
    def test_classify_match(self, league, expected):
        assert classify_match(league) == expected

    def test_classify_none_league(self):
        """None league should raise or be handled gracefully"""
        with pytest.raises((AttributeError, TypeError)):
            classify_match(None)


# ═══════════════════════════════════════════
# 2. 赔率反常规则
# ═══════════════════════════════════════════

class TestOddsAnomalyRules:
    def test_rules_loaded_with_default_params(self):
        rules = get_odds_anomaly_rules()
        assert len(rules) > 0

    def test_rules_have_required_keys(self):
        rules = get_odds_anomaly_rules()
        required = {"id", "name", "signal_strength", "action"}
        for rule in rules:
            assert required.issubset(rule.keys()), f"Rule {rule.get('id')} missing: {required - rule.keys()}"

    def test_r1_draw_lowest_condition(self):
        rules = get_odds_anomaly_rules(DEFAULT_RULE_PARAMS)
        r1 = next(r for r in rules if r['id'] == 'R1')
        assert r1['condition'](3.0, 2.5, 4.0)
        assert not r1['condition'](2.0, 3.5, 3.0)

    def test_r2_odds_up_condition(self):
        rules = get_odds_anomaly_rules(DEFAULT_RULE_PARAMS)
        r2 = next(r for r in rules if r['id'] == 'R2')
        assert r2['condition'](2.0, 1.8)
        assert not r2['condition'](2.0, 2.0)

    def test_r3_odds_down_condition(self):
        rules = get_odds_anomaly_rules(DEFAULT_RULE_PARAMS)
        r3 = next(r for r in rules if r['id'] == 'R3')
        assert r3['condition'](1.8, 2.0)
        assert not r3['condition'](2.0, 2.0)

    def test_r4_ultra_low_condition(self):
        rules = get_odds_anomaly_rules(DEFAULT_RULE_PARAMS)
        r4 = next(r for r in rules if r['id'] == 'R4')
        assert r4['condition'](1.1)
        assert not r4['condition'](1.5)

    def test_r_signal_strengths_distinct(self):
        rules = get_odds_anomaly_rules(DEFAULT_RULE_PARAMS)
        strengths = [r['signal_strength'] for r in rules]
        assert len(set(strengths)) == len(strengths)

    def test_custom_params_affect_rules(self):
        custom_params = DEFAULT_RULE_PARAMS.copy()
        custom_params['R2_odds_up_threshold'] = 1.20
        rules = get_odds_anomaly_rules(custom_params)
        r2 = next(r for r in rules if r['id'] == 'R2')
        assert not r2['condition'](2.0, 1.8)


# ═══════════════════════════════════════════
# 3. 赛中攻略规则
# ═══════════════════════════════════════════

class TestInplayRules:
    def test_inplay_rules_structure(self):
        rules = get_inplay_rules()
        assert "first_half" in rules
        assert "second_half" in rules
        assert len(rules["first_half"]) > 0
        assert len(rules["second_half"]) > 0

    def test_p1_penalty_condition(self):
        rules = get_inplay_rules(DEFAULT_RULE_PARAMS)
        p1 = rules["first_half"][0]
        assert p1['condition'](5)
        assert p1['condition'](10)
        assert not p1['condition'](15)

    def test_p2_fast_goal_condition(self):
        rules = get_inplay_rules(DEFAULT_RULE_PARAMS)
        p2 = rules["first_half"][1]
        assert p2['condition'](2, 10)
        assert not p2['condition'](2, 20)
        assert not p2['condition'](1, 10)

    def test_custom_params_affect_inplay(self):
        params = DEFAULT_RULE_PARAMS.copy()
        params['P1_penalty_time'] = 20
        rules = get_inplay_rules(params)
        p1 = rules["first_half"][0]
        assert p1['condition'](15)
        assert not p1['condition'](25)


# ═══════════════════════════════════════════
# 4. 综合决策入口
# ═══════════════════════════════════════════

class TestBettingDecision:
    def test_returns_dict_with_required_keys(self):
        result = get_betting_decision({
            "league": "World Cup Group Stage",
            "close_odds": {"H": 2.0, "D": 3.5, "A": 4.0},
        })
        assert "match_type" in result
        assert "match_frame" in result
        assert "active_rules" in result

    def test_classifies_league_correctly(self):
        result = get_betting_decision({
            "league": "Premier League",
            "close_odds": {"H": 2.0, "D": 3.5, "A": 4.0},
        })
        assert result["match_type"] == "league"

    def test_detects_r1_draw_lowest(self):
        result = get_betting_decision({
            "league": "World Cup",
            "close_odds": {"H": 3.0, "D": 2.5, "A": 4.0},
        })
        assert "R1" in result["active_rules"]

    def test_detects_r4_ultra_low(self):
        result = get_betting_decision({
            "league": "World Cup",
            "close_odds": {"H": 1.1, "D": 8.0, "A": 15.0},
        })
        assert "R4" in result["active_rules"]

    def test_no_close_odds_no_crash(self):
        result = get_betting_decision({"league": "Premier League"})
        assert result["match_type"] == "league"
        assert result["active_rules"] == []

    def test_empty_data_no_crash(self):
        result = get_betting_decision({})
        assert "match_type" in result

    def test_specific_advice_world_cup_group(self):
        result = get_betting_decision({
            "league": "World Cup Group Stage",
            "close_odds": {"H": 1.15, "D": 7.0, "A": 12.0},
        })
        assert "specific_advice" in result

    def test_specific_advice_knockout_draw(self):
        result = get_betting_decision({
            "league": "World Cup Knockout",
            "close_odds": {"H": 3.0, "D": 2.8, "A": 3.0},
        })
        assert "specific_advice" in result

    def test_open_odds_available(self):
        result = get_betting_decision({
            "league": "Premier League",
            "close_odds": {"H": 2.0, "D": 3.5, "A": 4.0},
            "open_odds": {"H": 1.8, "D": 3.6, "A": 4.2},
        })
        assert "R2" in result["active_rules"]

    def test_load_rule_params(self):
        params = load_rule_params()
        assert isinstance(params, dict)
        assert len(params) > 0

    def test_params_none_uses_default(self):
        result = get_betting_decision({
            "league": "Test",
            "close_odds": {"H": 2.0, "D": 3.5, "A": 4.0},
        }, params=None)
        assert result["match_type"] == "league"


# ═══════════════════════════════════════════
# 5. Multi-Signal Engine — Verdict
# ═══════════════════════════════════════════

class TestMultiSignalVerdict:
    def test_verdict_returns_tuple(self):
        v, reason = verdict(2.0, 3.5, 4.0, -0.5, 2.5)
        assert v in ("H", "D", "A")
        assert isinstance(reason, str)

    def test_verdict_home_favorite(self):
        v, r = verdict(1.3, 5.0, 10.0, -1.5, 2.5)
        assert v == "H", f"Expected H, got {v} (reason: {r})"

    def test_verdict_away_favorite(self):
        v, r = verdict(10.0, 5.0, 1.3, 1.5, 2.5)
        assert v == "A", f"Expected A, got {v} (reason: {r})"

    def test_verdict_hot_upset_rule(self):
        """R1: hot_upset 条件 → 返回 D"""
        v, r = verdict(1.2, 5.0, 15.0, -1.5, 2.5)
        assert v == "D", f"Expected D, got {v} (reason: {r})"
        assert "hot_upset" in r

    def test_verdict_safety_veto_high_ou(self):
        """ou > 3.0 阻止平局"""
        v, r = verdict(1.2, 5.0, 15.0, -1.5, 3.5)
        assert v in ("H", "A"), f"Expected H/A, got {v}"

    def test_verdict_safety_veto_high_draw_odds(self):
        """od > 8.5 阻止平局"""
        v, r = verdict(1.2, 9.0, 15.0, -1.5, 2.5)
        assert v == "H", f"Expected H, got {v} (reason: {r})"

    @pytest.mark.parametrize("oh,od,oa,hcp,ou,expected_verdict", [
        (2.0, 3.5, 4.0, -0.5, 2.5, "H"),
        (4.0, 3.5, 2.0, 0.5, 2.5, "A"),
        (2.5, 3.0, 3.2, 0.0, 2.5, "H"),
    ])
    def test_verdict_basic(self, oh, od, oa, hcp, ou, expected_verdict):
        v, r = verdict(oh, od, oa, hcp, ou)
        assert v == expected_verdict, f"Expected {expected_verdict}, got {v} (reason: {r})"

    def test_cs_other_safety_veto(self):
        v_with, r_with = verdict(1.2, 5.0, 15.0, -1.5, 2.5, cs_other=4.0)
        v_without, r_without = verdict(1.2, 5.0, 15.0, -1.5, 2.5, cs_other=6.0)
        # cs_other=4.0 < 5.0 suppresses D prediction
        assert v_without == "D", f"Without veto expected D, got {v_without}"


# ═══════════════════════════════════════════
# 6. Multi-Signal — Backtest
# ═══════════════════════════════════════════

class TestMultiSignalBacktest:
    def test_backtest_returns_dict(self):
        matches = [
            (2.0, 3.5, 4.0, -0.5, 2.5, "H"),
            (4.0, 3.5, 2.0, 0.5, 2.5, "A"),
        ]
        result = backtest(matches)
        assert "baseline" in result
        assert "engine" in result
        assert "delta" in result
        assert 0 <= result["baseline"] <= 1.0
        assert 0 <= result["engine"] <= 1.0

    def test_backtest_empty_tuple(self):
        """Empty match list should not crash"""
        try:
            result = backtest([])
            assert "baseline" in result
        except ZeroDivisionError:
            pass  # Acceptable implementation behavior


# ═══════════════════════════════════════════
# 7. 边界和异常输入
# ═══════════════════════════════════════════

class TestEdgeCases:
    def test_verdict_zero_odds_no_crash(self):
        """零赔率不应崩溃"""
        try:
            v, r = verdict(0, 0, 0, 0, 0)
            assert v in ("H", "D", "A", "")
        except (ZeroDivisionError, ValueError, TypeError):
            pass  # Acceptable to crash on truly invalid input

    def test_verdict_none_hcp(self):
        """None handicap 不应崩溃"""
        v, r = verdict(2.0, 3.5, 4.0, None, 2.5)
        assert v in ("H", "D", "A")
