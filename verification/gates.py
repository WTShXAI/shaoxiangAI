"""五道门禁 + 三态判定 (T04).

门禁为硬约束, 不可被前端/API/报告旁路:
  G1 样本量:       n >= MIN_SAMPLE (2500)
  G2 ROI 95%CI:    ci_low > 0
  G3 预测质量:     vs_market_ll < 0 (模型LL更优) 或 方向准确率 > 随机(1/3)
  G4 校准:         ECE <= ECE_MAX 且 斜率 ∈ [SLOPE_LO, SLOPE_HI] (缺字段视为未违反)
  G5 方向二项:     direction_binomial_p < CI_ALPHA

verdict() 是唯一允许产出 EDGE / NO EDGE / INCONCLUSIVE 的函数。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from verification.constants import (
    MIN_SAMPLE,
    CI_ALPHA,
    ECE_MAX,
    SLOPE_LO,
    SLOPE_HI,
    DIR_P0,
)
from verification import stats as _stats
from verification import metrics as _metrics


class Gates:
    def __init__(self, min_sample: int = MIN_SAMPLE, ci_alpha: float = CI_ALPHA,
                 ece_max: float = ECE_MAX, slope_lo: float = SLOPE_LO,
                 slope_hi: float = SLOPE_HI, dir_p0: float = DIR_P0) -> None:
        self.min_sample = min_sample
        self.ci_alpha = ci_alpha
        self.ece_max = ece_max
        self.slope_lo = slope_lo
        self.slope_hi = slope_hi
        self.dir_p0 = dir_p0

    def check(self, bundle: "_metrics.MetricsBundle") -> Dict[str, dict]:
        """逐道返回 {gate: {passed, detail}}."""
        n = bundle.n

        # G1 样本量
        g1_pass = n >= self.min_sample

        # G2 ROI 95% CI 下限 > 0
        g2_pass = (bundle.roi_ci_low is not None) and (bundle.roi_ci_low > 0.0)

        # G3 预测质量优于基线
        if bundle.vs_market_ll is not None:
            g3_pass = bundle.vs_market_ll < 0.0
        elif bundle.accuracy is not None:
            g3_pass = bundle.accuracy > self.dir_p0
        else:
            g3_pass = False

        # G4 校准 (缺字段视为未违反)
        g4_pass = True
        if bundle.ece is not None:
            g4_pass = g4_pass and (bundle.ece <= self.ece_max)
        if bundle.slope is not None:
            g4_pass = g4_pass and (self.slope_lo <= bundle.slope <= self.slope_hi)

        # G5 方向二项检验显著
        g5_pass = (bundle.direction_binomial_p is not None) and \
                  (bundle.direction_binomial_p < self.ci_alpha)

        # G6 零信息机械对照 (2026-09-23 新增, 最高优先级判据)
        # 要求模型 ROI 相对"同批比赛无脑买最短赔率"的配对差 CI 下限 > 0。
        # 拦截去水偏差 + 热门倾向制造的伪 EDGE。缺失视为不通过 (无法证明超额)。
        g6_pass = (bundle.paired_excess_ci_low is not None) and \
                  (bundle.paired_excess_ci_low > 0.0)

        return {
            "G1_sample": {
                "passed": bool(g1_pass),
                "detail": f"n={n} >= {self.min_sample}",
            },
            "G2_roi_ci": {
                "passed": bool(g2_pass),
                "detail": f"ci_low={bundle.roi_ci_low:.4f} > 0",
            },
            "G3_quality": {
                "passed": bool(g3_pass),
                "detail": f"vs_market_ll={bundle.vs_market_ll}; acc={bundle.accuracy}",
            },
            "G4_calib": {
                "passed": bool(g4_pass),
                "detail": (f"ece={bundle.ece} <= {self.ece_max}; "
                           f"slope={bundle.slope} in [{self.slope_lo},{self.slope_hi}]"),
            },
            "G5_dir_binom": {
                "passed": bool(g5_pass),
                "detail": f"p={bundle.direction_binomial_p}",
            },
            "G6_mech_control": {
                "passed": bool(g6_pass),
                "detail": (
                    f"配对超额={bundle.paired_excess}; ci_low={bundle.paired_excess_ci_low} > 0 "
                    f"(机械买热门 ROI={bundle.mech_fav_roi})"
                ),
            },
        }

    def verdict(self, bundle: "_metrics.MetricsBundle") -> Tuple[str, List[str]]:
        """返回 ('EDGE'|'NO EDGE'|'INCONCLUSIVE', reasons[]). 三态唯一出口."""
        gates = self.check(bundle)
        reasons: List[str] = []

        # 样本不足 → 拒绝任何盈利声称
        if not gates["G1_sample"]["passed"]:
            reasons.append(
                f"样本不足 (n={bundle.n} < {self.min_sample})，拒绝任何盈利声称，仅陈述现状"
            )
            return ("INCONCLUSIVE", reasons)

        g2 = gates["G2_roi_ci"]["passed"]
        g3 = gates["G3_quality"]["passed"]
        g4 = gates["G4_calib"]["passed"]
        g5 = gates["G5_dir_binom"]["passed"]
        g6 = gates["G6_mech_control"]["passed"]

        # 样本充足 + 全部门禁通过 (含 G6 机械对照) → EDGE
        if g2 and g3 and g4 and g5 and g6:
            reasons.append(
                "样本充足 + ROI CI下限>0 + 预测质量优于基线 + 校准达标 + 方向二项显著 "
                "+ 相对零信息'买热门'基准仍有显著超额 → 可证伪的统计边缘成立"
            )
            return ("EDGE", reasons)

        # 样本充足但 ROI CI 下限≤0 / 质量不优 / 无机械对照超额 → NO EDGE
        if (not g2) or (not g3) or (not g6):
            if not g2:
                reasons.append(
                    f"ROI 95% CI 下限 ≤ 0 (ci_low={bundle.roi_ci_low:.4f})，无统计边缘"
                )
            if not g3:
                reasons.append("预测质量未优于市场基线")
            if not g6:
                reasons.append(
                    "相对零信息'无脑买最短赔率'机械基准无显著超额 "
                    f"(配对差 {bundle.paired_excess}, CI下限 {bundle.paired_excess_ci_low} ≤ 0) "
                    "→ 表观 ROI 由去水偏差/热门倾向驱动，判为伪影"
                )
            return ("NO EDGE", reasons)

        # 其余 (样本足、ROI/质量达标但显著性不足) → INCONCLUSIVE
        if not gates["G5_dir_binom"]["passed"]:
            reasons.append(
                f"方向二项检验不显著 (p={bundle.direction_binomial_p})，证据不足"
            )
        if not gates["G4_calib"]["passed"]:
            reasons.append("校准未达标 (ECE/斜率越界)")
        reasons.append("样本充足但显著性证据不足，仅陈述现状")
        return ("INCONCLUSIVE", reasons)


__all__ = ["Gates"]
