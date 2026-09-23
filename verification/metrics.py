"""指标聚合 — ROI±CI / 校准 / vs-market / 方向 / bundle (T03).

预测质量优先: LogLoss / Brier / ECE (相对同场集市场基线) 为首要成功指标;
ROI 仅作诚实对照。所有指标经 stats / calibration_bridge 复用 SSoT 计算。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from verification import stats as _stats
from verification import calibration_bridge as _bridge
from verification.constants import DIR_P0


@dataclass
class MetricsBundle:
    """单模型验证指标包; 是 gates.verdict 的唯一输入."""
    model_source: str
    n: int = 0
    roi_point: float = 0.0
    roi_ci_low: float = 0.0
    roi_ci_high: float = 0.0
    roi_method: str = "bootstrap"
    log_loss: Optional[float] = None
    brier: Optional[float] = None
    ece: Optional[float] = None
    slope: Optional[float] = None
    accuracy: Optional[float] = None
    vs_market_ll: Optional[float] = None        # 负值 = 模型优于市场 (同场集)
    direction_binomial_p: Optional[float] = None  # G5 方向二项检验 p 值
    # ── G6 零信息机械对照 (2026-09-23 新增) ──────────────────────────────
    # 动机: 比例法去水高估热门概率 → "无脑买最短赔率"这一零信息策略本身即可
    #   产出显著正 ROI (实测 KNN 同批 2562 场 +5.15%, CI下限>0), 使 G2/G3/G5
    #   被纯偏差驱动而误判 EDGE。G6 要求模型 ROI 相对该机械基准的配对差
    #   显著 > 0, 即证明"选边"本身有增量信息, 而非仅吃热门偏差。
    mech_fav_roi: Optional[float] = None          # 同批比赛"无脑买最短赔率"ROI
    paired_excess: Optional[float] = None         # 模型 ROI − 机械热门 ROI (配对)
    paired_excess_ci_low: Optional[float] = None  # 配对差 95%CI 下限 (G6 判据)


class Metrics:
    """指标计算静态工具集."""

    @staticmethod
    def roi(ledger_rows: List[dict], method: str = "bootstrap") -> dict:
        """纸盘 ROI 点估计 + 95% CI. 返回 {point, ci_low, ci_high, n, method}."""
        returns = [r["payoff"] for r in ledger_rows if r.get("payoff") is not None]
        point = _stats.roi_point(returns)
        if method == "t":
            lo, hi = _stats.roi_ci_t(returns)
        else:
            lo, hi = _stats.roi_ci_bootstrap(returns)
        return {
            "point": point,
            "ci_low": lo,
            "ci_high": hi,
            "n": len(returns),
            "method": method,
        }

    @staticmethod
    def calibration(rows_1x2: List[tuple]) -> Optional[dict]:
        """经 calibration_bridge → multi_metrics; 无概率行返回 None."""
        return _bridge.calibration_for_rows(rows_1x2)

    @staticmethod
    def accuracy_direction(verdicts: List[tuple]) -> float:
        """verdicts=[(predicted_outcome, actual)] → 方向准确率."""
        if not verdicts:
            return 0.0
        hits = sum(1 for pred, act in verdicts if pred == act)
        return hits / len(verdicts)

    @staticmethod
    def vs_market_ll(model_ll: Optional[float], base_ll: Optional[float]) -> float:
        """相对市场基线的 LogLoss 差值 (负 = 模型更优). 任一缺失返回 0.0."""
        if model_ll is None or base_ll is None:
            return 0.0
        return model_ll - base_ll

    @staticmethod
    def build_bundle(model_source: str, ledger_rows: List[dict],
                     cal_rows: List[tuple], base_metrics: Optional[dict]) -> MetricsBundle:
        """聚合单模型全部指标为 MetricsBundle.

        ledger_rows: 该 source 可信账本行 (含 payoff / chosen_outcome / settled_outcome /
                     p_home.. / devig_h..)
        cal_rows:    [(p_home, p_draw, p_away, actual)] 模型概率校准集 (KNN 为空)
        base_metrics: 同场集市场基线校准 dict (用于 vs_market_ll); market_baseline 自身传 None
        """
        roi = Metrics.roi(ledger_rows)
        bundle = MetricsBundle(
            model_source=model_source,
            n=roi["n"],
            roi_point=roi["point"],
            roi_ci_low=roi["ci_low"],
            roi_ci_high=roi["ci_high"],
            roi_method=roi["method"],
        )

        # 预测质量 (概率模型)
        cal = Metrics.calibration(cal_rows)
        if cal is not None:
            bundle.log_loss = cal.get("log_loss")
            bundle.brier = cal.get("brier_multiclass")
            per = cal.get("per_outcome") or {}
            eces = [per[k]["ece"] for k in ("home", "draw", "away")
                    if per.get(k) and per[k].get("ece") is not None]
            slopes = [per[k]["slope"] for k in ("home", "draw", "away")
                      if per.get(k) and per[k].get("slope") is not None]
            bundle.ece = float(sum(eces) / len(eces)) if eces else None
            bundle.slope = float(sum(slopes) / len(slopes)) if slopes else None
            bundle.accuracy = cal.get("accuracy")

        # 方向准确率 (概率模型 = 校准 TOP1; KNN/无概率 = chosen vs settled)
        verdicts = [
            (r["chosen_outcome"], r["settled_outcome"])
            for r in ledger_rows
            if r.get("chosen_outcome") and r.get("settled_outcome")
        ]
        direction_acc = Metrics.accuracy_direction(verdicts)
        if bundle.accuracy is None and verdicts:
            bundle.accuracy = direction_acc

        # 相对同场集市场基线的 LogLoss 差值。
        # 仅当模型确有 LogLoss 时才设置; KNN 无概率模型 → 留 None,
        # 使 G3 退化为 accuracy 分支 (符合设计 §4.4「KNN: 准确率>基线」)。
        if base_metrics is not None and bundle.log_loss is not None:
            bundle.vs_market_ll = Metrics.vs_market_ll(bundle.log_loss, base_metrics.get("log_loss"))

        # G5 方向二项检验 (1X2 零假设 = 1/3)
        if verdicts:
            k = round(direction_acc * len(verdicts))
            bundle.direction_binomial_p = _stats.binomial_p(k, len(verdicts), p0=DIR_P0)

        # G6 零信息机械对照: 同批比赛"无脑买最短 devig 赔率" vs 模型实际选边
        mech_pay: List[float] = []
        diffs: List[float] = []
        for r in ledger_rows:
            d = {"home": r.get("devig_h"), "draw": r.get("devig_d"), "away": r.get("devig_a")}
            act = r.get("settled_outcome")
            pay = r.get("payoff")
            if any(v is None for v in d.values()) or not act or pay is None:
                continue
            sh = min(d, key=lambda k: d[k])
            mech_pay.append((d[sh] - 1.0) if act == sh else -1.0)
            diffs.append(pay - mech_pay[-1])
        if mech_pay and diffs:
            bundle.mech_fav_roi = _stats.roi_point(mech_pay)
            bundle.paired_excess = _stats.roi_point(diffs)
            lo, _hi = _stats.roi_ci_bootstrap(diffs)
            bundle.paired_excess_ci_low = lo

        return bundle


__all__ = ["MetricsBundle", "Metrics"]
