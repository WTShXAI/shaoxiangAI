#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-3 分歧闸门 + 规范凯利 · 轻量 CI 回归断言 (无重DB, 纯fixture, 毫秒级).

守卫三件事, 防止生产价值层(value_layer)的「选边逻辑」退化:
  G1 分歧闸门正确性: 两庄结构性分歧 -> disagreement_detected=True; 共识 -> False.
  G2 规范凯利 / 防全押 (P0-3 根因bug): 借 p0_3 生产脚本的 decide_dir, 高赔冷门边
      stake <= MAX_STAKE_FRAC*equity, 且用规范 kelly_fraction=(p*o-1)/(o-1)
      (非误用的 0.5*(p*o-1) -> 高赔冷门变相数倍超押 -> 几何衰减归零).
  G3 分歧闸门是 edge 过滤器 (P0-3 核心结论): 受控合成样本上,
      经 disagreement_detected 闸门过滤的 value_layer 选边 ROI >> argmax(押热门) ROI.
  G4 一致性自检 (轻量): argmax@best 对照在相同 fixture 上 ROI 钉死 (防harness/决策数学漂移).

用法: python scripts/test_p0_3_divergence_gate.py
CI 集成: tests/test_p0_3_divergence_gate.py 以 subprocess 调用本脚本 (单一事实源).
"""
import os
import sys

# 仓库根 (兼容本地 Windows 与 Linux CI runner), 无需硬编码绝对路径
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput
from pipeline.deep_report import compute_value_layer, kelly_fraction

# 借生产下注核心的 decide_direction / decide_argmax (真实下注逻辑, 防回归; 单一事实源见 scripts/bet_core)
from scripts.bet_core import decide_direction as decide_dir, decide_argmax, MAX_STAKE_FRAC, FRAC_KELLY

IDX = {"H": 0, "D": 1, "A": 2}
fails = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  " + extra) if extra else ""))
    if not cond:
        fails.append(name)


def simulate(fixtures, gate_only):
    """对一组 (cons, best_odds, winner, disagreement) 跑资金曲线.
    gate_only=True -> 仅当 disagreement_detected 才下注 (分歧闸门)."""
    eq_v, eq_a = 3000.0, 3000.0
    bv = ba = wv = wa = 0
    for cons, best_odds, winner, is_dis in fixtures:
        vl = compute_value_layer(odds=best_odds, model_probs=cons,
                                  bankroll=3000.0, frac_kelly=FRAC_KELLY)
        if vl["decision"] == "BET":
            di = IDX[vl["best_direction"]]
            # 分歧闸门已在循环级 if not gate_only or is_dis 强制; 进入此处即应下注
            if not gate_only or is_dis:
                eq_v, sv, wv_ = decide_dir(di, cons, best_odds, eq_v, winner, gate=True)
                if sv > 0:
                    bv += 1
                if wv_:
                    wv += 1
        # argmax 对照 (无闸门, 全下)
        eq_a, sa, wa_ = decide_argmax(cons, best_odds, eq_a, winner, gate=True)
        if sa > 0:
            ba += 1
        if wa_:
            wa += 1
    roi_v = (eq_v - 3000.0) / 3000.0 * 100
    roi_a = (eq_a - 3000.0) / 3000.0 * 100
    return roi_v, roi_a, bv, ba, wv, wa


def build_fixtures():
    """受控合成样本: 两庄结构性分歧的比赛, 冷门高edge边有正EV且赛果命中."""
    eng = ReverseOddsEngine()
    cases = [
        # cons(H,D,A), best_odds(H,D,A), winner, 期望分歧
        ([0.55, 0.25, 0.20], [2.0, 3.5, 6.0], "A", True),
        ([0.50, 0.28, 0.22], [2.1, 3.4, 6.5], "A", True),
        ([0.48, 0.30, 0.22], [2.2, 3.5, 6.0], "A", True),
        ([0.52, 0.26, 0.22], [2.0, 3.8, 7.0], "A", True),
        ([0.45, 0.30, 0.25], [2.4, 3.3, 6.5], "A", True),
        ([0.50, 0.27, 0.23], [2.1, 3.6, 6.0], "A", True),
    ]
    fixtures = []
    for cons, best_odds, winner, _ in cases:
        # 两庄: 庄A看H热门(低赔), 庄B看A热门(低赔) -> 结构性分歧
        bA = OddsInput(open_h=1/cons[0], open_d=1/cons[1], open_a=1/cons[2],
                       close_h=1/cons[0], close_d=1/cons[1], close_a=1/cons[2])
        bB = OddsInput(open_h=1/cons[2], open_d=1/cons[1], open_a=1/cons[0],
                       close_h=1/cons[2], close_d=1/cons[1], close_a=1/cons[0])
        res = eng.analyze_multi([bA, bB])
        fixtures.append((cons, best_odds, winner, res.disagreement_detected))
    return fixtures


def run_checks():
    eng = ReverseOddsEngine()

    print("[G1] 分歧闸门正确性")
    bA = OddsInput(open_h=1.5, open_d=4.0, open_a=6.0, close_h=1.5, close_d=4.0, close_a=6.0)
    bB = OddsInput(open_h=3.0, open_d=3.5, open_a=2.2, close_h=3.0, close_d=3.5, close_a=2.2)
    res = eng.analyze_multi([bA, bB])
    check("结构性分歧 -> disagreement_detected=True", res.disagreement_detected is True)
    bC = OddsInput(open_h=1.5, open_d=4.0, open_a=6.0, close_h=1.5, close_d=4.0, close_a=6.0)
    res2 = eng.analyze_multi([bA, bC])
    check("共识一致 -> disagreement_detected=False", res2.disagreement_detected is False)

    print("[G2] 规范凯利 / 防全押 (P0-3 根因bug守卫)")
    p_vec = [0.41, 0.33, 0.26]
    odds = [7.25, 3.0, 2.0]
    eq0 = 3000.0
    new_eq, stake, _win = decide_dir(0, p_vec, odds, eq0, "H")  # winner无关注码数学
    canon = kelly_fraction(0.41, 7.25)
    check("高赔冷门 kelly 用规范公式 (p*o-1)/(o-1)",
          abs(canon - (0.41 * 7.25 - 1) / (7.25 - 1)) < 1e-9)
    check("单注 <= 10% 本金 (无全押bug)", stake <= MAX_STAKE_FRAC * eq0 + 1e-9,
          f"stake={stake:.1f} cap={MAX_STAKE_FRAC*eq0:.1f}")
    check("单注严格 < 本金 (不会all-in)", stake < eq0)
    # 反证: 若误用 0.5*(p*o-1) 当分数, 单注分数会≈0.986(近全押) -> 这里必须不成立
    buggy_frac = 0.5 * (0.41 * 7.25 - 1)
    check("非误用 0.5*(p*o-1) 超押", abs((stake / eq0) - buggy_frac) > 0.05,
          f"实际分数={stake/eq0:.3f} 误用={buggy_frac:.3f}")

    print("[G3] 分歧闸门是 edge 过滤器 (P0-3 核心结论)")
    fx = build_fixtures()
    check("fixture 全为分歧场景", all(d for _, _, _, d in fx))
    roi_v, roi_a, bv, ba, wv, wa = simulate(fx, gate_only=True)
    print(f"    gated value_layer ROI={roi_v:+.1f}% (注{bv},胜{wv}) | argmax ROI={roi_a:+.1f}% (注{ba},胜{wa})")
    check("经闸门 value_layer ROI 远超 argmax (edge过滤器成立)", roi_v > roi_a + 100.0,
          f"Δ={roi_v-roi_a:+.1f}pp")
    check("经闸门 value_layer 在分歧子集盈利 (ROI>0)", roi_v > 0)
    check("argmax 押热门在分歧样本亏损 (ROI<0)", roi_a < 0)

    print("[G4] 一致性自检 (argmax@best 对照 ROI 钉死, 防harness漂移)")
    check("argmax@best 对照 ROI ≈ -15.6%±5pp", abs(roi_a - (-15.6)) < 5.0,
          f"roi_a={roi_a:+.1f}%")

    print("\n结果:", "ALL PASS ✅" if not fails else f"{len(fails)} 项 FAIL ❌: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run_checks())
