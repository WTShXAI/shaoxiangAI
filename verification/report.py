"""报告渲染 + 落盘 (T04).

编排 Gates + Metrics: 从账本取数 → 聚合 bundle → 过门禁 → 渲染 JSON/Markdown/CLI。
每份报告必附 DISCLAIMER (不喊单、仅解释概率偏差)。
生成文本前对最终字符串跑 assert_clean (IR-32 双保险)。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from verification import ledger as _ledger
from verification import metrics as _metrics
from verification import gates as _gates
from verification import calibration_bridge as _bridge
from verification.constants import DISCLAIMER, MODEL_SOURCES, MIN_SAMPLE
from verification._ir32 import assert_clean

REPORT_DIR: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports"
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt(x, nd: int = 4):
    return f"{x:.{nd}f}" if isinstance(x, (int, float)) else "—"


def _build_bundles(led: "_ledger.VerificationLedger") -> Dict[str, "_metrics.MetricsBundle"]:
    """从账本聚合各 source 的 MetricsBundle.

    同场集市场基线: 用该 source 账本行的 devig_h/d/a (市场去水概率) 作基线对照,
    使 vs_market_ll 为严格同场集比较 (而非跨源近似)。market_baseline 自身不比对。
    """
    bundles: Dict[str, "_metrics.MetricsBundle"] = {}
    for src in MODEL_SOURCES:
        rows = led.fetch_credible(src)
        if not rows:
            continue
        # 模型概率校准集 (KNN 为空 → 仅方向口径)
        cal_rows = [
            (r["p_home"], r["p_draw"], r["p_away"], r["settled_outcome"])
            for r in rows if r.get("p_home") is not None
        ]
        # 同场集市场基线校准集 (devig_h/d/a 为 decimal odds → 换算为去水概率)
        base_rows = [
            (1.0 / r["devig_h"], 1.0 / r["devig_d"], 1.0 / r["devig_a"], r["settled_outcome"])
            for r in rows if r.get("devig_h") is not None
        ]
        base_metrics = _bridge.calibration_for_rows(base_rows) if base_rows else None
        bundles[src] = _metrics.Metrics.build_bundle(src, rows, cal_rows, base_metrics)
    return bundles


def render_json(bundles: dict) -> dict:
    """渲染 JSON 结构 (含三态结论 + 免责声明)."""
    g = _gates.Gates()
    models: Dict[str, dict] = {}
    for src, bundle in bundles.items():
        verdict, reasons = g.verdict(bundle)
        models[src] = {
            "matches": bundle.n,
            "roi": round(bundle.roi_point, 4),
            "ci_low": round(bundle.roi_ci_low, 4),
            "ci_high": round(bundle.roi_ci_high, 4),
            "roi_method": bundle.roi_method,
            "log_loss": round(bundle.log_loss, 5) if bundle.log_loss is not None else None,
            "brier": round(bundle.brier, 5) if bundle.brier is not None else None,
            "ece": round(bundle.ece, 5) if bundle.ece is not None else None,
            "slope": round(bundle.slope, 4) if bundle.slope is not None else None,
            "accuracy": round(bundle.accuracy, 4) if bundle.accuracy is not None else None,
            "vs_market_ll": round(bundle.vs_market_ll, 5)
            if bundle.vs_market_ll is not None else None,
            "direction_binomial_p": round(bundle.direction_binomial_p, 5)
            if bundle.direction_binomial_p is not None else None,
            "verdict": verdict,
            "reasons": reasons,
        }
        # 诚实标注: 方向性 EDGE 但无概率校准 (无 LogLoss) 时, 显式提示
        # (如 KNN 仅方向+二项式检验, 非概率校准型 edge)。
        if verdict == "EDGE" and bundle.log_loss is None:
            models[src]["edge_caveat"] = (
                "方向性 EDGE（无 LogLoss 概率校准，仅方向+二项式检验，非概率校准型 edge）"
            )
    out = {
        "generated_at": _now_utc(),
        "min_sample": MIN_SAMPLE,
        "disclaimer": DISCLAIMER,
        "models": models,
    }
    assert_clean(json.dumps(out, ensure_ascii=False))
    return out


def render_markdown(bundles: dict) -> str:
    """渲染 Markdown 表格 + 逐模型结论. 文本经 assert_clean 双保险."""
    g = _gates.Gates()
    lines: List[str] = [
        "# 盈利验证台周期报告",
        "",
        f"> {DISCLAIMER}",
        "",
        f"验收线: 每模型样本 ≥ {MIN_SAMPLE} 场 | 生成时间: {_now_utc()}",
        "",
        "| model_source | matches | ROI | CI_low | CI_high | LogLoss | Brier | ECE | "
        "vs_market_LL | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for src, bundle in bundles.items():
        verdict, _ = g.verdict(bundle)
        lines.append(
            f"| {src} | {bundle.n} | {_fmt(bundle.roi_point)} | {_fmt(bundle.roi_ci_low)} | "
            f"{_fmt(bundle.roi_ci_high)} | {_fmt(bundle.log_loss, 5)} | {_fmt(bundle.brier, 5)} | "
            f"{_fmt(bundle.ece, 5)} | {_fmt(bundle.vs_market_ll, 5)} | {verdict} |"
        )
    lines.append("")
    lines.append("## 逐模型结论")
    for src, bundle in bundles.items():
        verdict, reasons = g.verdict(bundle)
        lines.append(f"### {src}: {verdict}")
        for r in reasons:
            lines.append(f"- {r}")
        lines.append("")
    lines.append(f"> {DISCLAIMER}")
    text = "\n".join(lines)
    assert_clean(text)
    return text


def print_cli(bundles: dict) -> None:
    """控制台表格输出."""
    g = _gates.Gates()
    print("\n=== 盈利验证台 (walk-forward) ===")
    print(f"验收线: 每模型样本 ≥ {MIN_SAMPLE} 场 | {DISCLAIMER}\n")
    for src, bundle in bundles.items():
        verdict, reasons = g.verdict(bundle)
        print(f"[{src}] 样本={bundle.n}  "
              f"ROI={bundle.roi_point:+.4f} "
              f"(95%CI [{bundle.roi_ci_low:+.4f},{bundle.roi_ci_high:+.4f}])")
        print(f"       LogLoss={bundle.log_loss}  Brier={bundle.brier}  "
              f"ECE={bundle.ece}  vs_market_LL={bundle.vs_market_ll}")
        print(f"       结论: {verdict}")
        for r in reasons:
            print(f"         - {r}")
    print(f"\n{DISCLAIMER}")


def write_reports(out_dir: str, bundles: dict) -> Tuple[str, str]:
    """落盘 JSON + Markdown; 返回 (json_path, md_path)."""
    os.makedirs(out_dir, exist_ok=True)
    jp = os.path.join(out_dir, "verification_report.json")
    mp = os.path.join(out_dir, "verification_report.md")
    j = render_json(bundles)
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(j, f, ensure_ascii=False, indent=2)
    m = render_markdown(bundles)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(m)
    return (jp, mp)


__all__ = [
    "REPORT_DIR",
    "render_json",
    "render_markdown",
    "print_cli",
    "write_reports",
    "_build_bundles",
]
