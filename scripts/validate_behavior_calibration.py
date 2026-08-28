#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行为校准验证 (validate_behavior_calibration.py)

两种验证模式:
  [终盘模式]  early_frac=1.0 (默认)
    用整场 tick 的末态赔率作锚定 + 全程行为。终盘 anchor 已含赛果信息(泄露),
    仅用于证明校准层 (1) 安全不退化 (2) 在异常行为场有纠偏潜力。

  [早盘增量模式]  early_frac=0.3 (例)
    只取比赛**前 30%** 的 tick 作锚定(该时刻赔率) + 行为(到那时累积的信号),
    去预测**最终赛果**。这才是行为规则的真实价值点——
    在赛果远未确定、赔率尚未沉淀时, 行为校准能否比纯锚定多榨出识别力。

数据源: 电子盘 jsonl (有 score 的场为有效赛果)。真实盘口(live_poll)当前多为直播中,
        无最终赛果, 待 20+ 场完赛后切换 --source live 即可复跑。

输出: 基线(纯锚定) vs 校准(锚定+行为) 的 命中率 / Brier / logloss。
"""
import json
import glob
import os
import sys
import math

sys.path.insert(0, "scripts")
from pricing_behavior_features import extract_behavior, _get_1x2
from behavior_calibrator import calibrate

IDX = {"H": 0, "D": 1, "A": 2}


def parse_score(s):
    if not s or "-" not in str(s):
        return None
    try:
        a, b = str(s).split("-")
        return (int(a), int(b))
    except Exception:
        return None


def implied(h, d, a):
    inv = [1 / h, 1 / d, 1 / a]
    s = sum(inv)
    return [x / s for x in inv]


def _load(p):
    rows = []
    with open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
    return rows


def validate(files, early_frac=1.0, label=""):
    base_hit = cal_hit = n = 0
    brier_base = brier_cal = 0.0
    ll_base = ll_cal = 0.0
    chg_count = 0
    print("\n########## %s (early_frac=%.2f) ##########" % (label or "验证", early_frac))
    print("%-18s %-5s %-5s %-7s %s" % ("mid", "基线", "校准", "实际", "行为摘要"))
    for p in files:
        rows = _load(p)
        if len(rows) < 3:
            continue
        valid = [t for t in rows if _get_1x2(t.get("markets"))[0]]
        if not valid:
            continue
        # 锚定+行为源: 前 early_frac 比例的 tick
        k = max(3, int(len(valid) * early_frac))
        use = valid[:k]
        h1, d1, a1 = _get_1x2(use[-1]["markets"])
        p1 = implied(h1, d1, a1)
        # 最终赛果: 用整场末态 score (ground truth)
        sc = parse_score(valid[-1].get("score"))
        if not sc:
            continue
        n += 1
        actual = 0 if sc[0] > sc[1] else (2 if sc[1] > sc[0] else 1)
        pr1 = p1.index(max(p1))
        beh = extract_behavior(use)
        cal = calibrate({"H": p1[0], "D": p1[1], "A": p1[2]}, beh)
        prc_key = max(IDX, key=lambda kk: cal[kk])
        prc_idx = IDX[prc_key]
        if pr1 == actual:
            base_hit += 1
        if prc_idx == actual:
            cal_hit += 1
        ohe = [0, 0, 0]
        ohe[actual] = 1
        brier_base += (p1[0] - ohe[0]) ** 2 + (p1[1] - ohe[1]) ** 2 + (p1[2] - ohe[2]) ** 2
        brier_cal += (cal["H"] - ohe[0]) ** 2 + (cal["D"] - ohe[1]) ** 2 + (cal["A"] - ohe[2]) ** 2
        ll_base += -math.log(max(p1[actual], 1e-9))
        ll_cal += -math.log(max(cal[list(IDX.keys())[actual]], 1e-9))
        chg = " <<校准改判" if pr1 != prc_idx else ""
        if pr1 != prc_idx:
            chg_count += 1
        mid = os.path.basename(p).replace("electronic_poll_", "").replace(".jsonl", "")
        ds = beh.get("drift_consistency", 0)
        print("%-18s %-5s %-5s %-7s susp=%d cons=%.1f ev=%d%s"
              % (mid[:16], ["主", "平", "客"][pr1], ["主", "平", "客"][prc_idx],
                 "%d-%d" % sc, beh.get("suspend_count", 0), ds, beh.get("n_events", 0), chg))
    if n == 0:
        print("  无有效赛果场")
        return
    print("\n  有效赛果 %d 场 | 基线命中 %d/%d=%.1f%%  Brier=%.4f  logloss=%.4f"
          % (n, base_hit, n, base_hit / n * 100, brier_base / n, ll_base / n))
    print("  校准(锚定+行为) 命中 %d/%d=%.1f%%  Brier=%.4f  logloss=%.4f"
          % (cal_hit, n, cal_hit / n * 100, brier_cal / n, ll_cal / n))
    print("  ΔBrier=%.4f (负=校准更优)  Δlogloss=%.4f  改判场=%d"
          % (brier_cal / n - brier_base / n, ll_cal / n - ll_base / n, chg_count))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="行为校准验证 (终盘 / 早盘增量)")
    ap.add_argument("--source", default="electronic", choices=["electronic", "live"])
    ap.add_argument("--early-frac", type=float, default=1.0,
                    help="早盘增量比例: 0.3=只用前30%%tick预测终场; 1.0=终盘(默认)")
    ap.add_argument("--all-fracs", action="store_true", help="依次跑 1.0/0.6/0.3 对比")
    args = ap.parse_args()

    if args.source == "electronic":
        files = sorted(glob.glob("data/electronic_poll_*.jsonl"))
    else:
        files = sorted(glob.glob("data/live_poll_*.jsonl"))
    if not files:
        print("无数据文件")
        raise SystemExit(1)

    if args.all_fracs:
        for f in (1.0, 0.6, 0.3):
            validate(files, early_frac=f, label="电子盘·%d%%tick" % int(f * 100))
    else:
        validate(files, early_frac=args.early_frac,
                 label="电子盘·%s" % ("终盘" if args.early_frac >= 1.0 else "早盘%.0f%%" % (args.early_frac * 100)))
