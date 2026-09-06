# -*- coding: utf-8 -*-
"""
特征深度分析 — 不依赖赛果, 从盘口数据本身挖掘信号分布与可用性。
输出:
  1. 各特征分布统计 (均值/分位数/异常)
  2. 信号画像 (大小球/让球/独赢 各自的分布特征)
  3. 可用性分层 (哪些比赛能喂哪些模型)
  4. 特征交叉发现 (让球×大小×独赢 的组合信号)
  → data/long_features/feature_analysis_report.json
"""
import csv, json, statistics as st
from collections import Counter, defaultdict

IN = r"D:\Architecture\data\long_features\match_features_canon.csv"
OUT = r"D:\Architecture\data\long_features\feature_analysis_report.json"

def nums(rows, key):
    out = []
    for r in rows:
        v = r.get(key, "")
        if v == "" or v is None: continue
        try: out.append(float(v))
        except: pass
    return out

def dist(vals, label=""):
    if not vals: return {"n": 0}
    vals = sorted(vals)
    n = len(vals)
    def pct(p): return vals[min(n-1, int(p*n))]
    return {
        "n": n, "min": round(vals[0],3), "max": round(vals[-1],3),
        "mean": round(st.mean(vals),3), "median": round(st.median(vals),3),
        "p25": round(pct(0.25),3), "p75": round(pct(0.75),3),
        "stdev": round(st.pstdev(vals),3) if n>1 else 0,
    }

def main():
    rows = list(csv.DictReader(open(IN, encoding="utf-8-sig")))
    R = {}

    # ══ 1. 各特征分布 ══
    R["特征分布"] = {}
    # 大小球盘口线 (转成单一数值, 亚洲盘X/Y取均值)
    ou_totals = []
    for r in rows:
        ou = r.get("ou_line","")
        if not ou: continue
        try:
            parts = [float(x) for x in ou.split("/")]
            ou_totals.append(sum(parts)/len(parts))
        except: pass
    R["特征分布"]["大小球盘口线(隐含总进球)"] = dist(ou_totals)

    R["特征分布"]["让球盘(主队,负=主让热门)"] = dist(nums(rows,"handicap_home"))
    R["特征分布"]["独赢主赔"] = dist(nums(rows,"odds_h"))
    R["特征分布"]["独赢平赔"] = dist(nums(rows,"odds_d"))
    R["特征分布"]["独赢客赔"] = dist(nums(rows,"odds_a"))
    R["特征分布"]["隐含P(主)"] = dist(nums(rows,"imp_h"))
    R["特征分布"]["隐含P(平)"] = dist(nums(rows,"imp_d"))
    R["特征分布"]["隐含P(客)"] = dist(nums(rows,"imp_a"))
    R["特征分布"]["平局溢价draw_deviation"] = dist(nums(rows,"draw_deviation"))
    R["特征分布"]["抽水率margin%"] = dist(nums(rows,"margin_1x2"))

    # ══ 2. 大小球信号画像 (97%覆盖, 主力) ══
    ou_sig = {}
    # 分档: <2.0低进球, 2.0-2.75正常, >2.75高进球, >3.5对攻
    buckets = Counter()
    for t in ou_totals:
        if t < 2.0: buckets["低进球(<2.0)"] += 1
        elif t <= 2.75: buckets["正常(2.0-2.75)"] += 1
        elif t <= 3.5: buckets["高进球(2.75-3.5)"] += 1
        else: buckets["对攻(>3.5)"] += 1
    ou_sig["总进球分档"] = dict(buckets)
    ou_sig["解读"] = "盘口线反映操盘手对总进球的预期。低进球档(防守型)+高进球档(对攻型)是两端信号。"
    R["大小球信号画像"] = ou_sig

    # ══ 3. 让球信号画像 ══
    hcap = nums(rows, "handicap_home")
    hcap_sig = {}
    hc_buckets = Counter()
    for h in hcap:
        if h <= -1.0: hc_buckets["主让1球+(强主)"] += 1
        elif h < 0: hc_buckets["主让0.x(略强主)"] += 1
        elif h == 0: hc_buckets["平手盘"] += 1
        else: hc_buckets["主受让(客强)"] += 1
    hcap_sig["让球分档"] = dict(hc_buckets)
    hcap_sig["解读"] = "让球盘直接反映主客实力差。主让1球+=一边倒, 平手盘=势均力敌(平局概率高)。"
    R["让球信号画像"] = hcap_sig

    # ══ 4. 独赢胜负画像 ══
    fav = Counter(r.get("fav_side","") for r in rows if r.get("fav_side"))
    R["独赢胜负画像"] = {
        "热门方分布": dict(fav),
        "解读": "H=主队热门, A=客队热门, D=平局赔率最低(罕见, 强平局信号)",
    }

    # ══ 5. 让球-大小背离信号 (核心新发现) ══
    div_rows = [r for r in rows if r.get("handicap_ou_divergence") == "1"]
    consistent = [r for r in rows if r.get("handicap_ou_divergence") == "0"]
    R["让球大小背离信号"] = {
        "背离场次": len(div_rows),
        "一致场次": len(consistent),
        "背离率": f"{100*len(div_rows)/max(len(div_rows)+len(consistent),1):.0f}%",
        "解读": "背离=主让(强主)但总进球低, 或主受让但总进球高。操盘手定价矛盾, 历史上对应平局/冷门概率升高。这是本次最强的衍生信号。",
        "背离样本": [{"home":r.get("home",""),"away":r.get("away",""),"让球":r.get("handicap_home"),"大小":r.get("ou_line")} for r in div_rows[:8]],
    }

    # ══ 6. 平局信号交叉 (draw_deviation > 0 + 高让球背离) ══
    draw_candidates = []
    for r in rows:
        dv = r.get("draw_deviation","")
        if dv == "": continue
        try: dvf = float(dv)
        except: continue
        if dvf > 0.05:  # 平局隐含概率显著高于0.333
            draw_candidates.append({"home":r.get("home",""),"away":r.get("away",""),
                "imp_d":r.get("imp_d"),"draw_deviation":dv,"背离":r.get("handicap_ou_divergence","0")})
    R["平局信号候选(draw_deviation>+0.05)"] = {
        "数量": len(draw_candidates),
        "样本": draw_candidates[:10],
        "解读": "draw_deviation>+0.05=操盘手显著抬升平局定价, 是平局强信号(借鉴draw_signal)。叠加背离=双重确认。",
    }

    # ══ 7. 可用性分层 ══
    layer = {"L1_完整(独赢+让球+大小,可喂全套模型)":0,
             "L2_让球+大小(可喂让球/大小规则,无独赢)":0,
             "L3_仅大小球(97%覆盖,最基础)":0,
             "L4_仅资金面":0}
    for r in rows:
        has_1x2 = bool(r.get("odds_h"))
        has_hcap = r.get("handicap_home") not in ("",None)
        has_ou = r.get("ou_line") not in ("",None)
        has_bf = bool(r.get("bf_volume_total"))
        if has_1x2 and has_hcap and has_ou: layer["L1_完整(独赢+让球+大小,可喂全套模型)"] += 1
        elif has_hcap and has_ou: layer["L2_让球+大小(可喂让球/大小规则,无独赢)"] += 1
        elif has_ou: layer["L3_仅大小球(97%覆盖,最基础)"] += 1
        elif has_bf: layer["L4_仅资金面"] += 1
    R["可用性分层"] = layer

    json.dump(R, open(OUT,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"→ {OUT}")
    return R

if __name__ == "__main__":
    R = main()
    # 打印关键结论
    print("\n" + "="*60)
    print("特征深度分析结论")
    print("="*60)
    print(f"\n【大小球盘口线】 {R['特征分布']['大小球盘口线(隐含总进球)']}")
    print(f"  分档: {R['大小球信号画像']['总进球分档']}")
    print(f"\n【让球盘】 {R['特征分布']['让球盘(主队,负=主让热门)']}")
    print(f"  分档: {R['让球信号画像']['让球分档']}")
    print(f"\n【独赢热门方】 {R['独赢胜负画像']['热门方分布']}")
    print(f"\n【让球-大小背离】 {R['让球大小背离信号']['背离率']} ({R['让球大小背离信号']['背离场次']}场)")
    for s in R['让球大小背离信号']['背离样本'][:5]:
        print(f"    {s['home'][:10]} vs {s['away'][:10]} | 让{s['让球']} 大小{s['大小']}")
    print(f"\n【平局强信号候选 draw_deviation>+0.05】 {R['平局信号候选(draw_deviation>+0.05)']['数量']}场")
    for s in R['平局信号候选(draw_deviation>+0.05)']['样本'][:5]:
        print(f"    {s['home'][:10]} vs {s['away'][:10]} | imp_d={s['imp_d']} dev={s['draw_deviation']} 背离={s['背离']}")
    print(f"\n【可用性分层】")
    for k,v in R['可用性分层'].items(): print(f"    {k}: {v}")
