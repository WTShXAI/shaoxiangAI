#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行为校准器 (behavior_calibrator.py)
把"定价行为特征"映射到操盘手锚定 (unified_predictor) 的置信度修正。

集成方式 (零侵入 / 优雅降级):
  predictor = UnifiedPredictor()
  res = predictor.predict(home, away, oh, od, oa, ...)   # 现有锚定逻辑不动
  anchor = res["anchor_probabilities"]                    # 去水锚定概率 {H,D,A}
  behavior = extract_behavior(tick_seq)                  # 本模块依赖特征引擎
  calibrated = calibrate(anchor, behavior)               # 行为校准层
  # calibrated 作为"带行为上下文的锚定概率"使用; behavior 无效时恒等返回 anchor

设计原则:
  - 纯函数, 无副作用; behavior 无效 -> 返回 anchor 原样 (零副作用)
  - 乘性 logit 调整, 各规则带硬上限, 避免极端偏移
  - 规则来源: 电子盘提炼的庄家定价行为指纹 (时间衰减/drift/reprice不对称/suspend)
"""
import math

try:
    from pricing_behavior_features import extract_behavior
except Exception:
    extract_behavior = None


def calibrate(anchor, behavior, verbose=False):
    """anchor: {H,D,A} 去水锚定概率 (和=1)
    behavior: extract_behavior() 输出
    返回 {H,D,A} 校准后概率 (和=1)。零副作用: behavior 无效时返回原 anchor。"""
    if not anchor:
        return anchor
    if not behavior or not behavior.get("valid"):
        return dict(anchor)
    H = anchor.get("H", 0) or 0
    D = anchor.get("D", 0) or 0
    A = anchor.get("A", 0) or 0
    if H + D + A <= 0:
        return dict(anchor)
    lp = [math.log(max(x, 1e-9)) for x in (H, D, A)]
    notes = []
    # 1) drift 看好方: 赔率下降最多的一方 (drift 最小/最负)
    drifts = [behavior.get("drift_h", 0) or 0,
              behavior.get("drift_d", 0) or 0,
              behavior.get("drift_a", 0) or 0]
    best = min(range(3), key=lambda i: drifts[i])
    if behavior.get("drift_consistency", 0) >= 0.5 and drifts[best] < 0:
        k = min(0.30, -drifts[best] * 2.0)  # 赔率每降 0.5 单位 +0.30 封顶
        lp[best] += k
        notes.append("drift看好%s +%.3f" % ("HDA"[best], k))
    # 2) reprice 异常: 某方进球后该方赔率反升(应降) -> 谨慎, 减该方置信
    asym = behavior.get("reprice_asym") or {}
    if asym.get("H", {}).get("dh") is not None and asym["H"]["dh"] > 0:
        lp[0] -= 0.20
        notes.append("主进主胜反升→谨慎-0.20")
    if asym.get("A", {}).get("da") is not None and asym["A"]["da"] > 0:
        lp[2] -= 0.20
        notes.append("客进客胜反升→谨慎-0.20")
    # 3) suspend 频繁 -> 庄家可能知情, 拉向均匀
    sc = behavior.get("suspend_count", 0) or 0
    if sc > 0:
        pen = min(0.40, sc * 0.02)
        lp = [x - pen for x in lp]
        notes.append("suspend×%d→拉均-%.3f" % (sc, pen))
    mx = max(lp)
    ex = [math.exp(x - mx) for x in lp]
    s = sum(ex)
    out = {"H": ex[0] / s, "D": ex[1] / s, "A": ex[2] / s}
    if verbose:
        out["_notes"] = notes
    return out


def apply_to_predictor(predictor, home, away, odds_h, odds_d, odds_a, tick_seq, **kw):
    """演示如何接 unified_predictor (不修改其核心)。
    返回 {anchor_probabilities, calibrated_probabilities, behavior}。"""
    if extract_behavior is None:
        raise RuntimeError("pricing_behavior_features 未就绪")
    res = predictor.predict(home, away, odds_h, odds_d, odds_a, **kw)
    anchor = res.get("anchor_probabilities") or {"H": 0, "D": 0, "A": 0}
    behavior = extract_behavior(tick_seq)
    calibrated = calibrate(anchor, behavior)
    return {"anchor_probabilities": anchor,
            "calibrated_probabilities": calibrated,
            "behavior": behavior}


if __name__ == "__main__":
    import json, glob, os
    # 单场演示: 电子盘最大场 (德国 vs 科特迪瓦), 用初盘去水概率作 anchor + 全程行为校准
    p = sorted(glob.glob("data/electronic_poll_*.jsonl"))[-1]
    rows = []
    with open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
    mid = os.path.basename(p).replace("electronic_poll_", "").replace(".jsonl", "")
    # 初盘去水
    def _imp(h, d, a):
        inv = [1 / h, 1 / d, 1 / a]
        s = sum(inv)
        return [x / s for x in inv]
    first = next((r for r in rows if (r.get("markets") or {}).get("1X2")), None)
    if first:
        m = first["markets"]["1X2"]
        k = "h" if "h" in m else ("home" if "home" in m else None)
        if k:
            h0, d0, a0 = m[k], m["d" if k == "h" else "draw"], m["a" if k == "h" else "away"]
            anchor = {"H": _imp(h0, d0, a0)[0], "D": _imp(h0, d0, a0)[1], "A": _imp(h0, d0, a0)[2]}
            beh = extract_behavior(rows)
            cal = calibrate(anchor, beh, verbose=True)
            print("场次 mid:", mid)
            print("初盘 anchor : H=%.3f D=%.3f A=%.3f" % (anchor["H"], anchor["D"], anchor["A"]))
            print("行为校准后  : H=%.3f D=%.3f A=%.3f" % (cal["H"], cal["D"], cal["A"]))
            print("调整说明    :", cal.get("_notes", []))
            print("行为特征    : drift(h/d/a)=%.4f/%.4f/%.4f cons=%.1f susp=%d margin=%.3f"
                  % (beh["drift_h"], beh["drift_d"], beh["drift_a"], beh["drift_consistency"],
                     beh["suspend_count"], beh["margin_1x2"]))
