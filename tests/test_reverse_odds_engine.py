"""
ReverseOddsEngine 单元测试
============================
覆盖: 意图识别 / 误定价检测 / 凯利注码 / 综合分析 / 边界情况
运行: python -m pytest tests/test_reverse_odds_engine.py -v
"""
import os, sys, pytest
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'pipeline'))

from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput, Intent, AnalysisResult


@pytest.fixture
def engine():
    """加载引擎 (有模型用模型, 无模型降级规则)"""
    return ReverseOddsEngine()


# ═══════════════════════════════════════════════════════════════
# 1. OddsInput 基础
# ═══════════════════════════════════════════════════════════════
class TestOddsInput:
    def test_drift_auto_calc(self):
        """drift 应自动从 open→close 计算"""
        o = OddsInput(open_h=2.0, open_d=3.0, open_a=3.5, close_h=1.8, close_d=3.2, close_a=4.0)
        assert abs(o.drift_h - (-0.1)) < 1e-6   # (1.8-2.0)/2.0
        assert abs(o.drift_d - (0.0667)) < 0.001
        assert o.drift_a > 0  # 客胜赔率上升

    def test_implied_probs_normalized(self):
        """隐含概率去overround后应归一化(和=1)"""
        o = OddsInput(open_h=2.0, open_d=3.0, open_a=3.5, close_h=1.5, close_d=4.0, close_a=6.0)
        ih, idd, ia = o.implied_probs
        assert abs(ih + idd + ia - 1.0) < 1e-6
        assert ih > idd > ia or ih > ia  # 主胜赔率最低→概率最高

    def test_overround_positive(self):
        """overround 应为正 (庄家抽水)"""
        o = OddsInput(open_h=2.0, open_d=3.0, open_a=3.5, close_h=1.9, close_d=3.1, close_a=3.6)
        assert o.overround > 0


# ═══════════════════════════════════════════════════════════════
# 2. 意图识别
# ═══════════════════════════════════════════════════════════════
class TestIntentClassification:
    def test_honest_def_h(self, engine):
        """H赔率下调 + D/A上升 = 诚实防H"""
        o = OddsInput(open_h=2.0, open_d=3.3, open_a=3.3, close_h=1.7, close_d=3.6, close_a=3.8)
        intent, conf, pat = engine.classify_intent(o)
        assert intent == Intent.HONEST_DEF_H
        assert '↓' in pat  # H方向有下调

    def test_honest_def_a(self, engine):
        """A赔率下调 + H/D上升 = 诚实防A"""
        o = OddsInput(open_h=2.5, open_d=3.3, open_a=2.8, close_h=2.8, close_d=3.5, close_a=2.4)
        intent, conf, pat = engine.classify_intent(o)
        assert intent == Intent.HONEST_DEF_A

    def test_fake_def_h(self, engine):
        """H赔率下调但D也下调, 只有A上升 = 诱盘假H"""
        o = OddsInput(open_h=2.0, open_d=3.5, open_a=3.5, close_h=1.8, close_d=3.3, close_a=4.2)
        intent, conf, pat = engine.classify_intent(o)
        assert intent == Intent.FAKE_DEF_H

    def test_neutral_small_drift(self, engine):
        """微小drift(<2%)应判为neutral"""
        o = OddsInput(open_h=2.0, open_d=3.0, open_a=3.5, close_h=2.01, close_d=3.01, close_a=3.52)
        intent, conf, pat = engine.classify_intent(o)
        assert intent == Intent.NEUTRAL

    def test_confidence_increases_with_drift(self, engine):
        """drift幅度越大置信度越高"""
        small = OddsInput(open_h=2.0, open_d=3.0, open_a=3.0, close_h=1.9, close_d=3.2, close_a=3.2)
        large = OddsInput(open_h=2.0, open_d=3.0, open_a=3.0, close_h=1.5, close_d=3.8, close_a=3.8)
        _, conf_s, _ = engine.classify_intent(small)
        _, conf_l, _ = engine.classify_intent(large)
        assert conf_l > conf_s


# ═══════════════════════════════════════════════════════════════
# 3. 误定价检测
# ═══════════════════════════════════════════════════════════════
class TestMispricing:
    def test_returns_valid_probs(self, engine):
        """误定价检测应返回 [0,1] 范围的概率"""
        o = OddsInput(open_h=2.0, open_d=3.0, open_a=3.5, close_h=1.7, close_d=3.5, close_a=4.0)
        hit, edge, score = engine.predict_mispricing(o)
        assert 0 <= hit <= 1
        assert -1 <= edge <= 1
        assert 0 <= score <= 1

    def test_large_drift_higher_score(self, engine):
        """大drift的误定价分应>=小drift (统计规律)"""
        small = OddsInput(open_h=2.0, open_d=3.0, open_a=3.0, close_h=1.97, close_d=3.05, close_a=3.05)
        large = OddsInput(open_h=2.0, open_d=3.0, open_a=3.0, close_h=1.3, close_d=5.0, close_a=6.0)
        _, _, score_s = engine.predict_mispricing(small)
        _, _, score_l = engine.predict_mispricing(large)
        # 大drift应有更高误定价倾向 (允许相等,因降级模式)
        assert score_l >= score_s


# ═══════════════════════════════════════════════════════════════
# 4. 凯利注码
# ═══════════════════════════════════════════════════════════════
class TestKellyStake:
    def test_positive_edge_positive_kelly(self, engine):
        """真实概率 > 隐含时凯利应为正"""
        o = OddsInput(open_h=2.0, open_d=3.0, open_a=3.5, close_h=1.8, close_d=3.5, close_a=4.0)
        # 真实概率: H明显高于隐含
        true_probs = (0.65, 0.20, 0.15)  # H真实65%, 隐含约55%
        frac, side = engine.kelly_stake(o, true_probs)
        assert frac > 0
        assert side == 'H'

    def test_no_edge_no_bet(self, engine):
        """真实概率=隐含时凯利应≈0或负"""
        o = OddsInput(open_h=2.0, open_d=3.0, open_a=3.5, close_h=1.8, close_d=3.5, close_a=4.0)
        ih, idd, ia = o.implied_probs
        frac, side = engine.kelly_stake(o, (ih, idd, ia))  # 真实=隐含
        assert frac <= 0.01  # 无显著edge


# ═══════════════════════════════════════════════════════════════
# 5. 综合分析
# ═══════════════════════════════════════════════════════════════
class TestAnalyze:
    def test_returns_complete_result(self, engine):
        """analyze应返回完整结果对象"""
        o = OddsInput(open_h=2.0, open_d=3.3, open_a=3.3, close_h=1.7, close_d=3.6, close_a=3.8)
        r = engine.analyze(o)
        assert isinstance(r, AnalysisResult)
        assert r.intent in Intent
        assert len(r.implied_probs) == 3
        assert abs(sum(r.implied_probs) - 1.0) < 1e-6
        assert len(r.verdict) > 0

    def test_fake_intent_flagged_in_verdict(self, engine):
        """诱盘意图应在结论中警告"""
        o = OddsInput(open_h=2.0, open_d=3.5, open_a=3.5, close_h=1.8, close_d=3.3, close_a=4.2)
        r = engine.analyze(o)
        assert r.intent == Intent.FAKE_DEF_H
        assert '诱盘' in r.verdict or 'fake' in r.verdict.lower() or '防' in r.verdict


# ═══════════════════════════════════════════════════════════════
# 6. 边界情况
# ═══════════════════════════════════════════════════════════════
class TestEdgeCases:
    def test_equal_odds(self, engine):
        """完全相等的赔率(三不分)不应崩溃"""
        o = OddsInput(open_h=2.5, open_d=2.5, open_a=2.5, close_h=2.5, close_d=2.5, close_a=2.5)
        r = engine.analyze(o)
        assert r.intent == Intent.NEUTRAL

    def test_extreme_favorite(self, engine):
        """极端热门(赔率1.1)不应崩溃"""
        o = OddsInput(open_h=1.1, open_d=8.0, open_a=15.0, close_h=1.05, close_d=9.0, close_a=20.0)
        r = engine.analyze(o)
        assert 0 <= r.mispricing_score <= 1
