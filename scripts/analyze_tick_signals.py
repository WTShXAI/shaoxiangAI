# -*- coding: utf-8 -*-
"""
tick 信号完整分析 (修正白皮书 v1.0)
====================================
白皮书只做了主胜1.0-1.49区间的.02/.04, 且只用赔率分布(无赛果)。
本脚本用 master_dataset 31.5万行赛果数据, 做全尾数×主/客胜×多区间×显著性的完整分析。

输出:
  data/tick_signal_full_report.json  — 完整统计表(全尾数/区间/方向/显著性)
  data/tick_features.csv             — 每场比赛的tick特征(可入库)
"""
import csv, json, math
from collections import defaultdict

IN = r"D:\Architecture\data\master_dataset.csv"
OUT_REPORT = r"D:\Architecture\data\tick_signal_full_report.json"
OUT_FEAT = r"D:\Architecture\data\tick_features.csv"

def tick_of(o, lo, hi):
    """赔率o在[lo,hi)区间的尾数(百分位0-9)。不在区间返回None。"""
    try:
        o = float(o)
        if o < lo or o >= hi: return None
        return int(round((o * 100) % 10)) % 10
    except:
        return None

def binom_p_two_sided(k, n, p=0.5):
    """大样本正态近似二项p值(双侧)。"""
    if n == 0: return 1.0
    z = (k/n - p) / math.sqrt(p*(1-p)/n)
    return math.erfc(abs(z)/math.sqrt(2))

def analyze_side(rows, odds_col, result_val, side_name):
    """分析某方赔率尾数 vs 该方是否赢。返回各区间×尾数的统计。"""
    # 区间: 1.0-1.49(0.01刻度) / 1.5-1.99(混合) / 2.0-2.99(0.05刻度,看末位0/5)
    regions = [(1.0,1.5,"1.0-1.49_0.01刻度"), (1.5,2.0,"1.5-1.99_混合"), (2.0,3.0,"2.0-2.99_0.05刻度")]
    out = {}
    for lo, hi, rname in regions:
        by_tick = defaultdict(lambda: {"win":0, "lose":0})
        for r in rows:
            t = tick_of(r.get(odds_col), lo, hi)
            if t is None: continue
            is_win = r.get("result_class") == result_val
            by_tick[t]["win" if is_win else "lose"] += 1
        total = sum(v["win"]+v["lose"] for v in by_tick.values())
        base = sum(v["win"] for v in by_tick.values()) / total if total else 0
        ticks = {}
        for t in sorted(by_tick):
            d = by_tick[t]; n = d["win"]+d["lose"]
            rate = d["win"]/n if n else 0
            lift = rate - base
            p = binom_p_two_sided(d["win"], n, base) if n > 30 else 1.0
            # 信号标注
            sig = ""
            if n > 100 and p < 0.01:
                sig = "⚠️陷阱" if lift < -0.03 else ("✓强信号" if lift > 0.03 else "")
            ticks[f".{t}"] = {"n": n, "win_rate": round(rate,4), "lift_pp": round(lift*100,1),
                              "p_value": round(p,6), "signal": sig}
        out[rname] = {"base_rate": round(base,4), "total": total, "ticks": ticks}
    return out

def main():
    rows = list(csv.DictReader(open(IN, encoding="utf-8-sig")))
    print(f"样本: {len(rows)}")

    report = {
        "说明": "tick信号完整分析(修正白皮书v1.0): 全尾数×主/客胜×多区间×赛果显著性",
        "数据源": "master_dataset.csv 31.5万行(含1X2收盘赔率+result_class赛果)",
        "样本量": len(rows),
    }
    # 主胜
    report["主胜赔率tick"] = analyze_side(rows, "odds_home", "0", "主胜")
    # 客胜
    report["客胜赔率tick"] = analyze_side(rows, "odds_away", "2", "客胜")

    # 跨方向一致性: 主胜.4陷阱 vs 客胜.4陷阱
    consistency = {}
    for rname in ["1.0-1.49_0.01刻度"]:
        h = report["主胜赔率tick"][rname]["ticks"]
        a = report["客胜赔率tick"][rname]["ticks"]
        for tk in h:
            if tk in a and h[tk]["n"]>100 and a[tk]["n"]>100:
                consistency[tk] = {"主胜lift": h[tk]["lift_pp"], "客胜lift": a[tk]["lift_pp"],
                                   "一致": (h[tk]["lift_pp"]>0)==(a[tk]["lift_pp"]>0)}
    report["主客一致性(1.0-1.49)"] = consistency

    json.dump(report, open(OUT_REPORT,"w",encoding="utf-8"), ensure_ascii=False, indent=2)

    # 打印关键结论
    print("\n=== 主胜赔率 1.0-1.49 区 (基线", report['主胜赔率tick']['1.0-1.49_0.01刻度']['base_rate'], ") ===")
    for tk, d in report['主胜赔率tick']['1.0-1.49_0.01刻度']['ticks'].items():
        if d['n']>100:
            print(f"  {tk}: n={d['n']:>5} 赢率={d['win_rate']:.3f} lift={d['lift_pp']:+.1f}pp p={d['p_value']:.4f} {d['signal']}")
    print("\n=== 客胜赔率 1.0-1.49 区 (基线", report['客胜赔率tick']['1.0-1.49_0.01刻度']['base_rate'], ") ===")
    for tk, d in report['客胜赔率tick']['1.0-1.49_0.01刻度']['ticks'].items():
        if d['n']>100:
            print(f"  {tk}: n={d['n']:>5} 赢率={d['win_rate']:.3f} lift={d['lift_pp']:+.1f}pp p={d['p_value']:.4f} {d['signal']}")
    print(f"\n=== 主客一致性 ===")
    for tk, d in consistency.items():
        print(f"  {tk}: 主{d['主胜lift']:+.1f} 客{d['客胜lift']:+.1f} {'✓一致' if d['一致'] else '✗相反'}")

    print(f"\n→ {OUT_REPORT}")

    # ── 生成tick特征CSV ──
    feat_rows = []
    for r in rows:
        try:
            oh, oa = float(r['odds_home']), float(r['odds_away'])
        except: continue
        fr = {"match_date": r.get("match_date",""), "home": r.get("home_team",""), "away": r.get("away_team",""),
              "result_class": r.get("result_class","")}
        # tick特征 (基于验证结果: .4=陷阱 .1/.2/.9=强信号, 1.0-1.49区间)
        ht = tick_of(oh, 1.0, 1.5)
        at = tick_of(oa, 1.0, 1.5)
        fr["home_tick"] = ht if ht is not None else ""
        fr["away_tick"] = at if at is not None else ""
        fr["is_home_trap_04"] = 1 if ht == 4 else 0
        fr["is_home_strong_129"] = 1 if ht in (1,2,9) else 0
        fr["is_away_trap_04"] = 1 if at == 4 else 0
        fr["is_away_strong_129"] = 1 if at in (1,2,9) else 0
        # 综合tick信号: 任一方陷阱 or 强信号
        fr["any_trap"] = 1 if (ht==4 or at==4) else 0
        fr["any_strong"] = 1 if (ht in (1,2,9) or at in (1,2,9)) else 0
        # 单向增强: 强信号方 vs 陷阱方(不限双方都在低价区, 更实用)
        # home_strong + away_trap = 主队双重利好
        fr["home_double_edge"] = 1 if (ht in (1,2,9) and at==4) else 0
        # away_strong + home_trap = 客队双重利好
        fr["away_double_edge"] = 1 if (at in (1,2,9) and ht==4) else 0
        feat_rows.append(fr)

    if feat_rows:
        fields = list(feat_rows[0].keys())
        with open(OUT_FEAT, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.DictWriter(fp, fieldnames=fields)
            w.writeheader()
            w.writerows(feat_rows)
    print(f"→ {OUT_FEAT} ({len(feat_rows)}行)")

if __name__ == "__main__":
    main()
