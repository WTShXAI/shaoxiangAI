#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定价行为特征引擎 (pricing_behavior_features.py)
从赔率 tick 序列提取庄家时序定价行为特征。
数据源无关: 电子盘(EAFC25 jsonl) 与 真实盘口(live_poll jsonl) 同构输入皆可。

设计原则:
- 这些特征是"庄家定价函数的行为指纹", 用于校准操盘手锚定 (unified_predictor),
  而非直接预测赛果 (避免 score 泄露 / 分布偏移陷阱)。
- 纯函数, 无副作用, 字段缺失时优雅返回 valid=False。

extract_behavior(ticks, main_line=None) -> dict
  ticks: list[dict], 每个含 ts_epoch, score, minute, markets
  返回行为特征 dict (见 RETURN SCHEMA)
"""
import re, statistics, glob, json, os

OU_RE = re.compile(r"^OU_(\d+(?:\.\d+)?)$")

def _get_1x2(mk):
    m = (mk or {}).get("1X2")
    if not isinstance(m, dict):
        return (None, None, None)
    for ks in (("h", "d", "a"), ("home", "draw", "away")):
        if all(k in m for k in ks):
            return (m[ks[0]], m[ks[1]], m[ks[2]])
    for v in m.values():
        if isinstance(v, dict):
            for ks in (("h", "d", "a"), ("home", "draw", "away")):
                if all(k in v for k in ks):
                    return (v[ks[0]], v[ks[1]], v[ks[2]])
    return (None, None, None)

def _get_ou(mk, line):
    v = (mk or {}).get("OU_%s" % line)
    if isinstance(v, dict):
        return (v.get("over"), v.get("under"))
    return (None, None)

def _parse_score(s):
    if not s or "-" not in str(s):
        return None
    try:
        a, b = str(s).split("-")
        return (int(a), int(b))
    except Exception:
        return None

def _slope(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den > 0 else 0.0

def extract_behavior(ticks, main_line=None):
    """从 tick 序列提取定价行为特征。
    返回 dict:
      valid: bool
      n: 有效 1X2 tick 数
      main_line: 选中的 OU 主线
      drift_h/d/a: 1X2 赔率 vs tick序号 的线性回归斜率 (负=下降=庄家看好)
      drift_consistency: 首末方向一致度 (1.0 三方同向 | 0.5 两方同向 | 0.0)
      decay_over/decay_under: OU 大/小 赔率 vs 墙钟秒 的斜率 (时间衰减)
      n_events: 进球重定价事件数
      reprice_asym: {H:{dh,dd,da}, A:{dh,dd,da}} 主/客进球后 1X2 赔率平均跳变
      suspend_count: markets 全空的 tick 数 (庄家撤挂)
      margin_1x2: 末 tick 1X2 overround
      vol_h/d/a: 1X2 赔率标准差 (波动)
    """
    seq = []
    for t in ticks:
        mk = t.get("markets") or {}
        h, d, a = _get_1x2(mk)
        if None in (h, d, a):
            continue
        seq.append({"i": len(seq), "ts": t.get("ts_epoch") or 0,
                    "h": float(h), "d": float(d), "a": float(a),
                    "sc": _parse_score(t.get("score")), "mn": t.get("minute"), "mk": mk})
    if len(seq) < 3:
        return {"valid": False, "n": len(seq), "reason": "<3 valid 1X2 ticks"}
    # OU 主线: 取全场出现最多的 OU 线
    if main_line is None:
        cnt = {}
        for s in seq:
            for k in s["mk"]:
                m = OU_RE.match(k)
                if m:
                    cnt[m.group(1)] = cnt.get(m.group(1), 0) + 1
        main_line = max(cnt, key=cnt.get) if cnt else None
    for s in seq:
        ov, un = _get_ou(s["mk"], main_line) if main_line else (None, None)
        s["ov"] = float(ov) if ov is not None else None
        s["un"] = float(un) if un is not None else None
    # drift 斜率
    drift_h = _slope([s["i"] for s in seq], [s["h"] for s in seq]) or 0.0
    drift_d = _slope([s["i"] for s in seq], [s["d"] for s in seq]) or 0.0
    drift_a = _slope([s["i"] for s in seq], [s["a"] for s in seq]) or 0.0
    # 首末方向一致性
    def sgn(a, b):
        if a is None or b is None:
            return 0
        return 1 if b < a else (-1 if b > a else 0)
    fh = sgn(seq[0]["h"], seq[-1]["h"])
    fd = sgn(seq[0]["d"], seq[-1]["d"])
    fa = sgn(seq[0]["a"], seq[-1]["a"])
    nz = sum(1 for x in (fh, fd, fa) if x != 0)
    drift_consistency = 1.0 if (fh and fh == fd == fa) else (0.5 if nz >= 2 else 0.0)
    # 时间衰减: 取"首个无进球段"(开球->首个进球前) OU 大/小 vs 墙钟秒 斜率,
    # 避免进球 reprice 跳变污染衰减信号 (真实盘口有进球时尤其重要)。
    split_idx = len(seq)
    prev = seq[0]["sc"]
    for idx, s in enumerate(seq):
        if prev is not None and s["sc"] is not None and s["sc"] != prev:
            split_idx = idx
            break
        if s["sc"] is not None:
            prev = s["sc"]
    seg = seq[:split_idx]
    pts_ov = [(s["ts"], s["ov"]) for s in seg if s["ov"] is not None]
    pts_un = [(s["ts"], s["un"]) for s in seg if s["un"] is not None]
    decay_ov = _slope([p[0] for p in pts_ov], [p[1] for p in pts_ov]) if len(pts_ov) >= 3 else None
    decay_un = _slope([p[0] for p in pts_un], [p[1] for p in pts_un]) if len(pts_un) >= 3 else None
    # 进球重定价事件
    events = []
    prev = None
    for idx, s in enumerate(seq):
        if prev is None:
            if s["sc"] is not None:
                prev = s["sc"]
            continue
        if s["sc"] is not None and s["sc"] != prev:
            who = None
            if s["sc"][0] > prev[0]:
                who = "H"
            elif s["sc"][1] > prev[1]:
                who = "A"
            if idx > 0:
                p = seq[idx - 1]
                events.append({"who": who, "tick": idx,
                               "dh": s["h"] - p["h"], "dd": s["d"] - p["d"], "da": s["a"] - p["a"],
                               "dov": (s["ov"] - p["ov"]) if (s["ov"] is not None and p["ov"] is not None) else None,
                               "dun": (s["un"] - p["un"]) if (s["un"] is not None and p["un"] is not None) else None})
            prev = s["sc"]
    h_ev = [e for e in events if e["who"] == "H"]
    a_ev = [e for e in events if e["who"] == "A"]
    def _avg(lst, key):
        vs = [e[key] for e in lst if e[key] is not None]
        return sum(vs) / len(vs) if vs else None
    reprice_asym = {"H": {"dh": _avg(h_ev, "dh"), "dd": _avg(h_ev, "dd"), "da": _avg(h_ev, "da")},
                    "A": {"dh": _avg(a_ev, "dh"), "dd": _avg(a_ev, "dd"), "da": _avg(a_ev, "da")}}
    suspend = sum(1 for t in ticks if not (t.get("markets") or {}))
    last = seq[-1]
    margin = (1 / last["h"] + 1 / last["d"] + 1 / last["a"]) - 1
    vol_h = statistics.pstdev([s["h"] for s in seq])
    vol_d = statistics.pstdev([s["d"] for s in seq])
    vol_a = statistics.pstdev([s["a"] for s in seq])
    return {
        "valid": True, "n": len(seq), "main_line": main_line,
        "drift_h": round(drift_h, 5), "drift_d": round(drift_d, 5), "drift_a": round(drift_a, 5),
        "drift_consistency": drift_consistency,
        "decay_over": round(decay_ov, 8) if decay_ov is not None else None,
        "decay_under": round(decay_un, 8) if decay_un is not None else None,
        "n_events": len(events), "reprice_asym": reprice_asym,
        "suspend_count": suspend,
        "margin_1x2": round(margin, 4),
        "vol_h": round(vol_h, 4), "vol_d": round(vol_d, 4), "vol_a": round(vol_a, 4),
    }

if __name__ == "__main__":
    print("=== 定价行为特征自测 (电子盘 9 场) ===")
    for p in sorted(glob.glob("data/electronic_poll_*.jsonl")):
        rows = []
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        pass
        b = extract_behavior(rows)
        mid = os.path.basename(p).replace("electronic_poll_", "").replace(".jsonl", "")
        if not b.get("valid"):
            print("  %s: 无效 (%s)" % (mid, b.get("reason")))
            continue
        ra = b["reprice_asym"]
        print("  %s: n=%d line=%s drift(h/d/a)=%.4f/%.4f/%.4f cons=%.1f "
              "decay(o/u)=%s/%s ev=%d susp=%d margin=%.3f"
              % (mid, b["n"], b["main_line"], b["drift_h"], b["drift_d"], b["drift_a"],
                 b["drift_consistency"], b["decay_over"], b["decay_under"],
                 b["n_events"], b["suspend_count"], b["margin_1x2"]))
        print("      reprice_asym H: dh=%.3f dd=%.3f da=%.3f | A: dh=%.3f dd=%.3f da=%.3f"
              % (ra["H"]["dh"] or 0, ra["H"]["dd"] or 0, ra["H"]["da"] or 0,
                 ra["A"]["dh"] or 0, ra["A"]["dd"] or 0, ra["A"]["da"] or 0))
