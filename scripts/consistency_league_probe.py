"""
consistency_league_probe.py — 真 OOS 读片考卷 (深度方向 续九)

承接 续五/续七/续八: 候选深度假说清单还剩两条「单源读片方法」未过考卷:
  H-C  欧亚内部一致性 (AH 热门方 == 1X2 热门方 → 押共识方, 否则不押)
  H-L  联赛背景条件化 (用联赛样本量作「市场流动性/信息效率」代理:
       高量(好价)押热门, 低量(薄价/obscure)押冷门)

两条都不需要跨庄多庄数据, 复用 diagnostic_probe 诚实层:
  - 真相 = match_outcomes.result (SSoT, 剔虚拟)
  - 严格层 LEFT JOIN matches 剔 score_missing=1 假 0-0 (IR-04)
  - 时间切分 kickoff >= 2023-01-01 才计 OOS
  - 不读任何未来信息, 只用初盘 1X2 + AH 结构

诚实口径 (IR-20/IR-30):
  - 方法炸了/无信号 → 不押 (stake=0), 不退化成反向硬押
  - 对照市场基线 (MarketFavorite 方向准确率 + ROI)
  - ROI 用 bootstrap 95% CI; +EV 唯一判据 = CI 不跨零 (IR-17)

注意: events.db league 字段污染严重 (838 个不同值, 含篮球联赛 "IPBL篮球专业组",
      727 条空 league, 大量友谊赛/杯赛碎片) → 联赛「名称」不可信;
      故 H-L 只用「样本量」作流动性代理, 名称不解释。
"""
from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys = __import__("sys")
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

from diagnostic_probe import DiagnosticProbe, MarketFavorite, SIDES  # noqa: E402

CUTOFF = "2023-01-01"
DB = os.path.join(ROOT, "data", "events.db")
N_BOOT = 2000
SEED = 42


# ═════════════════════════════════════════════════════════════════════
# 辅助
# ═════════════════════════════════════════════════════════════════════
def ah_favorite(rep):
    """AH 热门方 (给盘口的一方)。line<0→主, line>0→客, 0/None→无方向(平手)。"""
    if rep.ah_line is None or rep.ah_home_odds is None:
        return None
    if rep.ah_line < 0:
        return "home"
    if rep.ah_line > 0:
        return "away"
    return None  # 平手盘, 无方向热门


def _to_idx(result):
    return {"home": 0, "draw": 1, "away": 2}.get((result or "").strip().lower())


def _bs_roi(odds_arr, wins, n_boot, rng):
    n = len(wins)
    if n == 0:
        return {"roi": float("nan"), "ci_low": float("nan"), "ci_high": float("nan"),
                "n": 0}
    rois = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        net = np.where(wins[idx], odds_arr[idx] - 1.0, -1.0).sum()
        rois[b] = net / n
    return {"roi": float(rois.mean()),
            "ci_low": float(np.percentile(rois, 2.5)),
            "ci_high": float(np.percentile(rois, 97.5)),
            "n": int(n)}


def _acc_ci(preds_idx, truth, n_boot, rng):
    n = len(truth)
    if n == 0:
        return float("nan"), [float("nan"), float("nan")]
    acc = float((preds_idx == truth).mean())
    bs = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        bs[b] = float((preds_idx[idx] == truth[idx]).mean())
    return acc, [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]


# ═════════════════════════════════════════════════════════════════════
# 主流程
# ═════════════════════════════════════════════════════════════════════
def main():
    rng = np.random.default_rng(SEED)
    probe = DiagnosticProbe(db_path=DB)
    reports, truths = probe._load(CUTOFF, strict_clean=True)
    n_total = len(reports)
    truth = np.array(truths, dtype=int)
    print(f"[load] 真 OOS 样本 n={n_total} (cutoff>={CUTOFF}, 严格层开)")

    # ── 市场基线 (MarketFavorite = 隐含概率最高方) ──
    mkt_idx = np.array([_to_idx(r.favorite) for r in reports])
    mkt_acc, mkt_ci = _acc_ci(mkt_idx, truth, N_BOOT, rng)
    mkt_odds = np.array([getattr(reports[i], f"{SIDES[mkt_idx[i]]}_odds") for i in range(n_total)])
    mkt_wins = (truth == mkt_idx)
    mkt_roi = _bs_roi(mkt_odds, mkt_wins, N_BOOT, rng)
    print(f"[基线] 市场热门 方向 {mkt_acc:.2%} CI[{mkt_ci[0]:.2%},{mkt_ci[1]:.2%}] "
          f"| ROI {mkt_roi['roi']:+.2%} CI[{mkt_roi['ci_low']:+.2%},{mkt_roi['ci_high']:+.2%}]")

    out = {
        "cutoff": CUTOFF,
        "n_total": n_total,
        "market_baseline": {
            "direction_acc": mkt_acc, "acc_ci": mkt_ci,
            "roi": mkt_roi["roi"], "roi_ci": [mkt_roi["ci_low"], mkt_roi["ci_high"]],
        },
        "notes": [],
    }

    # ════════════════ H-C 欧亚内部一致性 ════════════════
    # 共识方 = AH 热门方 与 1X2 热门方 一致时, 押该方; 否则不押。
    consensus_idx = []      # 押注方 (int) 或 None
    consensus_mask = np.zeros(n_total, dtype=bool)
    for i, r in enumerate(reports):
        af = ah_favorite(r)
        if af is None:
            consensus_idx.append(None)
            continue
        x2f = r.favorite
        if af == x2f:
            consensus_idx.append(_to_idx(af))
            consensus_mask[i] = True
        else:
            consensus_idx.append(None)

    n_consensus = int(consensus_mask.sum())
    if n_consensus > 0:
        c_idx = np.array([consensus_idx[i] for i in range(n_total) if consensus_mask[i]])
        c_truth = truth[consensus_mask]
        c_acc, c_ci = _acc_ci(c_idx, c_truth, N_BOOT, rng)
        c_odds = np.array([getattr(reports[i], f"{SIDES[consensus_idx[i]]}_odds")
                           for i in range(n_total) if consensus_mask[i]])
        c_wins = (c_truth == c_idx)
        c_roi = _bs_roi(c_odds, c_wins, N_BOOT, rng)
    else:
        c_acc, c_ci = float("nan"), [float("nan"), float("nan")]
        c_roi = {"roi": float("nan"), "ci_low": float("nan"), "ci_high": float("nan"), "n": 0}

    # 对照: 共识子集里「若反向押」的准确率 (sanity: 不应更高)
    rev_idx = np.array([2 - c_idx[i] for i in range(len(c_idx))])  # home<->away 翻转 (粗反向)
    rev_acc = float((rev_idx == c_truth).mean()) if n_consensus else float("nan")

    # 方向是否「统计上显著区别于市场」: 须 H-C 方向 CI 与市场 CI 不重叠
    # (H-C CI 下界 > 市场 CI 上界; 教科书级「显著不同」门槛, 防点估计噪声误判)
    dir_distinguishable = (not np.isnan(c_acc)) and (c_ci[0] > mkt_ci[1])
    out["H_consistency"] = {
        "n_with_ah": int(sum(1 for r in reports if ah_favorite(r) is not None)),
        "n_consensus": n_consensus,
        "coverage": n_consensus / n_total,
        "direction_acc": c_acc, "acc_ci": c_ci,
        "market_acc": mkt_acc, "market_acc_ci": mkt_ci,
        "dir_gap_pp": (c_acc - mkt_acc) * 100 if not np.isnan(c_acc) else float("nan"),
        "dir_distinguishable": bool(dir_distinguishable),
        "beats_market": bool(dir_distinguishable),
        "roi": c_roi["roi"], "roi_ci": [c_roi["ci_low"], c_roi["ci_high"]],
        "ev_established": (not np.isnan(c_roi["roi"])) and (c_roi["ci_low"] > 0),
        "reverse_acc_sanity": rev_acc,
        "verdict": ("PASS(+EV确立)" if ((not np.isnan(c_roi["roi"])) and c_roi["ci_low"] > 0)
                    else ("WEAK(方向显著但无+EV)" if dir_distinguishable
                          else "FAIL(未击败市场/未确立+EV)")),
    }
    print(f"[H-C] 共识覆盖 {n_consensus}/{n_total} ({n_consensus/n_total:.1%}) "
          f"| 方向 {c_acc:.2%} CI[{c_ci[0]:.2%},{c_ci[1]:.2%}] "
          f"| ROI {c_roi['roi']:+.2%} CI[{c_roi['ci_low']:+.2%},{c_roi['ci_high']:+.2%}] "
          f"| 反向sanity {rev_acc:.2%}")

    # ════════════════ H-L 联赛背景条件化 (样本量=流动性代理) ════════════════
    # 联赛样本量分布 → 高量(好价)/低量(薄价)。条件读法: 高量押热门, 低量押冷门。
    vol = {}
    for r in reports:
        lg = r.league or ""
        vol[lg] = vol.get(lg, 0) + 1
    # 以「样本量中位数」切分 (数据驱动, 不依赖联赛名可信度)
    vols = np.array(list(vol.values()))
    med = float(np.median(vols))
    hi_leagues = {lg for lg, c in vol.items() if c >= med}
    lo_leagues = {lg for lg, c in vol.items() if c < med}
    out["H_league"] = {"n_distinct_leagues": len(vol),
                       "median_volume": med,
                       "n_hi": len(hi_leagues), "n_lo": len(lo_leagues),
                       "contamination_note": "league 字段含非足球(如IPBL篮球)与空值727, 仅作量代理"}

    # 每桶: 市场热门准确率 / 冷门准确率 / 条件读法 ROI
    def bucket_stats(mask, bet="fav"):
        if mask.sum() == 0:
            return None
        sub_idx = np.where(mask)[0]
        t = truth[mask]
        if bet == "fav":
            pred = np.array([_to_idx(reports[i].favorite) for i in sub_idx])
        else:  # underdog = 隐含概率最低方
            pred = np.array([_to_idx(min(SIDES, key=lambda s: 1 / getattr(reports[i], f"{s}_odds")))
                             for i in sub_idx])
        acc, ci = _acc_ci(pred, t, N_BOOT, rng)
        odds = np.array([getattr(reports[i], f"{SIDES[pred[k]]}_odds") for k, i in enumerate(sub_idx)])
        wins = (t == pred)
        roi = _bs_roi(odds, wins, N_BOOT, rng)
        return {"n": int(mask.sum()), "dir_acc": acc, "acc_ci": ci,
                "roi": roi["roi"], "roi_ci": [roi["ci_low"], roi["ci_high"]]}

    hi_mask = np.array([ (r.league or "") in hi_leagues for r in reports])
    lo_mask = np.array([ (r.league or "") in lo_leagues for r in reports])

    hi_fav = bucket_stats(hi_mask, "fav")
    hi_ud = bucket_stats(hi_mask, "ud")
    lo_fav = bucket_stats(lo_mask, "fav")
    lo_ud = bucket_stats(lo_mask, "ud")
    out["H_league"]["hi_volume_fav"] = hi_fav
    out["H_league"]["hi_volume_underdog"] = hi_ud
    out["H_league"]["lo_volume_fav"] = lo_fav
    out["H_league"]["lo_volume_underdog"] = lo_ud

    # 条件读法: 高量→热门, 低量→冷门, 合并 ROI (无信号桶不计)
    cond_idx = np.full(n_total, -1, dtype=int)
    for i in range(n_total):
        lg = reports[i].league or ""
        if lg in hi_leagues:
            cond_idx[i] = _to_idx(reports[i].favorite)
        elif lg in lo_leagues:
            cond_idx[i] = _to_idx(min(SIDES, key=lambda s: 1 / getattr(reports[i], f"{s}_odds")))
    cond_mask = cond_idx >= 0
    if cond_mask.sum() > 0:
        cidx = cond_idx[cond_mask]
        ct = truth[cond_mask]
        cacc, cci = _acc_ci(cidx, ct, N_BOOT, rng)
        codds = np.array([getattr(reports[i], f"{SIDES[cidx[k]]}_odds") for k, i in enumerate(np.where(cond_mask)[0])])
        cwins = (ct == cidx)
        croi = _bs_roi(codds, cwins, N_BOOT, rng)
        out["H_league"]["conditioned_method"] = {
            "n": int(cond_mask.sum()), "dir_acc": cacc, "acc_ci": cci,
            "roi": croi["roi"], "roi_ci": [croi["ci_low"], croi["ci_high"]],
            "market_acc": mkt_acc, "market_acc_ci": mkt_ci,
            "dir_gap_pp": (cacc - mkt_acc) * 100,
            "dir_distinguishable": bool(cci[0] > mkt_ci[1]),
            "beats_market": bool(cci[0] > mkt_acc),
            "ev_established": croi["ci_low"] > 0,
            "verdict": ("PASS(+EV确立)" if croi["ci_low"] > 0
                        else ("WEAK(方向显著但无+EV)" if cci[0] > mkt_acc
                              else "FAIL(无增量/无+EV)")),
        }
        print(f"[H-L] 条件读法 n={int(cond_mask.sum())} 方向 {cacc:.2%} "
              f"ROI {croi['roi']:+.2%} CI[{croi['ci_low']:+.2%},{croi['ci_high']:+.2%}]")
    else:
        out["H_league"]["conditioned_method"] = None

    print(f"[H-L] 高量({hi_mask.sum()}) 热门 {hi_fav['dir_acc']:.2%}/冷门 {hi_ud['dir_acc']:.2%} | "
          f"低量({lo_mask.sum()}) 热门 {lo_fav['dir_acc']:.2%}/冷门 {lo_ud['dir_acc']:.2%}")

    # ── 诚实总判定 ──
    verdict_lines = []
    hc = out["H_consistency"]
    hl = out["H_league"].get("conditioned_method") or {}
    if hc.get("ev_established"):
        verdict_lines.append("H-C 欧亚一致: +EV 确立即真信号, 可建仓(需过五道关)")
    elif hc.get("dir_distinguishable"):
        verdict_lines.append(f"H-C 欧亚一致: 方向+{hc['dir_gap_pp']:.2f}pp 显著但无+EV(ROI {hc['roi']:+.2%}) → 弱信号不建仓 (IR-17)")
    else:
        verdict_lines.append(f"H-C 欧亚一致: 方向+{hc.get('dir_gap_pp', float('nan')):.2f}pp 但CI重叠=噪声, ROI {hc['roi']:+.2%} 无+EV → 不建仓 (IR-30)")
    if hl.get("ev_established"):
        verdict_lines.append("H-L 联赛条件化: +EV 确立即真信号")
    elif hl.get("dir_distinguishable"):
        verdict_lines.append(f"H-L 联赛条件化: 方向 {hl['dir_gap_pp']:+.2f}pp 显著但无+EV → 弱信号不建仓 (IR-17)")
    else:
        verdict_lines.append(f"H-L 联赛条件化: 方向 {hl.get('dir_gap_pp', float('nan')):+.2f}pp 无增量, ROI {hl.get('roi', float('nan')):+.2%} 无+EV → 不建仓 (IR-30)")
    out["verdict"] = verdict_lines

    # 写出
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jpath = os.path.join(ROOT, "scripts", f"consistency_league_probe_report_{ts}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n" + "=" * 70)
    print("真 OOS 考卷 — 续九 (H-C 欧亚一致 / H-L 联赛条件化)")
    print("=" * 70)
    print(f"样本 n={n_total} | 市场基线方向 {mkt_acc:.2%} ROI {mkt_roi['roi']:+.2%}")
    print(" | ".join(verdict_lines))
    print(f"报告: {jpath}")
    return out, jpath


if __name__ == "__main__":
    main()
