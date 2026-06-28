"""
output_schema.py 单元测试 (pytest 重构版)
=========================================
覆盖: ThreeWayProbability, DistributionExtension, ReasoningChain,
      ConfidenceAssessment, EvidencePackage, UnifiedPrediction,
      FusedPrediction, SchemaValidator, TerminologyInjector
"""
import sys, os, json, math

import pytest
from modules.output_schema import (
    ThreeWayProbability, DistributionExtension, ReasoningChain,
    ConfidenceAssessment, EvidencePackage, UnifiedPrediction,
    FusedPrediction, ExpertContribution, SchemaValidator,
    TerminologyInjector, MarketType,
    create_simple_prediction, create_fallback_prediction,
    create_from_v3_output, _confidence_to_level,
)

class TestThreeWayProbability:
    def test_sum_one(self):
        twp = ThreeWayProbability(home=0.5, draw=0.3, away=0.2)
        assert abs(twp.home + twp.draw + twp.away - 1.0) < 0.001

    def test_top_prediction_h(self):
        twp = ThreeWayProbability(home=0.5, draw=0.3, away=0.2)
        assert twp.top_prediction() == "H"

    def test_top_prediction_d(self):
        twp = ThreeWayProbability(home=0.3, draw=0.4, away=0.3)
        assert twp.top_prediction() == "D"

    def test_top_prediction_a(self):
        twp = ThreeWayProbability(home=0.2, draw=0.3, away=0.5)
        assert twp.top_prediction() == "A"

    def test_margin(self):
        twp = ThreeWayProbability(home=0.5, draw=0.3, away=0.2)
        assert abs(twp.margin() - (-0.2)) < 0.001

    def test_auto_normalization(self):
        tmp = ThreeWayProbability(home=50, draw=30, away=20)
        assert abs(tmp.home + tmp.draw + tmp.away - 1.0) < 0.001
        assert abs(tmp.home - 0.5) < 0.001
        assert abs(tmp.draw - 0.3) < 0.001

    def test_all_zeros_no_crash(self):
        tmp = ThreeWayProbability(home=0, draw=0, away=0)
        assert abs(tmp.home + tmp.draw + tmp.away) < 0.001

    def test_negative_values_no_crash(self):
        tmp = ThreeWayProbability(home=-0.1, draw=0.5, away=0.6)
        assert tmp is not None

    def test_from_dict(self):
        d = {"home": 0.6, "draw": 0.25, "away": 0.15}
        twp = ThreeWayProbability.from_dict(d)
        assert abs(twp.home - 0.6) < 0.001
        assert abs(twp.draw - 0.25) < 0.001

    def test_from_dict_empty(self):
        twp = ThreeWayProbability.from_dict({})
        assert abs(twp.home - 0.33) < 0.01
        assert abs(twp.draw - 0.34) < 0.01

    def test_to_dict(self):
        twp = ThreeWayProbability(home=0.5, draw=0.3, away=0.2)
        d = twp.to_dict()
        assert "home" in d and "draw" in d and "away" in d
        assert all(isinstance(v, float) for v in d.values())

class TestDistributionExtension:
    def test_empty_dict(self):
        dist = DistributionExtension()
        assert dist.to_dict() == {}

    def test_with_scores_and_goals(self):
        dist = DistributionExtension(
            score_probs={"1-0": 0.12, "2-1": 0.08},
            goal_probs={0: 0.08, 1: 0.15, 2: 0.30, 3: 0.25},
        )
        d = dist.to_dict()
        assert "score" in d
        assert "goals" in d
        assert isinstance(list(d["goals"].keys())[0], str)

class TestReasoningChain:
    @pytest.fixture
    def rc(self):
        return ReasoningChain(
            summary="主队优势明显",
            steps=[{"expert": "季泊松", "finding": "λ_H=2.1 λ_A=0.8", "impact": "+0.15 to H"}],
            contradictions=["赔率信号与模型预测方向相反"],
            key_factors=["spread=8.5", "主场优势"],
        )

    def test_to_dict(self, rc):
        d = rc.to_dict()
        assert d["summary"] == "主队优势明显"
        assert len(d["steps"]) == 1
        assert len(d["contradictions"]) == 1
        assert len(d["key_factors"]) == 2

class TestConfidenceAssessment:
    def test_with_fields(self):
        ca = ConfidenceAssessment(
            overall=0.72, level="high",
            calibration_score=0.68,
            uncertainty_band=(0.65, 0.79),
            expert_agreement=0.85,
        )
        d = ca.to_dict()
        assert d["overall"] == 0.72
        assert d["uncertainty"] == [0.65, 0.79]
        assert d["expert_agreement"] == 0.85

    def test_null_fields(self):
        ca = ConfidenceAssessment()
        d = ca.to_dict()
        assert d["calibration_score"] is None
        assert d["uncertainty"] is None

class TestEvidencePackage:
    def test_basic(self):
        ev = EvidencePackage(
            data_sources=["Interwetten", "球队近期战绩"],
            feature_contributions={"spread": 0.15, "drift": 0.08},
            model_version="v4.0-dev",
            data_freshness="2026-06-18",
            degradation_indicators=[],
        )
        d = ev.to_dict()
        assert len(d["data_sources"]) == 2
        assert len(d["feature_contributions"]) == 2
        assert d["model_version"] == "v4.0-dev"

class TestUnifiedPrediction:
    def test_simple_creation(self):
        up = create_simple_prediction(0.45, 0.30, 0.25, expert_id="test", confidence=0.72, summary="test")
        d = up.to_dict()
        for k in ["prediction", "reasoning", "confidence", "evidence", "meta"]:
            assert k in d
        assert d["prediction"]["top_pick"] == "H"

    def test_validate_pass(self):
        up = create_simple_prediction(0.45, 0.30, 0.25, expert_id="test", confidence=0.72, summary="test")
        ok, errs = up.validate()
        assert ok, f"errors: {errs}"

    def test_v3_compat(self):
        up = create_simple_prediction(0.45, 0.30, 0.25, expert_id="test", confidence=0.72, summary="test")
        v3 = up.to_v3_compat()
        for k in ["prediction", "confidence", "top_pick", "reasoning_summary", "model_version"]:
            assert k in v3
        assert abs(v3["prediction"]["home"] - 0.45) < 0.01

    def test_create_from_v3_output(self):
        v3_input = {"prediction": {"home": 0.55, "draw": 0.28, "away": 0.17}, "confidence": 0.68, "reasoning_summary": "v3 test"}
        up = create_from_v3_output(v3_input)
        assert abs(up.probability.home - 0.55) < 0.01
        assert abs(up.confidence.overall - 0.68) < 0.01

    def test_create_from_v3_empty(self):
        up = create_from_v3_output({})
        assert abs(up.probability.home - 0.33) < 0.01

    def test_validate_catches_bad_values(self):
        up = UnifiedPrediction(
            probability=ThreeWayProbability(home=-0.1, draw=1.2, away=-0.1),
            confidence=ConfidenceAssessment(overall=0.5),
            reasoning=ReasoningChain(summary="bad"),
        )
        ok, errs = up.validate()
        assert not ok
        assert len(errs) >= 2

    def test_validate_catches_nan(self):
        up = UnifiedPrediction(
            probability=ThreeWayProbability(home=float('nan'), draw=0.5, away=0.5),
            confidence=ConfidenceAssessment(),
            reasoning=ReasoningChain(),
        )
        ok, errs = up.validate()
        assert not ok

    def test_fallback_prediction(self):
        fb = create_fallback_prediction("冷启动降级")
        assert abs(fb.probability.home + fb.probability.draw + fb.probability.away - 1.0) < 0.01
        assert fb.confidence.overall == 0.1
        assert len(fb.evidence.degradation_indicators) == 1
        ok, _ = fb.validate()
        assert ok

class TestFusedPrediction:
    def test_basic(self):
        contribs = [
            ExpertContribution(
                expert_id="expert_1", expert_name="季泊松", domain="quantization",
                probability=ThreeWayProbability(0.5, 0.3, 0.2),
                weight=0.4, confidence=0.75, reasoning_summary="主队强", execution_time_ms=5.2, status="success",
            ),
            ExpertContribution(
                expert_id="expert_2", expert_name="杜博弈", domain="game_theory",
                probability=ThreeWayProbability(0.45, 0.35, 0.2),
                weight=0.35, confidence=0.68, reasoning_summary="赔率无异常", execution_time_ms=8.1, status="success",
            ),
        ]
        fp = FusedPrediction(
            probability=ThreeWayProbability(0.48, 0.32, 0.20),
            confidence=ConfidenceAssessment(overall=0.72, level="high"),
            reasoning=ReasoningChain(summary="融合预测"),
            expert_outputs=contribs,
            fusion_method="weighted_vote",
            expert_id="fusion",
        )
        d = fp.to_dict()
        assert "fusion" in d
        assert d["fusion"]["expert_count"] == 2
        assert len(d["fusion"]["contributions"]) == 2
        ok, _ = fp.validate()
        assert ok

    def test_empty_contributions(self):
        fp = FusedPrediction(
            probability=ThreeWayProbability(0.33, 0.34, 0.33),
            confidence=ConfidenceAssessment(),
            reasoning=ReasoningChain(),
        )
        d = fp.to_dict()
        assert d["fusion"]["expert_count"] == 0

class TestSchemaValidator:
    def test_validate_full_pass(self):
        up = create_simple_prediction(0.45, 0.30, 0.25, expert_id="test", confidence=0.72, summary="test")
        ok, _ = SchemaValidator.validate_full(up.to_dict())
        assert ok

    def test_validate_light_pass(self):
        up = create_simple_prediction(0.45, 0.30, 0.25, expert_id="test", confidence=0.72, summary="test")
        assert SchemaValidator.validate_light(up.to_dict())

    def test_validate_light_fail_empty(self):
        assert not SchemaValidator.validate_light({})

    def test_validate_full_bad_sum(self):
        assert not SchemaValidator.validate_full({
            "prediction": {"market": "1X2", "probabilities": {"home": 0.8, "draw": 0.5, "away": 0.3}, "top_pick": "H"},
            "confidence": {"overall": 0.5, "level": "medium"},
            "reasoning": {"summary": ""},
            "meta": {"expert_id": "test"},
        })[0]

class TestTerminologyInjector:
    def test_inject_quantization(self):
        rc = ReasoningChain()
        rc_injected = TerminologyInjector.inject(rc, "quantization")
        assert len(rc_injected.key_factors) == 3
        assert "泊松强度λ" in str(rc_injected.key_factors)

    def test_no_inject_if_has_factors(self):
        rc = ReasoningChain(key_factors=["已有因子"])
        rc_no_inject = TerminologyInjector.inject(rc, "quantization")
        assert len(rc_no_inject.key_factors) == 1

    def test_glossary_7_domains(self):
        glossary = TerminologyInjector.get_terminology_glossary()
        assert len(glossary) == 7, f"got {len(glossary)} domains: {list(glossary.keys())}"

class TestConfidenceHelpers:
    @pytest.mark.parametrize("value,expected", [
        (0.90, "very_high"),
        (0.80, "high"),
        (0.60, "medium"),
        (0.40, "low"),
        (0.10, "very_low"),
        (0.00, "very_low"),
        (-0.5, "very_low"),
    ])
    def test_confidence_to_level(self, value, expected):
        assert _confidence_to_level(value) == expected

class TestMarketType:
    def test_match_result_value(self):
        assert MarketType.MATCH_RESULT.value == "1X2"

    def test_all_markets(self):
        assert len(list(MarketType)) == 6
