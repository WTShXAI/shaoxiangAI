"""1X2 / league 融合 现实价(收盘前)压力测试 (2026-08-31, IR-30 对称闭环)
================================================================
对 OU 通用overlay 的研究证明: 开盘价ROI会"价格时点虚高", 收盘前现实价下 CI 跨零。
现对称施加到另两个融合模型(1X2 / league), 回答"它们是否也只是回测假象":
- 选择性overlay: 仅当 model[argmax] - 开盘去水隐含[argmax] >= MARGIN 才押 argmax(收盘前价结算)。
- 对照: 无脑押argmax@开盘(复现回测 -0.69%/+1.20%) / 选择性@开盘 / 选择性@收盘前。
- bootstrap 95% CI + 5段时间窗 + 按联赛结构化(前10联赛)。
- 绝不真实下注。
"""
from __future__ import annotations
import os, sys, json, sqlite3, datetime
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.build_fused_models_20260831 import collect, fl_probs, league_probs, build_feat, FIT_FRAC
import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")
DB = os.path.join(ROOT, "data", "events.db")
REPORTS = os.path.join(ROOT, "reports")
f_x2 = joblib.load(os.path.join(MODELS, "fused_1x2_20260831.joblib"))
f_lg = joblib.load(os.path.join(MODELS, "fused_league_20260831.joblib"))
MARGIN = 0.03

def devig3(oh, od, oa):
    inv = 1/oh + 1/od + 1/oa
    return np.array([(1/oh)/inv, (1/od)/inv, (1/oa)/inv])

def closing_1x2(con, mk, ko):
    rows = con.execute(
        "SELECT selection, odds, captured_at FROM odds_snapshots WHERE match_key=? "
        "AND market='1X2' AND captured_at <= ? ORDER BY captured_at DESC", (mk, ko)).fetchall()
    d = {}
    for sel, o, ca in rows:
        if sel not in d and o and o > 1.01:
            d[sel] = o
    return d.get("home"), d.get("draw"), d.get("away")

def fused_1x2_probs(fl_1x2, fl_ah):
    return f_x2["meta"].predict_proba(np.array([[fl_1x2[0], fl_1x2[1], fl_1x2[2], fl_ah[0], fl_ah[1]]]))[0]

def fused_league_probs(lg_main, lg_draw):
    return f_lg["meta"].predict_proba(np.array([[lg_main[0], lg_main[1], lg_main[2], lg_draw]]))[0]

def bootstrap(profits):
    if len(profits) < 30:
        return [None, None]
    arr = np.array(profits)
    rng = np.random.default_rng(20260831)
    bs = np.array([arr[rng.integers(0, len(arr), len(arr))].mean() for _ in range(1000)])
    return [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]

def seg_rois(profits_sorted):
    if len(profits_sorted) < 5:
        return [float(np.mean(profits_sorted)) if profits_sorted else None]
    segs = np.array_split(np.array(profits_sorted, dtype=object), 5)
    return [float(np.mean([float(x) for x in s])) if len(s) else None for s in segs]

def main():
    recs = collect()
    recs.sort(key=lambda r: r["ko"])
    k = int(len(recs) * FIT_FRAC)
    cutoff = recs[k-1]["ko"]
    paper = sorted([r for r in recs if r["ko"] > cutoff], key=lambda r: r["ko"])
    con = sqlite3.connect(DB, timeout=60); con.row_factory = sqlite3.Row

    rec = []
    skipped = 0
    for r in paper:
        fl = fl_probs(r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
                      r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"])
        lg_main, lg_draw = league_probs(build_feat(
            r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
            r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"]))
        fx = fused_1x2_probs(fl["1x2"], fl["ah"])
        flg = fused_league_probs(lg_main, lg_draw)
        imp = devig3(r["oh"], r["od"], r["oa"])
        y = 0 if r["sh"] > r["sa"] else (1 if r["sh"] == r["sa"] else 2)
        coh, cod, coa = closing_1x2(con, r["mk"], r["ko"])
        # 开盘价(真开盘快照, 来自 collect 的 oh/od/oa) 与 收盘前价(最新赛前快照) 各存 3 元数组
        rec.append(dict(fx=fx, flg=flg, imp=imp, y=y,
                        o=np.array([r["oh"], r["od"], r["oa"]]),
                        c=np.array([coh, cod, coa], dtype=object),
                        ko=r["ko"], league=r["league"]))
        if coh is None or cod is None or coa is None:
            skipped += 1
    con.close()
    print(f"纸盘宇宙 n={len(rec)} | 无收盘前1X2快照 {skipped}")

    def run_always(mk, price_key):
        """无脑押 argmax@price(复现回测)。"""
        prof = []
        for r in rec:
            p = r[mk]; pick = int(np.argmax(p)); odds = r[price_key][pick]
            if odds is None or odds <= 1.01:
                continue
            prof.append((odds - 1) if pick == r["y"] else -1.0)
        return (float(np.mean(prof)) if prof else float("nan"), len(prof))

    def selective_bets(mk, price_key, margin):
        """仅当 model[argmax]-imp[argmax] >= margin 押 argmax@price。返回 [(ko, league, profit)]。"""
        out = []
        for r in rec:
            p = r[mk]; pick = int(np.argmax(p)); edge = p[pick] - r["imp"][pick]
            if edge < margin:
                continue
            odds = r[price_key][pick]
            if odds is None or odds <= 1.01:
                continue
            out.append((r["ko"], r["league"], (odds - 1) if pick == r["y"] else -1.0))
        return out

    def roi_of(bets):
        p = [x[2] for x in bets]
        return (float(np.mean(p)) if p else float("nan")), len(p), bootstrap(p)

    out = dict(generated_at=datetime.datetime.now().astimezone().isoformat(),
               margin=MARGIN, cutoff_kickoff=cutoff, paper_n=len(rec), no_closing=skipped, models={})
    for name, mk in [("fused_1x2", "fx"), ("fused_league", "flg")]:
        r_ao, n_ao = run_always(mk, "o")
        sb_o = selective_bets(mk, "o", MARGIN)
        sb_c = selective_bets(mk, "c", MARGIN)
        r_so, n_so, _ = roi_of(sb_o)
        r_sc, n_sc, ci_sc = roi_of(sb_c)
        # ---- 诚实校验 1: 剔除空联赛(306场垃圾桶伪信号) ----
        sb_c_ne = [x for x in sb_c if x[1] != ""]
        r_ne, n_ne, ci_ne = roi_of(sb_c_ne)
        # ---- 诚实校验 2: 更高确信度 margin=0.05 ----
        sb_c05 = selective_bets(mk, "c", 0.05)
        r_c05, n_c05, ci_c05 = roi_of(sb_c05)
        sb_o05 = selective_bets(mk, "o", 0.05)
        r_o05, n_o05, _ = roi_of(sb_o05)
        seg = seg_rois([x[2] for x in sorted(sb_c, key=lambda t: t[0])])
        by_lg = defaultdict(list)
        for _, lg, pr in sb_c:
            by_lg[lg].append(pr)
        lg_roi = {lg: round(float(np.mean(v)), 4) for lg, v in by_lg.items() if len(v) >= 15}
        lg_roi = dict(sorted(lg_roi.items(), key=lambda kv: -kv[1])[:10])
        survives = (r_sc > 0 and (ci_sc[0] or 0) > 0 and r_ne > 0 and (ci_ne[0] or 0) > 0
                    and r_c05 > 0 and (ci_c05[0] or 0) > 0)
        out["models"][name] = dict(
            always_open_roi=round(r_ao, 4), n_always_open=n_ao,
            selective_open_roi=round(r_so, 4), n_selective_open=n_so,
            selective_closing_roi=round(r_sc, 4), n_selective_closing=n_sc,
            selective_closing_ci=[round(x, 4) if x else None for x in ci_sc],
            selective_closing_noempty_roi=round(r_ne, 4), n_selective_closing_noempty=n_ne,
            selective_closing_noempty_ci=[round(x, 4) if x else None for x in ci_ne],
            selective_closing_margin005_roi=round(r_c05, 4), n_selective_closing_margin005=n_c05,
            selective_closing_margin005_ci=[round(x, 4) if x else None for x in ci_c05],
            selective_open_margin005_roi=round(r_o05, 4),
            seg_closing=seg,
            top_leagues_closing=lg_roi,
            verdict=("SURVIVES_CLOSING" if survives else "FAILS_CLOSING"),
        )
        print(f"\n[{name}] 无脑@开盘={r_ao:+.2%}(n={n_ao}) | 选择性@开盘={r_so:+.2%}(n={n_so}) | "
              f"选择性@收盘前={r_sc:+.2%}(n={n_sc}) CI[{ci_sc[0] or 0:+.2%},{ci_sc[1] or 0:+.2%}]")
        print(f"  ├ 剔除空联赛: {r_ne:+.2%}(n={n_ne}) CI[{ci_ne[0] or 0:+.2%},{ci_ne[1] or 0:+.2%}]")
        print(f"  ├ margin=0.05@收盘: {r_c05:+.2%}(n={n_c05}) CI[{ci_c05[0] or 0:+.2%},{ci_c05[1] or 0:+.2%}]")
        print(f"  5段时间窗: " + " | ".join(f"{('—' if v is None else f'{v:+.2%}')}" for v in seg))
        print(f"  按联赛(收盘前, 前10): " + " | ".join(f"{lg or '(空)'}:{v:+.2%}" for lg, v in lg_roi.items()))
        print(f"  判定: {out['models'][name]['verdict']}")

    with open(os.path.join(REPORTS, "research_1x2_league_realistic_odds_20260831.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n-> reports/research_1x2_league_realistic_odds_20260831.json")

if __name__ == "__main__":
    main()
