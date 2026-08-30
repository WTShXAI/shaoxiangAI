"""滚球 λ 二选一验证 (2026-08-30): 现有 OIP λ vs 新训 Poisson GBM λ。

只换 λ 来源, 条件化(剩余时间缩放)与领先方先验**完全一致**, 保证公平。

  A = _live_predict 的 OIP λ (从赔率反推, 现生产逻辑)
  B = Poisson GBM λ (30 万场训练, models/poisson_goals_20260830.joblib)

两者都:
  ① 取开盘 1X2 → λ_h/λ_a
  ② 滚球条件化 λ_rem = λ * time_scale(minute), 并过滤不可能比分
  ③ 应用领先方先验 (config/lead_result_prior.json, α=0.7)
  ④ 对比真实终场

⚠ 只在**干净子集**评测(排除假 0-0, 即 score_missing=1 或从未有过比分快照)。

用法: runpy scripts/compare_inplay_lambda_20260830.py [样本场数]
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")
from analysis.live_goal_probe import _parse_kickoff, _open_1x2_from_snapshots  # noqa: E402
from pipeline.cross_score import _apply_lead_prior  # noqa: E402
from pipeline.poisson_gbm import available as gbm_ok, predict_lambdas  # noqa: E402

MAX_GOALS = 10
FUSE_WS = [0.25, 0.5, 0.75]
# GBM λ 系统性偏低(实测平均总 λ 2.273 vs OIP 2.500, 差 -9%), 试全局缩放校准
CAL_KS = [1.05, 1.10, 1.15, 1.20]
LAMS = {}
ALPHA_CAP_DBG = 0.7


def norm(s):
    return str(s or '').replace(':', '-')


def dir_of(s):
    p = norm(s).split('-')
    try:
        h, a = int(p[0] or 0), int(p[1] or 0)
    except Exception:
        return None
    return 'home' if h > a else ('away' if a > h else 'draw')


def poisson_pmf(lam, k):
    import math
    lam = max(min(float(lam), 20.0), 1e-6)
    return math.exp(-lam) * lam ** k / math.factorial(k)


def oip_lambdas(oh, od, oa, league=None):
    """A 组: 复刻现生产 OIP 口径 —— 去水概率 + 联赛基准总球反推 λ。"""
    ih, id_, ia = 1.0 / oh, 1.0 / od, 1.0 / oa
    s = ih + id_ + ia
    ph, pd_, pa = ih / s, id_ / s, ia / s
    # 与 _live_predict 同量级: 基准总球 2.5, 按胜平负去水概率分配强弱
    tot = 2.5
    # 用胜率比例把总球拆给主客 (与 B2 baseline 同思路)
    ratio = ph / (ph + pa) if (ph + pa) > 0 else 0.5
    return (tot * ratio, tot * (1 - ratio))


def time_scale(minute: int) -> float:
    """剩余时间缩放（与 cross_score Phase 2 同口径）。"""
    m = max(0, min(int(minute or 0), 90))
    return max((90 - m) / 90.0, 0.0)


def inplay_dist(lam_h, lam_a, sh, sa, minute):
    """条件化: 最终比分 = 当前比分 + 剩余进球(泊松, λ 按剩余时间缩放)。"""
    ts = time_scale(minute)
    lh = lam_h * ts
    la = lam_a * ts
    dist = {}
    need_h = need_a = 0
    for i in range(need_h, MAX_GOALS + 1):
        pi = poisson_pmf(lh, i)
        if pi < 1e-9 and i > 6:
            break
        for j in range(need_a, MAX_GOALS + 1):
            pj = poisson_pmf(la, j)
            if pj < 1e-9 and j > 6:
                break
            dist[f"{sh + i}-{sa + j}"] = pi * pj
    tot = sum(dist.values()) or 1.0
    return {k: v / tot for k, v in dist.items()}


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 900
    if not gbm_ok():
        print("Poisson GBM 模型不可用, 退出")
        return
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff, league FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>='2026-08-20' "
        "ORDER BY kickoff DESC LIMIT ?", (n,)).fetchall()
    now = time.time()

    stat = defaultdict(lambda: [0, 0, 0, 0, 0.0])   # n,dir,top1,top3,brier
    skipped = defaultdict(int)
    lam_sum = defaultdict(float)

    for mk, home, away, sh, sa, ko, league in rows:
        kots = _parse_kickoff(ko)
        if not kots or now - kots < 2.5 * 3600:
            skipped['recent'] += 1
            continue
        # 干净子集
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1",
                       (mk,)).fetchone():
            skipped['fake_zero'] += 1
            continue
        if not con.execute(
                "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            skipped['no_score'] += 1
            continue

        snaps = con.execute(
            "SELECT score_at, captured_at FROM odds_snapshots WHERE match_key=? "
            "AND minute_at>0 AND score_at IS NOT NULL AND score_at!='' "
            "AND captured_at>? ORDER BY captured_at ASC", (mk, kots)).fetchall()
        if not snaps:
            skipped['no_snap'] += 1
            continue

        # 开盘 1X2
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            oh = od = oa = None
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            skipped['no_odds'] += 1
            continue

        lamA = oip_lambdas(oh, od, oa, league)
        lamB = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=league)
        if not lamB:
            skipped['gbm_fail'] += 1
            continue
        # 采样若干滚球时点 (同时取该时刻的实时 1X2, 供 GBM 使用)
        # ⚠ 初版把开盘赔率当"当前赔率"传给 GBM(ch=oh), 等于没给它滚球信息 ——
        #   那是不公平对比。滚球时赔率已反映比分与时间, 必须喂实时值。
        picked = {}
        for score_at, cap in snaps:
            elapsed_min = (cap - kots) / 60.0
            tm = elapsed_min if elapsed_min <= 45 else (
                45 if elapsed_min <= 60 else elapsed_min - 15)
            for tgt in (25, 45, 65, 80):
                if abs(tm - tgt) <= 4 and tgt not in picked:
                    picked[tgt] = (score_at, cap)
        if not picked:
            skipped['no_point'] += 1
            continue

        true_s = f"{int(sh)}-{int(sa)}"
        true_d = dir_of(true_s)

        for tgt, (score_at, cap_ts) in picked.items():
            try:
                csh, csa = (int(x) for x in str(score_at).replace(':', '-').split('-')[:2])
            except Exception:
                continue
            # 该时刻的实时 1X2 (取该快照所在 60 秒桶内最近一条)
            cur = con.execute(
                "SELECT selection, odds FROM odds_snapshots WHERE match_key=? "
                "AND market='1X2' AND odds IS NOT NULL AND odds>1.01 "
                "AND captured_at BETWEEN ? AND ? "
                "ORDER BY captured_at DESC LIMIT 12", (mk, cap_ts - 120, cap_ts + 60)).fetchall()
            cm = {}
            for sel, o in cur:
                k = str(sel).strip()
                if k in ("1", "h", "home", "Home"):
                    cm['h'] = float(o)
                elif k in ("X", "d", "draw", "Draw"):
                    cm['d'] = float(o)
                elif k in ("2", "a", "away", "Away"):
                    cm['a'] = float(o)
            lamB = predict_lambdas(oh, od, oa,
                                   ch=cm.get('h'), cd=cm.get('d'), ca=cm.get('a'),
                                   league=league)
            if not lamB:
                lamB = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=league)
            if not lamB:
                continue
            lam_sum['B'] += lamB[0] + lamB[1]
            lam_sum['A'] += lamA[0] + lamA[1]
            lam_sum['n'] += 1
            for w in FUSE_WS:
                lamF = (w * lamA[0] + (1 - w) * lamB[0],
                        w * lamA[1] + (1 - w) * lamB[1])
                LAMS[w] = lamF
            for k in CAL_KS:
                LAMS[f"k{k}"] = (lamB[0] * k, lamB[1] * k)
            for tag, lam in ([("OIP(A)", lamA), ("GBM(B)", lamB)]
                             + [(f"fuse{w}", LAMS[w]) for w in FUSE_WS]
                             + [(f"GBMx{k}", LAMS[f"k{k}"]) for k in CAL_KS]):
                dist = inplay_dist(lam[0], lam[1], csh, csa, tgt)
                dist2, note = _apply_lead_prior(dist, csh, csa, tgt)
                if not dist2:
                    continue
                top = sorted(dist2.items(), key=lambda x: -x[1])[:3]
                s = stat[tag]
                s[0] += 1
                if dir_of(top[0][0]) == true_d:
                    s[1] += 1
                if top[0][0] == true_s:
                    s[2] += 1
                if true_s in [k for k, _ in top]:
                    s[3] += 1
                s[4] += (1.0 - dist2.get(true_s, 0.0)) ** 2

    print(f"跳过: {dict(skipped)}")
    nA = max(stat['OIP(A)'][0], 1)
    nB = max(stat['GBM(B)'][0], 1)
    _n = max(lam_sum.get('n', 1), 1)
    print(f"平均总 λ (按采样点):  OIP={lam_sum['A']/_n:.3f}   GBM={lam_sum['B']/_n:.3f}")
    print()
    print(f"{'方案':<10}{'样本':>7}{'方向准确率':>12}{'top1':>10}{'top3':>10}{'Brier':>10}")
    print("-" * 60)
    for tag in ["OIP(A)"] + [f"GBMx{k}" for k in CAL_KS] + [f"fuse{w}" for w in FUSE_WS] + ["GBM(B)"]:
        nn, dh, t1, t3, br = stat[tag]
        if not nn:
            continue
        print(f"{tag:<10}{nn:>7d}{dh/nn*100:>11.1f}%{t1/nn*100:>9.1f}%"
              f"{t3/nn*100:>9.1f}%{br/nn:>10.4f}")
    a, b = stat['OIP(A)'], stat['GBM(B)']
    if a[0] and b[0]:
        print("-" * 60)
        print(f"{'差(B-A)':<10}{'':>7}{(b[1]/b[0]-a[1]/a[0])*100:>+11.1f}pp"
              f"{(b[2]/b[0]-a[2]/a[0])*100:>+9.1f}pp"
              f"{(b[3]/b[0]-a[3]/a[0])*100:>+9.1f}pp"
              f"{b[4]/b[0]-a[4]/a[0]:>+10.4f}")
        d = (b[1] / b[0] - a[1] / a[0]) * 100
        print(f"\n结论: 滚球方向准确率 {'GBM λ 更优' if d > 0 else 'OIP λ 更优' if d < 0 else '持平'} "
              f"({d:+.2f}pp)")
    con.close()


if __name__ == "__main__":
    main()
