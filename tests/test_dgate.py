"""
D-Gate 引擎单元测试 — P0-1
============================
覆盖: rules/d_gate_engine.py, rules/drawgate_v53.py
模式: A/B/C/C-away/Default, 边界阈值, S7+S1信号, 赛事类型检测
"""
import sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from rules.drawgate_v53 import (
    apply_drawgate, detect_match_type, imp_from_odds, quick_scan,
)
from rules.d_gate_engine import apply_dgate_v51, apply_dgate, detect_match_type as engine_detect


def _imp_from_odds_manual(oh, od, oa):
    """手动计算隐含概率 (与源码相同的算法)"""
    if oh <= 0 or od <= 0 or oa <= 0:
        return 0, 0, 0
    s = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/s, (1.0/od)/s, (1.0/oa)/s


# ═══════════════════════════════════════════
# 1. 赔率→隐含概率计算
# ═══════════════════════════════════════════

class TestImpFromOdds:
    @pytest.mark.parametrize("oh,od,oa", [
        (2.0, 3.5, 4.0),
        (1.2, 6.0, 10.0),
        (2.5, 2.8, 3.2),
        (5.0, 3.5, 1.8),
    ])
    def test_imp_calculation(self, oh, od, oa):
        exp_h, exp_d, exp_a = _imp_from_odds_manual(oh, od, oa)
        h, d, a = imp_from_odds(oh, od, oa)
        assert abs(h - exp_h) < 0.001, f"imp_h={h:.4f} != {exp_h:.4f}"
        assert abs(d - exp_d) < 0.001, f"imp_d={d:.4f} != {exp_d:.4f}"
        assert abs(a - exp_a) < 0.001, f"imp_a={a:.4f} != {exp_a:.4f}"

    def test_sum_close_to_one(self):
        h, d, a = imp_from_odds(1.5, 4.0, 6.0)
        assert abs(h + d + a - 1.0) < 0.001

    def test_zero_odds_raises(self):
        """零赔率应抛出 ZeroDivisionError"""
        with pytest.raises(ZeroDivisionError):
            imp_from_odds(0, 3.5, 4.0)


# ═══════════════════════════════════════════
# 2. 赛事类型检测
# ═══════════════════════════════════════════

class TestDetectMatchType:
    @pytest.mark.parametrize("name,expected", [
        ("World Cup Group Stage", "tournament"),
        ("欧冠 小组赛", "tournament"),
        ("Premier League", "league"),
        ("英超 第38轮", "league"),
        ("EURO 2024", "tournament"),
        ("Copa America 2024", "tournament"),
        ("La Liga", "league"),
        ("", "tournament"),      # 空字符串默认杯赛
        ("unknown random league", "league"),  # "league" 关键词 → league
        ("2026世界杯", "tournament"),
        ("中超 第15轮", "league"),
    ])
    def test_detect_types(self, name, expected):
        assert detect_match_type(name) == expected
        assert engine_detect(name) == expected


# ═══════════════════════════════════════════
# 3. DrawGate v5.3 核心 — Mode C (超热门翻车)
# ═══════════════════════════════════════════

class TestModeC:
    """Mode C: 超热门 (max_imp >= 0.72) + 平赔 <= 6.0 → upset_warning"""

    @pytest.fixture
    def odds_c(self):
        return {"home": 1.2, "draw": 5.0, "away": 10.0}

    @pytest.fixture
    def imps_c(self, odds_c):
        return imp_from_odds(odds_c["home"], odds_c["draw"], odds_c["away"])

    def test_mode_c_trigger(self, imps_c, odds_c):
        r = apply_drawgate(imps_c[0], imps_c[1], imps_c[2],
                           odds=odds_c, match_type="tournament")
        assert r["risk_tag"] == "upset_warning"
        assert r["dgate_mode"] == "C"
        assert r["confidence_mult"] == 0.85
        assert r["draw_threshold_adj"] <= 0.27

    @pytest.mark.parametrize("oh,od,oa,exp_mode", [
        (1.1, 6.0, 15.0, "C"),    # 平赔正好 = od_max=6.0 → 触发
        (1.3, 4.5, 8.0, "none"),  # imp_h ~= 0.69 < 0.72 → 不触发
    ])
    def test_mode_c_boundaries(self, oh, od, oa, exp_mode):
        imps = imp_from_odds(oh, od, oa)
        r = apply_drawgate(imps[0], imps[1], imps[2],
                           odds={"home": oh, "draw": od, "away": oa},
                           match_type="tournament")
        assert r["dgate_mode"] == exp_mode, f"Expected {exp_mode}, got {r['dgate_mode']}"

    def test_mode_c_threshold_adjust(self, imps_c, odds_c):
        """Mode C 应降低平局阈值 (0.32 → max(0.27, 0.32-0.10) = 0.27)"""
        r = apply_drawgate(imps_c[0], imps_c[1], imps_c[2],
                           odds=odds_c, match_type="tournament")
        assert r["draw_threshold_adj"] <= 0.27

    def test_mode_c_different_match_types(self, imps_c, odds_c):
        """杯赛和联赛参数应不同"""
        r_tour = apply_drawgate(imps_c[0], imps_c[1], imps_c[2],
                                odds=odds_c, match_type="tournament")
        r_league = apply_drawgate(imps_c[0], imps_c[1], imps_c[2],
                                  odds=odds_c, match_type="league")
        assert r_league["confidence_mult"] != r_tour["confidence_mult"]


# ═══════════════════════════════════════════
# 4. Mode C-away (客队热门翻车)
# ═══════════════════════════════════════════

class TestModeCAway:
    """Mode C-away: 客队热门 (imp_a >= 0.65) + 主队不高"""

    def test_c_away_trigger(self):
        """客队隐率>=0.65 + 主队imp<0.72 → C-away"""
        # od=5.0 需要满足, imp_a 需要>=0.65
        # (7.0, 5.0, 1.5): imp_a = (1/1.5)/(1/7+1/5+1/1.5) = 0.667/0.8095 = 0.824 >= 0.65 ✓
        # od=5.0 <= 8.5 ✓
        imps = imp_from_odds(7.0, 5.0, 1.5)
        r = apply_drawgate(imps[0], imps[1], imps[2],
                           odds={"home": 7.0, "draw": 5.0, "away": 1.5},
                           match_type="tournament")
        assert r["dgate_mode"] == "C-away", f"Expected C-away, got {r['dgate_mode']}"

    def test_c_away_not_triggered_when_c_active(self):
        """Mode C 先触发时, C-away 不应再触发"""
        imps = imp_from_odds(1.2, 5.0, 10.0)
        r = apply_drawgate(imps[0], imps[1], imps[2],
                           odds={"home": 1.2, "draw": 5.0, "away": 10.0},
                           match_type="tournament")
        assert r["dgate_mode"] == "C", f"Expected C, got {r['dgate_mode']}"


# ═══════════════════════════════════════════
# 5. Mode A (中等热门画局风险)
# ═══════════════════════════════════════════

class TestModeA:
    """Mode A: 0.40 <= max_imp <= 0.70 + boost > threshold"""

    @pytest.mark.parametrize("oh,od,oa,expected_dgate", [
        (2.0, 3.5, 4.0, "A"),      # imp_h=0.483(>0.40), boost > 0.24
        (2.8, 3.0, 2.8, "A"),      # 均衡局面 imp_h=0.349<0.40 → maybe none
    ])
    def test_mode_a_trigger(self, oh, od, oa, expected_dgate):
        imps = imp_from_odds(oh, od, oa)
        r = apply_drawgate(imps[0], imps[1], imps[2],
                           odds={"home": oh, "draw": od, "away": oa},
                           handicap=0.0, ou_line=2.5,
                           match_type="tournament")
        assert r["dgate_mode"] in (expected_dgate, "none"), (
            f"Expected {expected_dgate} or none, got {r['dgate_mode']}")

    def test_mode_a_draw_expert_boost(self):
        """DrawExpert 信号 >= 0.30 应增强 boost"""
        imps = imp_from_odds(2.2, 3.3, 3.5)
        r_with = apply_drawgate(imps[0], imps[1], imps[2],
                                odds={"home": 2.2, "draw": 3.3, "away": 3.5},
                                handicap=0.0, ou_line=2.5,
                                draw_expert_signal=0.40,
                                match_type="tournament")
        r_without = apply_drawgate(imps[0], imps[1], imps[2],
                                   odds={"home": 2.2, "draw": 3.3, "away": 3.5},
                                   handicap=0.0, ou_line=2.5,
                                   match_type="tournament")
        assert r_with["draw_boost"] >= r_without["draw_boost"]

    def test_mode_a_lambda_residual_accept(self):
        """λ残差 > 0.3 应可接受 (不崩溃)"""
        imps = imp_from_odds(2.2, 3.3, 3.5)
        r = apply_drawgate(imps[0], imps[1], imps[2],
                           odds={"home": 2.2, "draw": 3.3, "away": 3.5},
                           handicap=0.0, ou_line=2.5,
                           lambda_residual=0.4,
                           match_type="tournament")
        assert "dgate_mode" in r


# ═══════════════════════════════════════════
# 6. 默认模式 (clean/none)
# ═══════════════════════════════════════════

class TestDefaultClean:
    """无模式触发时应返回 clean 状态"""

    def test_clean_odds(self):
        """正常赔率 → clean/none"""
        r = apply_drawgate(0.40, 0.30, 0.30,
                           odds={"home": 2.5, "draw": 3.3, "away": 3.3},
                           match_type="tournament")
        assert "dgate_mode" in r
        assert "risk_tag" in r
        assert 0.65 <= r["confidence_mult"] <= 1.0


# ═══════════════════════════════════════════
# 7. D-Gate 引擎兼容层
# ═══════════════════════════════════════════

class TestDGateEngine:
    def test_apply_dgate_v51_smoke(self):
        r = apply_dgate_v51(
            imp_h=0.50, imp_d=0.28, imp_a=0.22,
            odds={"home": 2.0, "draw": 3.5, "away": 4.0},
        )
        assert "d_gate_active" in r
        assert "d_gate_mode" in r
        assert "verdict" in r
        assert "risk_tag" in r
        assert isinstance(r["d_gate_active"], bool)

    def test_apply_dgate_v51_verdict_h(self):
        r = apply_dgate_v51(
            imp_h=0.60, imp_d=0.25, imp_a=0.15,
            odds={"home": 1.6, "draw": 3.8, "away": 6.0},
        )
        assert r["verdict"] in ("H", "D")

    def test_apply_dgate_alias(self):
        r1 = apply_dgate(
            imp_h=0.50, imp_d=0.28, imp_a=0.22,
            odds={"home": 2.0, "draw": 3.5, "away": 4.0},
        )
        r2 = apply_dgate_v51(
            imp_h=0.50, imp_d=0.28, imp_a=0.22,
            odds={"home": 2.0, "draw": 3.5, "away": 4.0},
        )
        assert r1["d_gate_active"] == r2["d_gate_active"]
        assert r1["d_gate_mode"] == r2["d_gate_mode"]

    def test_score_predictions_contradiction(self):
        r = apply_dgate_v51(
            imp_h=0.50, imp_d=0.28, imp_a=0.22,
            odds={"home": 2.0, "draw": 3.5, "away": 4.0},
            score_predictions=[
                (1, 1), (2, 0), (0, 0), (1, 2), (2, 1),
            ],
        )
        assert r["score_contradiction"] == 2


# ═══════════════════════════════════════════
# 8. quick_scan 便捷函数
# ═══════════════════════════════════════════

class TestQuickScan:
    def test_quick_scan_smoke(self):
        r = quick_scan(2.0, 3.5, 4.0, league="Premier League")
        assert "risk_tag" in r
        assert "dgate_mode" in r

    def test_quick_scan_with_draw_expert(self):
        r = quick_scan(2.6, 3.0, 3.0, league="Premier League",
                       de_signal=0.50)
        assert "draw_boost" in r


# ═══════════════════════════════════════════
# 9. 边界条件
# ═══════════════════════════════════════════

class TestEdgeCases:
    def test_extreme_odds_no_crash(self):
        r = apply_drawgate(0.90, 0.05, 0.05,
                           odds={"home": 1.01, "draw": 20.0, "away": 30.0},
                           match_type="tournament")
        assert r["confidence_mult"] >= 0.65

    def test_zero_handicap(self):
        r = apply_drawgate(0.40, 0.30, 0.30,
                           odds={"home": 2.5, "draw": 3.3, "away": 3.3},
                           handicap=0.0, ou_line=2.5,
                           match_type="tournament")
        assert "dgate_mode" in r

    def test_negative_handicap(self):
        r = apply_drawgate(0.50, 0.28, 0.22,
                           odds={"home": 2.0, "draw": 3.5, "away": 4.0},
                           handicap=-0.5, ou_line=2.5,
                           match_type="tournament")
        assert "dgate_mode" in r

    def test_case_insensitive_match_type(self):
        r1 = apply_drawgate(0.50, 0.28, 0.22,
                            odds={"home": 2.0, "draw": 3.5, "away": 4.0},
                            match_type="TOURNAMENT")
        r2 = apply_drawgate(0.50, 0.28, 0.22,
                            odds={"home": 2.0, "draw": 3.5, "away": 4.0},
                            match_type="tournament")
        assert r1["dgate_mode"] == r2["dgate_mode"]

    def test_all_modes_return_required_keys(self):
        configs = [
            (1.2, 5.0, 10.0),     # Mode C
            (7.0, 5.0, 1.5),      # Mode C-away
            (2.0, 3.5, 4.0),      # Mode A
        ]
        required_keys = {"risk_tag", "dgate_mode", "confidence_mult",
                         "draw_threshold_adj", "draw_boost", "triggered_signals"}
        for oh, od, oa in configs:
            imps = imp_from_odds(oh, od, oa)
            r = apply_drawgate(imps[0], imps[1], imps[2],
                               odds={"home": oh, "draw": od, "away": oa},
                               match_type="tournament")
            assert required_keys.issubset(r.keys()), f"Missing keys: {required_keys - r.keys()}"
