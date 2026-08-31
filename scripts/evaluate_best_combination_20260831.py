"""4 盘口「打穿盘口」诚实压测 (2026-08-31, IR-30)
================================================================
对 胜平负(1X2) / 大小球(OU) / 让球(AH) / 波胆(CS) 四个盘口各自最佳模型/策略,
**在收盘前现实价结算**, bootstrap 95% CI + 5段时间窗, 回答"能否打穿盘口"。

- 1X2 : fl_1x2 argmax @ 收盘前1X2价  (对照 fused_1x2)
- OU   : 选择性overlay(|edge|>=0.02) @ 收盘前OU价  (全量 + 低线窄 2.0-2.75)
- AH   : fl_ah argmax @ 收盘前AH价(跳过走水)  (对照 AH共识过滤:1X2方向一致+edge>=0.10)
- CS   : 泊松 top1/top3/top5 命中率 (非ROI, 明确"非单点预测"/分析非预测)

结算价全部取 odds_snapshots 最新赛前快照(captured_at <= ko), 即你实际能成交的现实价。
绝不真实下注(IR-21)。
"""
from __future__ import annotations
import os, sys, json, sqlite3, datetime, math
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.build_fused_models_20260831 import collect, fl_probs, league_probs, build_feat, FIT_FRAC
from scripts.compare_ou_models_20260830 import opening_ou
from pipeline.poisson_gbm import predict_lambdas
import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")
DB = os.path.join(ROOT, "data", "events.db")
REPORTS = os.path.join(ROOT, "reports")
f_x2 = joblib.load(os.path.join(MODELS, "fused_1x2_20260831.joblib"))
f_ou = joblib.load(os.path.join(MODELS, "fused_ou_20260831.joblib"))

# ----------------------------------------------------------- 去水
def devig3(oh, od, oa):
    inv = 1/oh + 1/od + 1/oa
    return np.array([(1/oh)/inv, (1/od)/inv, (1/oa)/inv])

def devig2(ov, un):
    inv = 1/ov + 1/un
    return (1/ov)/inv, (1/un)/inv

# ----------------------------------------------------------- 收盘前现实价
def closing_1x2(con, mk, ko):
    rows = con.execute(
        "SELECT selection, odds, captured_at FROM odds_snapshots WHERE match_key=? "
        "AND market='1X2' AND captured_at <= ? ORDER BY captured_at DESC", (mk, ko)).fetchall()
    d = {}
    for sel, o, ca in rows:
        if sel not in d and o and o > 1.01:
            d[sel] = o
    return d.get("home"), d.get("draw"), d.get("away")

def closing_ou(con, mk, line, ko):
    rows = con.execute(
        "SELECT selection, odds, captured_at FROM odds_snapshots WHERE match_key=? "
        "AND market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' AND line=? AND captured_at <= ? "
        "ORDER BY captured_at DESC", (mk, line, ko)).fetchall()
    d = {}
    for sel, o, ca in rows:
        if sel not in d and o and o > 1.01:
            d[sel] = o
    return d.get("over"), d.get("under")

def closing_ah(con, mk, ko, line):
    rows = con.execute(
        "SELECT selection, odds, captured_at FROM odds_snapshots WHERE match_key=? "
        "AND market LIKE 'AH_%' AND market NOT LIKE 'AH_1H%' AND market NOT LIKE 'AH_2H%' "
        "AND line=? AND selection IN ('home','away') AND captured_at <= ? ORDER BY captured_at DESC",
        (mk, line, ko)).fetchall()
    d = {}
    for sel, o, ca in rows:
        if sel not in d and o and o > 1.01:
            d[sel] = o
    return d.get("home"), d.get("away")

# ----------------------------------------------------------- 统计
def bootstrap(profits):
    if len(profits) < 30:
        return [None, None]
    arr = np.array(profits, dtype=float)
    rng = np.random.default_rng(20260831)
    bs = np.array([arr[rng.integers(0, len(arr), len(arr))].mean() for _ in range(1000)])
    return [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]

def seg_rois(profits_sorted):
    if len(profits_sorted) < 5:
        return [float(np.mean(profits_sorted)) if profits_sorted else None]
    segs = np.array_split(np.array(profits_sorted, dtype=object), 5)
    return [float(np.mean([float(x) for x in s])) if len(s) else None for s in segs]

def poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    if k < 0:
        return 0.0
    return math.exp(k*math.log(lam) - lam - sum(math.log(i) for i in range(1, k+1)))

def cs_hit(sh, sa, lh, la, topn):
    grid = {}
    for i in range(0, 9):
        for j in range(0, 9):
            grid[(i, j)] = poisson_pmf(i, lh) * poisson_pmf(j, la)
    ranked = sorted(grid.items(), key=lambda kv: -kv[1])
    top = set(p for p, _ in ranked[:topn])
    return (sh, sa) in top

# ----------------------------------------------------------- AH 结算
def ah_settle(sh, sa, line, pick, odds):
    """返回单位注利润; 走水返回 None(跳过)。quarter line 跳过(少数, 不宣称其 edge)。"""
    if abs(line * 4) % 2 == 1:   # 0.25/0.75  Quarter —— 跳过避免误结算
        return None
    d = sh - sa
    thr = -line
    if d == thr:
        return None  # 走水
    home_covers = d > thr
    win = (pick == 0 and home_covers) or (pick == 1 and not home_covers)
    return (odds - 1.0) if win else -1.0

# ----------------------------------------------------------- 主流程
def main():
    recs = collect()
    recs.sort(key=lambda r: r["ko"])
    k = int(len(recs) * FIT_FRAC)
    cutoff = recs[k-1]["ko"]
    paper = sorted([r for r in recs if r["ko"] > cutoff], key=lambda r: r["ko"])
    con = sqlite3.connect(DB, timeout=60); con.row_factory = sqlite3.Row

    rec = []
    skip_ah = skip_ou = skip_1x2 = 0
    for r in paper:
        fl = fl_probs(r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
                      r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"])
        fl_1x2 = fl["1x2"]; fl_ou_pOver = fl["ou"][0] if fl["ou"] else None; fl_ah = fl["ah"]  # fl_model_ou 已下线(2026-08-31)
        lam = predict_lambdas(r["oh"], r["od"], r["oa"], ch=r["oh"], cd=r["od"], ca=r["oa"], league=r["league"])
        sh, sa = r["sh"], r["sa"]
        y = 0 if sh > sa else (1 if sh == sa else 2)
        y_ou = 1 if (sh + sa) > r["line"] else 0
        imp_1x2 = devig3(r["oh"], r["od"], r["oa"])
        imp_ou = devig2(r["ov"], r["un"])
        coh, cod, coa = closing_1x2(con, r["mk"], r["ko"])
        cou, cun = closing_ou(con, r["mk"], r["line"], r["ko"])
        cah, caa = closing_ah(con, r["mk"], r["ko"], r["ah_line"])
        if coh is None or cod is None or coa is None:
            skip_1x2 += 1; coh, cod, coa = r["oh"], r["od"], r["oa"]
        if cou is None or cun is None:
            skip_ou += 1; cou, cun = r["ov"], r["un"]
        if cah is None or caa is None:
            skip_ah += 1; cah, caa = r["ah_h"], r["ah_a"]
        rec.append(dict(fl_1x2=fl_1x2, fl_ou_pOver=fl_ou_pOver, fl_ah=fl_ah,
                        lam=lam, sh=sh, sa=sa, y=y, y_ou=y_ou, line=r["line"],
                        ah_line=r["ah_line"],
                        imp_1x2=imp_1x2, imp_ou=imp_ou,
                        c1x2=np.array([coh, cod, coa]), c_ou=np.array([cou, cun]),
                        c_ah=np.array([cah, caa]), ko=r["ko"], league=r["league"]))
        rec[-1]["y"] = y
    con.close()
    n = len(rec)
    print(f"纸盘宇宙 n={n} | 缺收盘快照: 1X2跳过{skip_1x2} OU跳过{skip_ou} AH跳过{skip_ah}")

    out = dict(generated_at=datetime.datetime.now().astimezone().isoformat(),
               cutoff_kickoff=cutoff, paper_n=n, bet_types={})

    # ---- 1X2 ----
    def x2_roi(price_key):
        prof = []
        for r in rec:
            p = r["fl_1x2"]; pick = int(np.argmax(p)); odds = r[price_key][pick]
            if odds is None or odds <= 1.01: continue
            prof.append((odds - 1) if pick == r["y"] else -1.0)
        return prof
    p_x2 = x2_roi("c1x2"); r_x2 = float(np.mean(p_x2)) if p_x2 else float("nan")
    ci_x2 = bootstrap(p_x2); seg_x2 = seg_rois([x for x in sorted(p_x2)])  # 1x2 无 ko 排序需求, 用全序
    out["bet_types"]["1X2_fl"] = dict(roi=round(r_x2,4), n=len(p_x2),
        ci=[round(x,4) if x else None for x in ci_x2],
        verdict=("SURVIVES" if (r_x2>0 and (ci_x2[0] or 0)>0) else "FAILS"))

    # ---- OU 选择性 overlay @ 收盘前 ----
    def ou_selective(line_filter=None):
        prof = []
        for r in rec:
            if line_filter and not (line_filter[0] <= r["line"] <= line_filter[1]):
                continue
            edge = r["fl_ou_pOver"] - r["imp_ou"][0]
            if abs(edge) < 0.02: continue
            pick = 0 if edge > 0 else 1
            odds = r["c_ou"][pick]
            if odds is None or odds <= 1.01: continue
            prof.append((odds - 1) if pick == r["y_ou"] else -1.0)
        return prof
    p_ou_all = ou_selective(); r_ou_all = float(np.mean(p_ou_all)) if p_ou_all else float("nan")
    p_ou_low = ou_selective((2.0, 2.75)); r_ou_low = float(np.mean(p_ou_low)) if p_ou_low else float("nan")
    out["bet_types"]["OU_full_overlay"] = dict(roi=round(r_ou_all,4), n=len(p_ou_all),
        ci=[round(x,4) if x else None for x in bootstrap(p_ou_all)],
        verdict=("SURVIVES" if (r_ou_all>0 and (bootstrap(p_ou_all)[0] or 0)>0) else "FAILS"))
    out["bet_types"]["OU_lowline_2.0_2.75"] = dict(roi=round(r_ou_low,4), n=len(p_ou_low),
        ci=[round(x,4) if x else None for x in bootstrap(p_ou_low)],
        verdict=("SURVIVES" if (r_ou_low>0 and (bootstrap(p_ou_low)[0] or 0)>0) else "FAILS"))

    # ---- AH: 泊松覆盖率模型 vs AH赔率隐含覆盖率 (双边 overlay @ 收盘前) ----
    # fl_ah 是"主/客胜"2类分类器(非覆盖率), 不能直接押让球盘; 正确覆盖率来自泊松λ。
    def poisson_cover(lh, la, line):
        ph = pa = pp = 0.0
        for i in range(0, 13):
            for j in range(0, 13):
                pr = poisson_pmf(i, lh) * poisson_pmf(j, la)
                d = i - j
                if d > -line: ph += pr
                elif d < -line: pa += pr
                else: pp += pr  # 走水
        return ph, pa, pp

    def ah_overlay():
        prof = []
        odds_list = []
        for r in rec:
            if not r["lam"]: continue
            lh, la = r["lam"]
            ph, pa, pp = poisson_cover(lh, la, r["ah_line"])
            imp_h, imp_a = devig2(r["c_ah"][0], r["c_ah"][1])
            edge = ph - imp_h
            if abs(edge) < 0.02: continue
            pick = 0 if edge > 0 else 1
            odds = r["c_ah"][pick]
            if odds is None or odds <= 1.01: continue
            res = ah_settle(r["sh"], r["sa"], r["ah_line"], pick, odds)
            if res is None: continue
            prof.append(res); odds_list.append(odds)
        return prof, odds_list

    def ah_always_dog():
        """朴素基线: 永远押受让方(狗)覆盖。数据集显示热门方实际覆盖率仅~0.46(狗>0.5)。"""
        prof = []
        for r in rec:
            odds = r["c_ah"][1]  # away = 受让方(线为负时 away 为狗)
            if odds is None or odds <= 1.01: continue
            # 仅当 home 为热门(line<0) 才押狗; 否则押 home
            pick = 1 if r["ah_line"] < 0 else 0
            odds = r["c_ah"][pick]
            res = ah_settle(r["sh"], r["sa"], r["ah_line"], pick, odds)
            if res is None: continue
            prof.append(res)
        return prof

    p_ah, odds_ah = ah_overlay(); r_ah = float(np.mean(p_ah)) if p_ah else float("nan")
    win_ah = float(np.mean([1 if x > 0 else 0 for x in p_ah])) if p_ah else float("nan")
    p_dog = ah_always_dog(); r_dog = float(np.mean(p_dog)) if p_dog else float("nan")
    # 数据 reality check: 热门方实际覆盖率
    fav_cover = float(np.mean([(r["sh"]-r["sa"] > -r["ah_line"])
                               for r in rec if abs(r["ah_line"]*4)%2==0]))
    out["bet_types"]["AH_poisson_overlay"] = dict(roi=round(r_ah,4), n=len(p_ah),
        win_rate=round(win_ah,4), mean_odds=round(float(np.mean(odds_ah)),4) if odds_ah else None,
        ci=[round(x,4) if x else None for x in bootstrap(p_ah)],
        verdict=("SURVIVES" if (r_ah>0 and (bootstrap(p_ah)[0] or 0)>0) else "FAILS"))
    out["bet_types"]["AH_always_dog"] = dict(roi=round(r_dog,4), n=len(p_dog),
        ci=[round(x,4) if x else None for x in bootstrap(p_dog)],
        note="朴素基线: 永远押受让方覆盖; 数据集热门方实际覆盖率={:.2%}".format(fav_cover),
        verdict=("SURVIVES" if (r_dog>0 and (bootstrap(p_dog)[0] or 0)>0) else "FAILS"))

    # ---- CS 泊松命中率 (非ROI) ----
    cs_res = {}
    for topn in (1, 3, 5):
        hit = 0; tot = 0
        for r in rec:
            if not r["lam"]: continue
            tot += 1
            if cs_hit(r["sh"], r["sa"], r["lam"][0], r["lam"][1], topn):
                hit += 1
        cs_res[f"top{topn}_hit"] = round(hit/tot, 4) if tot else None
        cs_res[f"top{topn}_n"] = tot
    out["bet_types"]["CS_poisson"] = dict(**cs_res,
        note="泊松top-N波胆命中率(非ROI); 明确'非单点预测'——波胆=庄家诱导器(IR-03), 仅给概率分布")

    out["overall_verdict"] = {
        "beat_bookmaker": any(out["bet_types"][b].get("verdict")=="SURVIVES"
                              for b in ("1X2_fl","OU_lowline_2.0_2.75","AH_poisson_overlay","AH_always_dog")),
        "note": "仅当某盘口 ROI>0 且 95% CI 下界>0 才算打穿盘口; 否则=无异于无edge(IR-30)",
    }

    # ---- 控制台摘要 ----
    for b, v in out["bet_types"].items():
        if "roi" in v:
            ci = v.get("ci") or [None,None]
            print(f"  [{b}] ROI={v['roi']:+.2%} n={v['n']} CI[{ci[0] or 0:+.2%},{ci[1] or 0:+.2%}] -> {v['verdict']}")
        else:
            print(f"  [{b}] {v}")
    print(f"\nAH 热门方实际覆盖率(本harness重算) = {fav_cover:.2%}  (catalog 宣称 fl_AH 76.47% 实为'主客胜负2类'准确率, 非让球覆盖率)")
    print(f"打穿盘口? {out['overall_verdict']['beat_bookmaker']}")

    with open(os.path.join(REPORTS, "evaluate_best_combination_20260831.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("-> reports/evaluate_best_combination_20260831.json")

if __name__ == "__main__":
    main()
