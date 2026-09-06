# -*- coding: utf-8 -*-
"""
校准偏置层 walk-forward 回测
=============================
验证 apply_1x2_overlay / apply_ou_overlay 是否真正提升预测准确率。

1X2: master_dataset 31.5万行, train≤2022/test≥2023
  - 基线: argmax(imp_h/d/a) 方向准确率
  - 加偏置: argmax(overlay后的imp) 方向准确率
OU: events.db odds_snapshots OU_* JOIN match_outcomes, 14463场
  - 基线: over命中 (total>line)
  - 加偏置: 看 ≤1.75 的场偏置是否提升 over 命中率(校准度)
"""
import csv, sqlite3, re, sys, math
sys.path.insert(0, r"D:\Architecture")
from pipeline.calibration_overlay import apply_1x2_overlay, apply_ou_overlay

def demargin(oh, od, oa):
    o = 1/oh + 1/od + 1/oa
    return (1/oh)/o, (1/od)/o, (1/oa)/o

def backtest_1x2():
    rows = list(csv.DictReader(open(r"D:\Architecture\data\master_dataset.csv", encoding="utf-8-sig")))
    test = [r for r in rows if str(r.get("match_date","")) >= "2023"]
    print(f"=== 1X2 偏置回测 (test≥2023, {len(test)}场) ===")
    base_correct = over_correct = n = 0
    triggered = 0
    base_logloss = over_logloss = 0.0
    base_brier = over_brier = 0.0  # Brier score (校准度, 越低越好)
    # 触发子集单独统计(因全量被稀释)
    trig_base_correct = trig_over_correct = trig_n = 0
    trig_base_brier = trig_over_brier = 0.0
    rc_idx = {"0":0, "1":1, "2":2}
    for r in test:
        try:
            oh, od, oa = float(r["odds_home"]), float(r["odds_draw"]), float(r["odds_away"])
            rc = r["result_class"]
        except: continue
        ph, pd, pa = demargin(oh, od, oa)
        # 基线
        base_pred = ["0","1","2"][[ph,pd,pa].index(max(ph,pd,pa))]
        base_correct += (base_pred == rc)
        idx = rc_idx.get(rc, 0)
        actual = [0,0,0]; actual[idx] = 1
        base_logloss += -math.log(max(1e-6, [ph,pd,pa][idx]))
        base_brier += sum((a-p)**2 for a,p in zip(actual,[ph,pd,pa]))
        # 加偏置
        ph2, pd2, pa2, app = apply_1x2_overlay(ph, pd, pa, oh, od, oa)
        over_pred = ["0","1","2"][[ph2,pd2,pa2].index(max(ph2,pd2,pa2))]
        over_correct += (over_pred == rc)
        over_logloss += -math.log(max(1e-6, [ph2,pd2,pa2][idx]))
        over_brier += sum((a-p)**2 for a,p in zip(actual,[ph2,pd2,pa2]))
        if app:
            triggered += 1
            trig_base_correct += (base_pred == rc)
            trig_over_correct += (over_pred == rc)
            trig_base_brier += sum((a-p)**2 for a,p in zip(actual,[ph,pd,pa]))
            trig_over_brier += sum((a-p)**2 for a,p in zip(actual,[ph2,pd2,pa2]))
            trig_n += 1
        n += 1
    print(f"  全量方向准确率: 基线{base_correct/n:.4f} → 偏置{over_correct/n:.4f} ({(over_correct-base_correct)/n*100:+.2f}pp)")
    print(f"  全量Brier:      基线{base_brier/n:.4f} → 偏置{over_brier/n:.4f} ({(over_brier-base_brier)/n*100:+.3f}, 负=更好)")
    print(f"  偏置触发: {triggered}场 ({100*triggered//n}%)")
    if trig_n > 0:
        print(f"\n  ★触发子集({trig_n}场) — 校准才是偏置的价值:")
        print(f"    方向准确率: {trig_base_correct/trig_n:.4f} → {trig_over_correct/trig_n:.4f}")
        print(f"    Brier(校准): {trig_base_brier/trig_n:.4f} → {trig_over_brier/trig_n:.4f} ({(trig_over_brier-trig_base_brier)/trig_n*100:+.3f}, 负=更好)")
    return {"base_brier": base_brier/n, "overlay_brier": over_brier/n, "trig_n": trig_n}

def backtest_ou():
    c = sqlite3.connect(r"D:\Architecture\data\events.db"); c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT s.match_key, s.market, s.selection, s.odds, s.line,
               o.score_home, o.score_away,
               ROW_NUMBER() OVER (PARTITION BY s.match_key, s.market, s.selection ORDER BY s.captured_at) as rn
        FROM odds_snapshots s
        INNER JOIN matches m ON s.match_key = m.match_key
        INNER JOIN match_outcomes o ON o.mid = m.mid
        WHERE s.market GLOB 'OU_[0-9]*' AND s.selection IN ('over','under')
          AND o.score_home IS NOT NULL
    """).fetchall()
    # 每场每盘口取初盘 over/under 赔率 + 真实总进球
    from collections import defaultdict
    mk_line = defaultdict(lambda: {"over":None,"under":None,"total":None,"line":None})
    for r in rows:
        # 列序: 0=match_key 1=market 2=selection 3=odds 4=line 5=score_home 6=score_away 7=rn
        if r[7] != 1: continue
        mk = r[0]
        market = r[1]
        m = re.match(r'OU_([0-9.]+)', market)
        if not m: continue
        line = float(m.group(1))
        k = (mk, line)
        mk_line[k]["line"] = line
        mk_line[k]["total"] = r[5] + r[6]   # score_home + score_away
        mk_line[k][r[2]] = r[3]             # selection -> odds
    valid = [(k,v) for k,v in mk_line.items() if v["over"] and v["under"] and v["total"] is not None]
    print(f"\n=== OU 偏置回测 ({len(valid)}场) ===")
    base_correct = over_correct = n = triggered = 0
    base_brier = over_brier = 0.0
    trig_base_brier = trig_over_brier = trig_n = 0
    for k, v in valid:
        oo, uu = float(v["over"]), float(v["under"])
        total, line = v["total"], v["line"]
        actual_over = 1.0 if total > line else 0.0
        s = 1/oo + 1/uu
        p_over = (1/oo)/s; p_under = (1/uu)/s
        base_correct += ((p_over > p_under) == (actual_over > 0.5))
        base_brier += (p_over - actual_over)**2
        po2, pu2, app = apply_ou_overlay(p_over, p_under, oo, uu)
        if app:
            triggered += 1
            trig_base_brier += (p_over - actual_over)**2
            trig_over_brier += (po2 - actual_over)**2
            trig_n += 1
        over_correct += ((po2 > pu2) == (actual_over > 0.5))
        over_brier += (po2 - actual_over)**2
        n += 1
    print(f"  全量方向准确率: 基线{base_correct/n:.4f} → 偏置{over_correct/n:.4f} ({(over_correct-base_correct)/n*100:+.2f}pp)")
    print(f"  全量Brier:      基线{base_brier/n:.4f} → 偏置{over_brier/n:.4f} ({(over_brier-base_brier)/n*100:+.3f})")
    print(f"  偏置触发: {triggered}场 ({100*triggered//n}%)")
    if trig_n > 0:
        print(f"\n  ★触发子集({trig_n}场) Brier: {trig_base_brier/trig_n:.4f} → {trig_over_brier/trig_n:.4f} ({(trig_over_brier-trig_base_brier)/trig_n*100:+.3f})")
    c.close()
    return {"base_brier": base_brier/n, "overlay_brier": over_brier/n, "trig_n": trig_n}

if __name__ == "__main__":
    r1 = backtest_1x2()
    r2 = backtest_ou()
    print(f"\n=== 总结 (Brier越低=校准越好) ===")
    print(f"  1X2: {r1['base_brier']:.4f} → {r1['overlay_brier']:.4f}")
    print(f"  OU:  {r2['base_brier']:.4f} → {r2['overlay_brier']:.4f}")
