"""盈利验证台审计: 检验 KNN EDGE 是否为去水(devig)方法制造的伪影。

背景 (2026-09-23 接手审计):
  验证台判定 KNN = EDGE (n=2562, ROI +5.86%, 95%CI [+1.62%, +10.10%])。
  但 ROI 随赔率档位单调递减 (<1.5:+9.7% → 3-5:+1.8%)，是教科书级
  favorite-longshot bias (FLB) 特征。项目 SSoT pipeline/odds_math.py 自己
  在 devig_power 注释里写明"比比例法更抑 FLB" —— 而验证台用比例法(devig_n/devig3)。

核心假设:
  比例法去水把抽水按概率比例均摊 → 热门去水后概率被高估(赔率被抬高)
  → "买热门"这一零信息机械规则本身就能产生虚正 ROI。
  KNN 78.7% 选最短赔率(热门)，其 +5.86% 可能主要来自该偏差而非预测能力。

检验设计 (全部零信息机械对照, 不需要原始赔率):
  A. 在 KNN 下注的同一批比赛上，跑純机械规则的 ROI:
     - shortest: 总买最短(devig)赔率 → 热门
     - longest:  总买最长           → 冷门
     - random:   均匀随机选边 (蒙特卡洛)
  B. market_baseline 机制确认 (是否按市场概率抽样; 其 ROI 应≈0)
  C. KNN - 机械热门 配对差 (配对 t/自助法)
  D. ROI ~ log(odds) 单调性斜率

只读 verification.db；输出 reports/knn_edge_devig_audit.json + .md
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "verification.db"
OUT_JSON = ROOT / "reports" / "knn_edge_devig_audit.json"
OUT_MD = ROOT / "reports" / "knn_edge_devig_audit.md"


def load():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("select * from verification_ledger")]
    con.close()
    return rows


def roi(payoffs):
    if not payoffs:
        return None
    return sum(payoffs) / len(payoffs)


def ci95(payoffs, n_boot=4000, seed=42):
    """自助法 95% CI (均值)。"""
    if len(payoffs) < 5:
        return None
    rng = random.Random(seed)
    n = len(payoffs)
    means = []
    for _ in range(n_boot):
        s = sum(payoffs[rng.randrange(n)] for _ in range(n))
        means.append(s / n)
    means.sort()
    return [means[int(0.025 * len(means))], means[int(0.975 * len(means)) - 1]]


def se_mean(payoffs):
    if len(payoffs) < 2:
        return None
    m = sum(payoffs) / len(payoffs)
    v = sum((x - m) ** 2 for x in payoffs) / (len(payoffs) - 1)
    return math.sqrt(v / len(payoffs))


def band(o):
    if o < 1.5:
        return "<1.5"
    if o < 2:
        return "1.5-2"
    if o < 3:
        return "2-3"
    if o < 5:
        return "3-5"
    return ">5"


def payoff_of(r, outcome):
    """按 ledger 的 devig 赔率计算选定方向的纸盘收益(复算, 校验 ledger.payoff)。"""
    d = {"home": r["devig_h"], "draw": r["devig_d"], "away": r["devig_a"]}
    o = d[outcome]
    if o is None:
        return None
    win = r["settled_outcome"]
    if win is None:
        return None
    return (o - 1.0) if win == outcome else -1.0


def main():
    rows = load()
    settled = [r for r in rows if r["settled_outcome"] is not None]
    out = {
        "generated_at_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "ledger_rows": len(rows),
        "settled_rows": len(settled),
        "by_model": dict(Counter(r["model_source"] for r in settled)),
    }

    # --- 校验 ledger.payoff 与 devig 赔率自洽 ---
    mism = 0
    for r in settled:
        p = payoff_of(r, r["chosen_outcome"])
        if p is not None and r["payoff"] is not None and abs(p - r["payoff"]) > 1e-6:
            mism += 1
    out["payoff_recompute_mismatch"] = mism

    # --- A. KNN 同批比赛上的零信息机械基准 ---
    knn = [
        r
        for r in settled
        if r["model_source"] == "KNN" and r["settled_outcome"] is not None
    ]
    out["knn_n"] = len(knn)

    mech = {}
    pay_short, pay_long, pay_draw = [], [], []
    for r in knn:
        trio = {"home": r["devig_h"], "draw": r["devig_d"], "away": r["devig_a"]}
        if any(v is None for v in trio.values()):
            continue
        sh = min(trio, key=lambda k: trio[k])
        lg = max(trio, key=lambda k: trio[k])
        pay_short.append(payoff_of(r, sh))
        pay_long.append(payoff_of(r, lg))
        pay_draw.append(payoff_of(r, "draw"))
    mech["shortest_favorite"] = {
        "n": len(pay_short),
        "roi": roi(pay_short),
        "ci95": ci95(pay_short),
    }
    mech["longest_longshot"] = {
        "n": len(pay_long),
        "roi": roi(pay_long),
        "ci95": ci95(pay_long),
    }
    mech["always_draw"] = {"n": len(pay_draw), "roi": roi(pay_draw), "ci95": ci95(pay_draw)}

    # 均匀随机选边 (蒙特卡洛 200 次, 取均值分布)
    rng = random.Random(7)
    rand_rois = []
    for _ in range(200):
        ps = []
        for r in knn:
            trio = {"home": r["devig_h"], "draw": r["devig_d"], "away": r["devig_a"]}
            if any(v is None for v in trio.values()):
                continue
            ps.append(payoff_of(r, rng.choice(list(trio))))
        rand_rois.append(roi(ps))
    rand_rois.sort()
    mech["uniform_random"] = {
        "n": len(knn),
        "roi_mean": sum(rand_rois) / len(rand_rois),
        "roi_lo": rand_rois[int(0.025 * len(rand_rois))],
        "roi_hi": rand_rois[int(0.975 * len(rand_rois)) - 1],
    }
    out["mechanical_baselines_on_knn_universe"] = mech

    # --- KNN 自身 ---
    knn_pay = [r["payoff"] for r in knn]
    out["knn_actual"] = {
        "n": len(knn_pay),
        "roi": roi(knn_pay),
        "ci95": ci95(knn_pay),
        "se": se_mean(knn_pay),
    }

    # --- C. 配对差 KNN - 机械热门 (同一场配对) ---
    diffs = []
    for r in knn:
        trio = {"home": r["devig_h"], "draw": r["devig_d"], "away": r["devig_a"]}
        if any(v is None for v in trio.values()):
            continue
        sh = min(trio, key=lambda k: trio[k])
        diffs.append(r["payoff"] - payoff_of(r, sh))
    out["paired_knn_minus_shortest"] = {
        "n": len(diffs),
        "mean_diff": roi(diffs),
        "ci95": ci95(diffs),
        "se": se_mean(diffs),
        "interpretation": "若 CI 含 0 → KNN 相对'无脑买热门'无超额, EDGE 为 devig 伪影",
    }

    # --- B. market_baseline 机制 ---
    mb = [r for r in settled if r["model_source"] == "market_baseline"]
    mb_pay = [r["payoff"] for r in mb]
    # 确认是否按市场概率抽样: 比较 chosen 分布 vs devig 概率均值
    chosen = Counter(r["chosen_outcome"] for r in mb)
    avg_p = {
        k: sum(1.0 / r[f"devig_{k[0]}"] for r in mb) / len(mb) for k in ("home", "draw", "away")
    }
    out["market_baseline"] = {
        "n": len(mb),
        "roi": roi(mb_pay),
        "ci95": ci95(mb_pay),
        "chosen_share": {k: chosen[k] / len(mb) for k in ("home", "draw", "away")},
        "avg_devig_prob": avg_p,
        "note": "若 chosen_share≈avg_devig_prob → 按市场概率抽样的零信息策略; 其 ROI 应≈0, 显著>0 即 devig 残差偏差",
    }

    # --- D. 单调性: ROI ~ log(odds) 斜率 (KNN) ---
    xs = [math.log(r["chosen_dec_odds"]) for r in knn]
    ys = [r["payoff"] for r in knn]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else None
    resid = [y - (my + slope * (x - mx)) for x, y in zip(xs, ys)]
    se_slope = se_mean(resid) / math.sqrt(sxx) if sxx else None
    out["roi_vs_logodds_slope"] = {
        "slope": slope,
        "se": se_slope,
        "t": (slope / se_slope) if se_slope else None,
        "interpretation": "显著负斜率 = ROI 随赔率升高而降 → FLB/devig 伪影特征",
    }

    # --- KNN 分档 ROI 复算 ---
    bands = {}
    for k in ("<1.5", "1.5-2", "2-3", "3-5", ">5"):
        sel = [r["payoff"] for r in knn if band(r["chosen_dec_odds"]) == k]
        if sel:
            bands[k] = {"n": len(sel), "roi": roi(sel), "ci95": ci95(sel)}
    out["knn_roi_by_odds_band"] = bands

    # --- 判定 ---
    pd = out["paired_knn_minus_shortest"]
    mbv = out["market_baseline"]
    artifact = bool(
        pd["ci95"]
        and pd["ci95"][0] <= 0 <= pd["ci95"][1]
        and mbv["ci95"]
        and mbv["ci95"][0] > 0
    )
    out["verdict"] = {
        "devig_artifact_confirmed": artifact,
        "summary": (
            "KNN EDGE 判定为去水方法伪影(favorite-longshot bias), 非可交易预测能力"
            if artifact
            else "证据不足以推翻 EDGE; 需原始赔率 + 幂法去水重算复核"
        ),
        "required_fix": "验证台改用 devig_power(幂法, 项目 SSoT 已自带且注明'更抑 FLB')重算全部账本; 或账本增存原始赔率列以支持多方法重算",
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # Markdown
    def f(v, p=4):
        return "n/a" if v is None else f"{v:+.{p}f}"

    def ci(c):
        return "n/a" if not c else f"[{c[0]:+.4f}, {c[1]:+.4f}]"

    L = []
    L.append("# KNN EDGE 去水伪影审计")
    L.append("")
    L.append("> 目的: 检验验证台唯一过线判定 (KNN=EDGE) 是否由比例法去水的 favorite-longshot bias 制造。")
    L.append("")
    L.append(f"- 账本总行 {out['ledger_rows']} / 已结算 {out['settled_rows']}")
    L.append(f"- payoff 重算不一致行: {mism} (0=账本自洽)")
    L.append("")
    L.append("## 1. 零信息机械基准 (KNN 同一批比赛)")
    L.append("")
    L.append("| 策略 | n | ROI | 95%CI |")
    L.append("|---|---|---|---|")
    L.append(
        f"| KNN 实际 | {out['knn_actual']['n']} | {f(out['knn_actual']['roi'])} | {ci(out['knn_actual']['ci95'])} |"
    )
    L.append(
        f"| **无脑买最短(热门)** | {mech['shortest_favorite']['n']} | {f(mech['shortest_favorite']['roi'])} | {ci(mech['shortest_favorite']['ci95'])} |"
    )
    L.append(
        f"| 无脑买最长(冷门) | {mech['longest_longshot']['n']} | {f(mech['longest_longshot']['roi'])} | {ci(mech['longest_longshot']['ci95'])} |"
    )
    L.append(
        f"| 无脑买平局 | {mech['always_draw']['n']} | {f(mech['always_draw']['roi'])} | {ci(mech['always_draw']['ci95'])} |"
    )
    L.append(
        f"| 均匀随机选边 | {mech['uniform_random']['n']} | {f(mech['uniform_random']['roi_mean'])} | [{mech['uniform_random']['roi_lo']:+.4f}, {mech['uniform_random']['roi_hi']:+.4f}] |"
    )
    L.append("")
    L.append("## 2. 配对差: KNN − 无脑买热门 (同一场)")
    L.append("")
    L.append(
        f"- 均值差 {f(pd['mean_diff'])}  CI {ci(pd['ci95'])}  (n={pd['n']})"
    )
    L.append(f"- 判读: {pd['interpretation']}")
    L.append("")
    L.append("## 3. market_baseline (零信息对照)")
    L.append("")
    L.append(f"- n={mbv['n']}  ROI {f(mbv['roi'])}  CI {ci(mbv['ci95'])}")
    L.append(f"- 选边占比 {json.dumps(mbv['chosen_share'], ensure_ascii=False)}")
    L.append(f"- 平均去水概率 {json.dumps({k: round(v,4) for k,v in mbv['avg_devig_prob'].items()}, ensure_ascii=False)}")
    L.append(f"- 注: {mbv['note']}")
    L.append("")
    L.append("## 4. ROI 对赔率的单调性 (FLB 特征)")
    L.append("")
    s = out["roi_vs_logodds_slope"]
    L.append(f"- 斜率 {f(s['slope'])}  SE {f(s['se'])}  t {f(s['t'], 2)}")
    L.append(f"- 判读: {s['interpretation']}")
    L.append("")
    L.append("| 赔率档 | n | ROI | 95%CI |")
    L.append("|---|---|---|---|")
    for k, v in out["knn_roi_by_odds_band"].items():
        L.append(f"| {k} | {v['n']} | {f(v['roi'])} | {ci(v['ci95'])} |")
    L.append("")
    L.append("## 判定")
    L.append("")
    L.append(f"- **devig 伪影成立: {out['verdict']['devig_artifact_confirmed']}**")
    L.append(f"- {out['verdict']['summary']}")
    L.append(f"- 必修: {out['verdict']['required_fix']}")
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
