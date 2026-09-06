# -*- coding: utf-8 -*-
"""
test_inplay_calibration.py — IR-07/IR-19 回归测试

固化诊断报告《足球实时预测页面诊断报告》的 1-4/87' 用例：
  - 1-4 @87' 必须 → 客胜主导 (≥0.6)，修复"静态λ主胜77%"失真。
  - 0-0       行为不退化（后验≈静态，方向来自赛前1X2）。
  - λ一致性告警：原始静态λ严重背离比分时触发（不可信标记）。

运行: python scripts/test_inplay_calibration.py
"""
from __future__ import annotations

import sys

from analysis.inplay_calibration import (
    dynamic_team_lambda,
    simulate_inplay_1x2,
    lambda_consistency_flag,
    isotonic_calibrate_1x2,
)
from analysis.live_goal_probe import predict_fulltime_outcome


def _odds(home, draw, away, ou_total=2.5, over=2.0, under=1.8):
    return {
        '1X2__home': home, '1X2__draw': draw, '1X2__away': away,
        f'OU_{ou_total:.2f}__over': over, f'OU_{ou_total:.2f}__under': under,
    }


def main():
    fails = []

    # ── 用例1: 1-4 @87' (诊断根因) ──
    odds = _odds(1.30, 5.0, 11.0)  # 赛前主胜~77%
    r = predict_fulltime_outcome(odds, '1-4', 87, league='回归测试')
    print(f"[1-4@87'] direction={r['direction']} conf={r['confidence']} expected={r['expected_score']}")
    if r['direction'] != '客胜':
        fails.append(f"1-4@87' 方向应为客胜, 实际 {r['direction']}")
    if not (r['confidence'] or 0) >= 0.6:
        fails.append(f"1-4@87' 客胜置信应≥0.6, 实际 {r['confidence']}")
    if r['expected_score'] != '1-4':
        fails.append(f"1-4@87' 终场预期应为 1-4, 实际 {r['expected_score']}")

    # ── 用例2: 0-0 @20 (行为不退化) ──
    r0 = predict_fulltime_outcome(odds, '0-0', 20, league='回归测试')
    print(f"[0-0@20] direction={r0['direction']} conf={r0['confidence']} expected={r0['expected_score']}")
    if r0['direction'] != '主胜':
        fails.append(f"0-0@20 方向应=主胜(赛前77%), 实际 {r0['direction']}")
    if abs((r0['confidence'] or 0) - 0.73) > 0.05:
        fails.append(f"0-0@20 置信应≈0.73(赛前去水), 实际 {r0['confidence']}")

    # ── 用例3: 模块级 λ 一致性告警 (原始静态λ背离) ──
    # 客队已进4球但原始静态λ=0.37 → flag 应触发
    flag = lambda_consistency_flag(2.0, 0.37, 1, 4)
    print(f"[λ告警] away已进4但λ=0.37 → flag={flag}")
    if flag is None:
        fails.append("λ一致性告警应对'客队进4球但λ<0.5'触发")

    # ── 用例4: 模块级动态λ自检 (1-4@87') ──
    hp, ap, rem = dynamic_team_lambda(2.05, 0.37, 1, 4, 87)
    ph, pd, pa = simulate_inplay_1x2(hp, ap, 1, 4, 87)
    print(f"[动态λ] 1-4@87' → 主胜{ph*100:.1f}% 平{pd*100:.1f}% 客胜{pa*100:.1f}%")
    if pa < 0.6:
        fails.append(f"模块级 1-4@87' 客胜应≥0.6, 实际 {pa*100:.1f}%")

    # ── 用例5: 等渗校准回退语义 (IR-19) ──
    # (a) 真实 models 目录：不存在的联赛应命中全局默认校准（非恒等）
    ph2, pd2, pa2, note = isotonic_calibrate_1x2(0.5, 0.2, 0.3, league='不存在的联赛XYZ')
    if note is None or 'global_default' not in note:
        fails.append(f"不存在联赛应走全局默认校准, 实际 note={note}")
    # (b) 指向空目录：无任何校准文件 → 恒等回退
    import tempfile, os
    empty = tempfile.mkdtemp()
    ph3, pd3, pa3, note3 = isotonic_calibrate_1x2(0.5, 0.2, 0.3, league='x', calib_dir=empty)
    if (ph3, pd3, pa3) != (0.5, 0.2, 0.3) or note3 is not None:
        fails.append(f"空目录应恒等回退, 实际 {(ph3,pd3,pa3)} note={note3}")
    os.rmdir(empty)

    if fails:
        print("\n❌ 回归测试失败:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("\n✅ 全部回归用例通过 (IR-07 动态λ + IR-19 校准回退 生效)")


if __name__ == '__main__':
    main()
