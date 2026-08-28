"""
cup_over_edge_test.py — 「杯赛大球」反向 edge 检验

来源:
  strategy_significance_test.py 中唯一统计显著的信号是 **负向** 的:
      Cup 小球  ROI = -30.83%  (n=484)  CI=[-39.5%, -22.1%]  完全不含 0
      IS -14.35% / OOS -53.26%  方向一致
  即: 在杯赛里买小球是稳定亏钱的 => 反方向(买大球)可能存在真实 edge。

  注意这不是简单取反: 小球亏 30% 不等于大球赚 30%,
  因为庄家两边都抽水 (overround), 中间有 6~10% 的水钱。
  必须用「大球赔率」真金白银重算。

四关同样适用: Bootstrap CI / 置换检验 / 时间切分 OOS / 多重检验修正。
另加:
  - 按 OU 线位分层 (看 edge 集中在哪些线)
  - 按赔率区间分层 (排除「只在极端赔率上盈利」的伪信号)
  - Kelly 半凯利模拟资金曲线 + 最大回撤

输出: data/pricing_template/cup_over_edge_report.json
"""
import os
import sys
import json
import logging
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.clean_outcomes import load_clean_outcomes  # noqa: E402
from pipeline.opening_line import build_opening_lines    # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
GQ_DB = os.path.join(DATA_DIR, "events.db")
OUT_DIR = os.path.join(DATA_DIR, "pricing_template")

RNG = np.random.default_rng(20260805)
N_BOOT = 10000
N_PERM = 10000


def load_pool() -> pd.DataFrame:
    scan = pd.read_csv(os.path.join(OUT_DIR, "template_deviation_scan.csv"))
    # 2026-08-05: 走 clean_outcomes (剔电子盘 + 剔采集截断)
    oc, rep = load_clean_outcomes(return_report=True)
    log.info(f"[数据] 清洗后 n={rep['after']['n']} 均进球={rep['after']['avg_goals']} "
             f"零封={rep['after']['zero_rate']:.2%} "
             f"(剔虚拟{rep['dropped_virtual']} 剔截断{rep['dropped_truncated']})")
    # 2026-08-05 二次修复: 旧 op_ou_line 是"梯队最小线"不是主盘线 ->
    # 大球恒热造出 +15% 假 edge。改用 opening_line 重建的真主盘。
    op = build_opening_lines()
    oc["match_key"] = oc["home"].astype(str) + " vs " + oc["away"].astype(str)
    oc = oc.drop(columns=[c for c in ("op_ou_line", "op_ou_over", "op_ou_under")
                          if c in oc.columns])
    oc = oc.merge(op[["match_key", "line", "over", "under"]],
                  on="match_key", how="inner").drop_duplicates(subset=["mid"])
    oc = oc.rename(columns={"line": "op_ou_line", "over": "op_ou_over",
                            "under": "op_ou_under"})
    log.info(f"[数据] 初盘主盘线匹配 {len(oc)} 场, 均线位={oc['op_ou_line'].mean():.3f} "
             f"中位={oc['op_ou_line'].median():.2f}")
    oc = oc[["mid", "score_home", "score_away",
             "op_ou_line", "op_ou_over", "op_ou_under"]]
    oc["mid"] = oc["mid"].astype(str)
    scan["mid"] = scan["mid"].astype(str)
    df = scan.merge(oc, on="mid", how="inner")
    df = df[df["op_ou_line"].notna() & df["op_ou_over"].notna() & df["op_ou_under"].notna()].copy()
    df["total_goals"] = df["score_home"] + df["score_away"]
    df["date"] = pd.to_datetime(df["kickoff"], errors="coerce")

    line = df["op_ou_line"].astype(float)
    tot = df["total_goals"].astype(float)
    df["profit_over"] = np.where(tot > line, df["op_ou_over"].astype(float) - 1.0,
                                 np.where(tot < line, -1.0, 0.0))
    df["profit_under"] = np.where(tot < line, df["op_ou_under"].astype(float) - 1.0,
                                  np.where(tot > line, -1.0, 0.0))
    # overround
    inv = 1.0 / df["op_ou_over"].astype(float) + 1.0 / df["op_ou_under"].astype(float)
    df["ou_overround"] = inv - 1.0

    lg = df["league"].fillna("").astype(str)
    df["is_cup"] = lg.str.contains("杯|Cup", regex=True, na=False)
    df["is_youth"] = lg.str.contains("U1[6-9]|U2[0-3]|青年|预备|Youth|Reserve", regex=True, na=False)
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def boot_ci(p: np.ndarray, n_boot=N_BOOT, alpha=0.05):
    n = len(p)
    if n < 5:
        return None
    idx = RNG.integers(0, n, size=(n_boot, n))
    r = p[idx].mean(axis=1)
    lo, hi = np.percentile(r, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {"roi": round(float(p.mean()), 4), "ci_lo": round(float(lo), 4),
            "ci_hi": round(float(hi), 4), "p_gt0": round(float((r > 0).mean()), 4)}


def perm_test(all_profits: np.ndarray, mask: np.ndarray, n_perm=N_PERM):
    n_sel = int(mask.sum())
    if n_sel < 5 or n_sel >= len(all_profits):
        return None
    real = float(all_profits[mask].mean())
    N = len(all_profits)
    null = np.empty(n_perm)
    for i in range(n_perm):
        null[i] = all_profits[RNG.choice(N, size=n_sel, replace=False)].mean()
    return {"real_roi": round(real, 4), "p_value": round(float((null >= real).mean()), 4),
            "z": round(float((real - null.mean()) / max(1e-9, null.std())), 3)}


def stats(p: np.ndarray):
    w = int((p > 0).sum()); l = int((p < 0).sum()); pu = int((p == 0).sum())
    return {"n": len(p), "win": w, "loss": l, "push": pu,
            "hit": round(w / max(1, w + l), 4),
            "roi": round(float(p.mean()), 4) if len(p) else 0.0}


def equity_curve(p: np.ndarray, stake=0.02, bankroll=1.0):
    """固定比例注码资金曲线, 返回终值/最大回撤."""
    bk = bankroll
    peak = bk
    mdd = 0.0
    for x in p:
        bk *= (1.0 + stake * x)
        peak = max(peak, bk)
        mdd = max(mdd, (peak - bk) / peak)
    return {"final_bankroll": round(float(bk), 4),
            "total_return": round(float(bk - bankroll), 4),
            "max_drawdown": round(float(mdd), 4)}


def main():
    df = load_pool()
    log.info("=" * 68)
    log.info("「杯赛大球」反向 edge 检验")
    log.info("=" * 68)
    log.info(f"可下注池: {len(df)} 场, 其中杯赛 {int(df['is_cup'].sum())} 场")
    log.info(f"OU 平均抽水(overround): {df['ou_overround'].mean():.2%}")

    all_over = df["profit_over"].values

    log.info("\n" + "-" * 68)
    log.info("一、方向对照: 同一批杯赛, 买小 vs 买大")
    log.info("-" * 68)
    cup = df[df["is_cup"]]
    su = stats(cup["profit_under"].values)
    so = stats(cup["profit_over"].values)
    log.info(f"  杯赛买小: n={su['n']}, 命中={su['hit']:.1%}, ROI={su['roi']:+.2%}")
    log.info(f"  杯赛买大: n={so['n']}, 命中={so['hit']:.1%}, ROI={so['roi']:+.2%}")
    log.info(f"  非杯赛买大: ", )
    nc = df[~df["is_cup"]]
    snc = stats(nc["profit_over"].values)
    log.info(f"           n={snc['n']}, 命中={snc['hit']:.1%}, ROI={snc['roi']:+.2%}")

    # 待检策略 (买大球方向)
    strategies = {
        "Cup大球(全)": df["is_cup"].values,
        "Cup大球 OU<=2.5": (df["is_cup"] & (df["op_ou_line"].astype(float) <= 2.5)).values,
        "Cup大球 OU>=3.0": (df["is_cup"] & (df["op_ou_line"].astype(float) >= 3.0)).values,
        "Cup大球 赔率1.7-2.2": (df["is_cup"] & df["op_ou_over"].astype(float).between(1.7, 2.2)).values,
        "Cup大球 非Youth": (df["is_cup"] & ~df["is_youth"]).values,
        "全池大球(基准)": np.ones(len(df), dtype=bool),
    }

    log.info("\n" + "-" * 68)
    log.info("二、关卡1+2: Bootstrap 95% CI & 置换检验 (买大球)")
    log.info("-" * 68)
    log.info(f"  {'策略':<22s}{'n':>5s}{'命中':>8s}{'ROI':>9s}{'CI下界':>10s}{'CI上界':>10s}{'p值':>8s}")
    log.info("  " + "-" * 64)
    rows = []
    for name, m in strategies.items():
        p = all_over[m]
        if len(p) < 5:
            continue
        s = stats(p); ci = boot_ci(p); pt = perm_test(all_over, m)
        rows.append({"strategy": name, **s, "bootstrap": ci, "permutation": pt})
        pv = pt["p_value"] if pt else float("nan")
        log.info(f"  {name:<22s}{s['n']:>5d}{s['hit']:>8.1%}{s['roi']:>+9.2%}"
                 f"{ci['ci_lo']:>+10.2%}{ci['ci_hi']:>+10.2%}{pv:>8.4f}")

    K = len([r for r in rows if r["strategy"] != "全池大球(基准)"])
    bonf = 0.05 / max(1, K)
    log.info(f"\n  Bonferroni 阈值 (K={K}): {bonf:.4f}")

    log.info("\n" + "-" * 68)
    log.info("三、关卡3: 时间切分 OOS (前60%/后40%)")
    log.info("-" * 68)
    cut = int(len(df) * 0.6)
    log.info(f"  {'策略':<22s}{'IS n':>6s}{'IS ROI':>10s}{'OOS n':>7s}{'OOS ROI':>10s}{'OOS CI下界':>12s}")
    log.info("  " + "-" * 64)
    oos = {}
    for name, m in strategies.items():
        m_tr, m_te = m[:cut], m[cut:]
        p_tr = all_over[:cut][m_tr]
        p_te = all_over[cut:][m_te]
        if len(p_tr) < 5 or len(p_te) < 5:
            continue
        ci = boot_ci(p_te)
        oos[name] = {"is_n": len(p_tr), "is_roi": round(float(p_tr.mean()), 4),
                     "oos_n": len(p_te), "oos_roi": round(float(p_te.mean()), 4),
                     "oos_ci_lo": ci["ci_lo"], "oos_ci_hi": ci["ci_hi"],
                     "consistent": bool(p_tr.mean() > 0 and p_te.mean() > 0)}
        log.info(f"  {name:<22s}{len(p_tr):>6d}{p_tr.mean():>+10.2%}"
                 f"{len(p_te):>7d}{p_te.mean():>+10.2%}{ci['ci_lo']:>+12.2%}")

    log.info("\n" + "-" * 68)
    log.info("四、Cup 大球 按 OU 线位分层 (看 edge 是否集中/稳定)")
    log.info("-" * 68)
    by_line = []
    cupdf = df[df["is_cup"]]
    for lv in sorted(cupdf["op_ou_line"].astype(float).unique()):
        sub = cupdf[cupdf["op_ou_line"].astype(float) == lv]
        if len(sub) < 15:
            continue
        p = sub["profit_over"].values
        s = stats(p); ci = boot_ci(p)
        by_line.append({"line": float(lv), **s, "ci_lo": ci["ci_lo"], "ci_hi": ci["ci_hi"]})
        log.info(f"  OU {lv:>4.2f}: n={s['n']:>4d} 命中={s['hit']:>6.1%} ROI={s['roi']:>+8.2%} "
                 f"CI=[{ci['ci_lo']:+.1%},{ci['ci_hi']:+.1%}]")

    log.info("\n" + "-" * 68)
    log.info("五、资金曲线 (2% 固定比例注码)")
    log.info("-" * 68)
    eq = {}
    for name in ["Cup大球(全)", "Cup大球 非Youth", "全池大球(基准)"]:
        if name not in strategies:
            continue
        p = all_over[strategies[name]]
        e = equity_curve(p)
        eq[name] = e
        log.info(f"  {name:<22s} 终值={e['final_bankroll']:.4f} "
                 f"收益={e['total_return']:+.2%} 最大回撤={e['max_drawdown']:.2%}")

    # 最终判定
    log.info("\n" + "=" * 68)
    log.info("最终判定")
    log.info("=" * 68)
    final = []
    for r in rows:
        if r["strategy"] == "全池大球(基准)":
            continue
        o = oos.get(r["strategy"], {})
        p = r["permutation"]["p_value"] if r["permutation"] else 1.0
        ci_lo = r["bootstrap"]["ci_lo"]
        pass_bonf = p < bonf
        pass_ci = ci_lo > 0
        pass_oos = bool(o.get("consistent")) and o.get("oos_ci_lo", -1) > 0
        n_pass = sum([pass_bonf, pass_ci, pass_oos])
        verdict = "生产可用" if n_pass == 3 else "仅观察" if n_pass == 2 else "不可用"
        final.append({"strategy": r["strategy"], "n": r["n"], "roi": r["roi"], "hit": r["hit"],
                      "ci_lo": ci_lo, "ci_hi": r["bootstrap"]["ci_hi"], "perm_p": p,
                      "pass_bonferroni": pass_bonf, "pass_ci": pass_ci, "pass_oos": pass_oos,
                      "verdict": verdict, **{f"oos_{k}": v for k, v in o.items()}})
        log.info(f"  {r['strategy']:<22s} ROI={r['roi']:>+7.2%} "
                 f"CI下界={ci_lo:>+7.2%} p={p:.4f} OOS={'一致' if pass_oos else '不一致'} -> {verdict}")

    usable = [f["strategy"] for f in final if f["verdict"] == "生产可用"]
    watch = [f["strategy"] for f in final if f["verdict"] == "仅观察"]
    log.info(f"\n  生产可用: {usable if usable else '无'}")
    log.info(f"  仅观察  : {watch if watch else '无'}")

    out = {"pool_n": len(df), "cup_n": int(df["is_cup"].sum()),
           "ou_overround_mean": round(float(df["ou_overround"].mean()), 4),
           "direction_compare": {"cup_under": su, "cup_over": so, "noncup_over": snc},
           "detail": rows, "oos": oos, "by_line": by_line, "equity": eq,
           "final": final, "usable": usable, "watch_only": watch}
    path = os.path.join(OUT_DIR, "cup_over_edge_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"\n报告已存: {path}")
    return out


if __name__ == "__main__":
    main()
