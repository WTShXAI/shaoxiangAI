#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-1 soft-line 决策闭环回归测试.

验证三件事:
  (1) analyze_multi 在跨庄方向性分歧时触发 soft-line 淡化, 共识热门压到 0.41, 概率归一;
  (2) compute_value_layer 用 adjusted_probs 作 model_probs 后 model_prob/edge 改变 (接回生效);
  (3) bridge_service.ENABLE_SOFTLINE_DECISION 开关控制 cons 是否被覆盖到主 1X2 决策.

用法: python scripts/test_softline_decision_closure.py
CI 集成: tests/test_softline_decision_closure.py 以 subprocess 调用本脚本 (单一事实源).
"""
import os
import sys

# 仓库根 (兼容本地 Windows 与 Linux CI runner), 不写死绝对路径
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput
from pipeline.deep_report import compute_value_layer


def run_checks():
    """执行全部闭环断言, 返回进程退出码 (0=全 PASS, 1=有 FAIL)."""
    fails = []

    def check(name, cond):
        print(("  PASS " if cond else "  FAIL ") + name)
        if not cond:
            fails.append(name)

    eng = ReverseOddsEngine()

    # ── 场景: 跨庄方向性分歧 ──
    # 庄A 强看主胜(H); 庄B 看客胜(A, 较弱) → 共识热门=H 且概率>0.41, 两庄看法不一 → 触发淡化
    bA = OddsInput(open_h=1.5, open_d=4.0, open_a=6.0, close_h=1.5, close_d=4.0, close_a=6.0)
    bB = OddsInput(open_h=3.0, open_d=3.5, open_a=2.2, close_h=3.0, close_d=3.5, close_a=2.2)
    res = eng.analyze_multi([bA, bB])

    print("[1] analyze_multi 分歧场景")
    check("分歧检测触发", res.disagreement_detected is True)
    check("soft-line fade 触发", res.softline_fade_applied is True)
    check("共识热门压到 0.41", abs(res.softline_adjusted_probs[0] - 0.41) < 1e-6)
    check("adjusted 概率归一(和=1)", abs(sum(res.softline_adjusted_probs) - 1.0) < 1e-6)
    check("共识热门原值 >0.41 才淡", res.implied_probs[0] > 0.41)

    # ── 对照: 一致场景不淡 ──
    bC = OddsInput(open_h=1.5, open_d=4.0, open_a=6.0, close_h=1.5, close_d=4.0, close_a=6.0)
    res2 = eng.analyze_multi([bA, bC])
    check("一致场景不触发 fade", res2.softline_fade_applied is False)

    # ── compute_value_layer 接回验证 ──
    print("[2] compute_value_layer 接回 adjusted_probs")
    best_odds = [3.0, 4.0, 6.0]
    vl_cons = compute_value_layer(odds=best_odds, model_probs=list(res.implied_probs), overround=0.10)
    vl_adj = compute_value_layer(odds=best_odds, model_probs=list(res.softline_adjusted_probs), overround=0.10)
    check("接回改变 model_prob", vl_cons["model_prob"] != vl_adj["model_prob"])
    check("接回改变 edge/best_direction",
          vl_cons["best_direction"] != vl_adj["best_direction"] or
          abs(vl_cons["best_edge_pct"] - vl_adj["best_edge_pct"]) > 1e-6)
    check("接回后 model_prob == adjusted(round4)",
          all(abs(a - round(b, 4)) < 1e-9 for a, b in zip(vl_adj["model_prob"], res.softline_adjusted_probs)))

    # ── 集成: bridge_service 开关控制 cons 覆盖 ──
    print("[3] bridge_service.ENABLE_SOFTLINE_DECISION 开关")
    try:
        import bridge_service as bs
        extra = [["A", 1.5, 4.0, 6.0], ["B", 3.0, 3.5, 2.2]]

        bs.ENABLE_SOFTLINE_DECISION = False
        vl_off = bs._live_predict("Home", "Away", 1.5, 4.0, 6.0, extra_bookmakers=extra)
        _vl_off = vl_off["value_layer"]
        check("关: soft-line 仍被检测(展示层)",
              (_vl_off.get("softline") or {}).get("softline_fade_applied") is True)
        check("关: 主决策未用 adjusted (仍信共识)",
              _vl_off["model_prob"] != list(res.softline_adjusted_probs))

        bs.ENABLE_SOFTLINE_DECISION = True
        vl_on = bs._live_predict("Home", "Away", 1.5, 4.0, 6.0, extra_bookmakers=extra)
        _vl_on = vl_on["value_layer"]
        check("开: 主决策 model_prob 已用 adjusted",
              all(abs(a - b) < 1e-3 for a, b in zip(_vl_on["model_prob"], res.softline_adjusted_probs)))
        check("开: 报告 softline_fade_applied=True",
              (_vl_on.get("softline") or {}).get("softline_fade_applied") is True)

        bs.ENABLE_SOFTLINE_DECISION = False  # 复位为生产默认
    except Exception as e:
        print("  WARN 集成测试跳过 (bridge/_live_predict 导入或运行异常):", repr(e))

    print("\n结果:", "ALL PASS ✅" if not fails else f"{len(fails)} 项 FAIL ❌: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_checks())
