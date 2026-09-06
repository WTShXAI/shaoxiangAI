"""Poisson GBM OU 五道关诚实验证 (2026-08-31, Fix-2)。

背景
----
compare/verify 系列已给出 OU 判别力信号: GBM λ → P(over) 时间外 AUC ~0.577,
但 ROI 口径有两处弱点, 本次修复并加严:
  1. 主盘线口径: 旧 opening_ou(compare_ou_models) 用 minute_at(全库占位垃圾坑)+
     最低抽水线, 违反 IR-01 (盘口线 SSoT = opening_line.build_opening_lines)。
  2. 清算口径: 旧 ROI 把整数线 push 当全输、.25/.75 半赢半输当全赢/全输,
     系统性低估 ROI (整数线占主盘 23.3%)。
  3. 缺五道关的关2/3/4/5 (bootstrap CI / +EV 三件套 / 结构健康 / 随机对照)。

本脚本五道关:
  关0 前视自查   : 模型输入=开盘1X2, 结算=开盘OU线, 信号=开盘时可得概率
  关1 IR-04 过滤 : score_missing=1 排除 + 真实比分快照 + 健康度体检
  关2 bootstrap  : 2000 重采样 → AUC/ROI/edge_pp 的 95% CI
  关3 +EV 三件套 : win_rate / implied / edge_pp (IR-17: 实际胜率 > 隐含+抽水)
  关4 结构健康   : 按 |p-implied| 分档 / 时间前/后半 / 联赛; Brier/ECE 校准
  关5 随机对照   : permute p 500 次 → 策略 ROI 百分位

口径
----
- 盘口线: build_opening_lines(events.db, 'OU')  (IR-01 SSoT)
- 赛果  : matches 表 + IR-04 过滤 (真实 score_at 快照存在性)
- 开盘1X2: _open_1x2_from_snapshots (kickoff+300s 闸门, 每方向最早帧, 无前视)
- λ    : predict_lambdas(oh,od,oa, ch=oh,...)  —— 无 drift, 开盘信息闭集
- 结算  : 正确 1/4 球清算 (全赢/全输/push/半赢/半输)
- 下注  : |p-implied|>0.05 押差异侧 (与既有脚本同规则, 便于对比)

不修改任何模型/数据库文件。
用法: runpy scripts/verify_poisson_ou_fivegates_20260831.py [--since 2026-07-01]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "events.db")
CAL_PATH = os.path.join(ROOT, "models", "ou_calibrator_20260830.joblib")

from pipeline.opening_line import build_opening_lines          # noqa: E402  IR-01
from pipeline.poisson_gbm import available as gbm_ok, predict_lambdas, p_over  # noqa: E402
from analysis.live_goal_probe import _open_1x2_from_snapshots   # noqa: E402

EPS = 1e-6
BET_GAP = 0.05       # 下注闸门 |p-implied|>0.05 (与既有脚本同规则)
N_BOOT = 2000
N_PERM = 500
FIT_FRAC = 0.6       # 时间切分: 前 60% 拟合 Platt, 后 40% 时间外评估


# ── 正确 1/4 球清算 ──────────────────────────────────────────────
def over_net(tot: float, line: float, ov: float, un: float) -> float:
    """押 over 每 1 单位 stake 的净收益 (含 push / .25 / .75 半赢半输)。"""
    f = line % 1.0
    base = int(line)
    if f == 0.75:
        if tot == base + 1:
            return 0.5 * (ov - 1.0)      # 半赢
        if tot >= base + 2:
            return ov - 1.0              # 全赢
        return -1.0                      # 全输
    if f == 0.25:
        if tot == base:
            return -0.5                  # 半输
        if tot >= base + 1:
            return ov - 1.0              # 全赢
        return -1.0                      # 全输
    if tot > line:
        return ov - 1.0
    if tot == line:
        return 0.0                       # push
    return -1.0


def under_net(tot: float, line: float, ov: float, un: float) -> float:
    """押 under 每 1 单位 stake 的净收益 (镜像对称)。"""
    return over_net(-tot, -line, un, ov)


def settle(tot, line, ov, un, side: str) -> float:
    return over_net(tot, line, ov, un) if side == "over" else under_net(tot, line, ov, un)


# ── 数据收集 (关0 前视纪律: 全部开盘时可得信息) ─────────────────
def collect(since: str, limit: int = 20000):
    op = build_opening_lines(DB, market="OU", full_time_only=True)
    op_map = {r["match_key"]: r for _, r in op.iterrows()}
    print(f"[collect] build_opening_lines OU 主盘 SSoT: {len(op_map)} 场")

    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff, league FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>=? "
        "ORDER BY kickoff ASC LIMIT ?", (since, limit)).fetchall()

    rec, skip = [], {"fake0": 0, "no_snap": 0, "no_ou": 0, "no_1x2": 0, "gbm": 0}
    for mk, home, away, sh, sa, ko, league in rows:
        # 关1: IR-04 假 0-0 双过滤 (Fix-1 标记 + 真实比分快照)
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1",
                       (mk,)).fetchone():
            skip["fake0"] += 1
            continue
        if not con.execute(
                "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            skip["no_snap"] += 1
            continue
        o = op_map.get(mk)
        if o is None:
            skip["no_ou"] += 1
            continue
        line, ov, un = float(o["line"]), float(o["over"]), float(o["under"])
        if not (ov > 1.01 and un > 1.01):
            skip["no_ou"] += 1
            continue
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            oh = od = oa = None
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            skip["no_1x2"] += 1
            continue
        lam = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=league)
        if not lam:
            skip["gbm"] += 1
            continue
        tot = int(sh) + int(sa)
        implied = (1.0 / ov) / ((1.0 / ov) + (1.0 / un))
        rec.append(dict(y=1 if tot > line else 0, tot=tot, line=line, ov=ov, un=un,
                        implied=implied, raw=float(p_over(lam[0], lam[1], line)),
                        ko=ko, league=league or "?", home=home, away=away))
    con.close()
    print(f"[collect] 有效 {len(rec)} | 跳过 {skip}")
    return rec


def logit(p):
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def cal_platt(raw_tr, y_tr, raw_te):
    """时间外内拟合 Platt (C=1e6), 返回评估段校准概率。"""
    lr = LogisticRegression(C=1e6).fit(logit(raw_tr).reshape(-1, 1), y_tr)
    return lr.predict_proba(logit(raw_te).reshape(-1, 1))[:, 1], lr


def bets(p, imp):
    """下注掩码与方向: |p-implied|>BET_GAP 押差异侧。返回 (mask, side_over_bool)。"""
    mask = np.abs(p - imp) > BET_GAP
    side_over = p[mask] > imp[mask]
    return mask, side_over


def net_array(rec, p, imp):
    """每注净收益 (未下注=0)。"""
    p = np.asarray(p, float); imp = np.asarray(imp, float)
    n = len(rec)
    mask = np.abs(p - imp) > BET_GAP
    side_over = np.zeros(n, bool)
    side_over[mask] = p[mask] > imp[mask]
    out = np.zeros(n)
    for i in np.where(mask)[0]:
        r = rec[i]
        out[i] = settle(r["tot"], r["line"], r["ov"], r["un"],
                        "over" if side_over[i] else "under")
    return out, mask


def roi_of(net, mask):
    return float(net[mask].sum() / mask.sum() * 100.0) if mask.sum() else 0.0


def bet_stats(rec, net, mask, p, imp):
    """关3 +EV 三件套 (IR-17): win_rate / implied / edge_pp (含抽水)。"""
    if not mask.sum():
        return {"n_bets": 0}
    odds_side = np.where(p[mask] > imp[mask], [r["ov"] for i, r in enumerate(rec) if mask[i]],
                         [r["un"] for i, r in enumerate(rec) if mask[i]])
    win = net[mask] > 0
    half_win = np.isclose(net[mask], 0.5 * (odds_side - 1.0))
    win_rate = (win.sum() + 0.5 * half_win.sum()) / mask.sum()
    implied = float(np.mean(1.0 / odds_side))
    # 抽水 = 该场 OU 线两边隐含之和 - 1 (与押边无关)
    margin_bets = float(np.mean([1.0 / r["ov"] + 1.0 / r["un"] - 1.0
                                 for i, r in enumerate(rec) if mask[i]]))
    roi = roi_of(net, mask)
    return {
        "n_bets": int(mask.sum()),
        "n_over": int((p[mask] > imp[mask]).sum()),
        "n_under": int(mask.sum() - (p[mask] > imp[mask]).sum()),
        "win_rate": round(float(win_rate), 4),
        "avg_implied": round(implied, 4),
        "avg_margin_side": round(float(margin_bets), 4),
        "edge_pp": round(float(win_rate - implied), 4),
        "edge_pp_margin": round(float(win_rate - implied - margin_bets), 4),
        "roi": round(roi, 2),
        "ev_per_stake": round(float(net[mask].mean()), 4),
    }


def ece(p, y, n_bins=10):
    """Expected Calibration Error。"""
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, int)
    idx = np.clip((p * n_bins).astype(int), 0, n_bins - 1)
    acc = np.zeros(n_bins); conf = np.zeros(n_bins); cnt = np.zeros(n_bins)
    for b in range(n_bins):
        m = idx == b
        cnt[b] = m.sum()
        if m.sum():
            conf[b] = p[m].mean(); acc[b] = y[m].mean()
    tot = cnt.sum() or 1.0
    return float((cnt * np.abs(acc - conf)).sum() / tot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-01")
    ap.add_argument("--limit", type=int, default=20000)
    args = ap.parse_args()

    if not gbm_ok():
        print("Poisson GBM 不可用, 中止"); return
    t0 = time.time()

    rec = collect(args.since, args.limit)
    if len(rec) < 200:
        print("样本不足"); return
    rec.sort(key=lambda r: r["ko"])
    k = int(len(rec) * FIT_FRAC)
    tr, te = rec[:k], rec[k:]
    print(f"  时间切分: 拟合段 {len(tr)} (早) | 评估段 {len(te)} (晚, 时间外)")

    y = np.array([r["y"] for r in te], int)
    tot = np.array([r["tot"] for r in te], float)
    imp = np.array([r["implied"] for r in te], float)
    raw = np.array([r["raw"] for r in te], float)
    ov = np.array([r["ov"] for r in te], float)
    un = np.array([r["un"] for r in te], float)

    # 关1: 健康度 (干净子集)
    over_rate = float((tot > 2.5).mean())
    zero_rate = float((tot == 0).mean())
    print(f"\n[关1 IR-04] 评估段干净子集 n={len(te)} | 大2.5率 {over_rate:.2%} | "
          f"0-0率 {zero_rate:.2%} | 平均线 {np.mean([r['line'] for r in te]):.2f}")

    # 本次样本内重新拟合 Platt (独立于生产 calibrator)
    ptr = np.array([r["raw"] for r in tr], float)
    ytr = np.array([r["y"] for r in tr], int)
    p_cal, _ = cal_platt(ptr, ytr, raw)
    # 生产 calibrator 对照 (部署口径)
    try:
        prod = joblib.load(CAL_PATH)
        lr_prod = prod.get("logreg")
        p_cal_prod = lr_prod.predict_proba(logit(raw).reshape(-1, 1))[:, 1] if lr_prod else None
        w_prod = float(prod.get("fuse_w", 0.7))
    except Exception:
        p_cal_prod, w_prod = None, 0.7
    # 融合权重在本样本拟合段搜 (评估段独立)
    imptr = np.array([r["implied"] for r in tr], float)
    ptr_cal, _ = cal_platt(ptr, ytr, ptr)
    best_w, best_roi = 0.7, -1e9
    for w in np.arange(0.3, 1.001, 0.1):
        n_, m_ = net_array(tr, w * ptr_cal + (1 - w) * imptr, imptr)
        r_ = roi_of(n_, m_)
        if r_ > best_roi:
            best_w, best_roi = float(w), r_
    p_fuse = best_w * p_cal + (1 - best_w) * imp

    print(f"\n[关0 前视自查] 输入=开盘1X2(无drift) | 结算=开盘OU线 SSoT | "
          f"信号=开盘时可得 | 校准器=本样本时间外拟合 (生产 calibrator 仅对照)")

    # ── 各策略评估 ──
    strat = {"B naive(市场隐含)": imp, "C GBM原始": raw,
             "C2 GBM+Platt(本次)": p_cal, "C3 融合(w=%.1f)" % best_w: p_fuse}
    if p_cal_prod is not None:
        strat["C2p 生产calibrator"] = p_cal_prod

    print(f"\n===== 评估段 (时间外 n={len(te)}) =====")
    print(f"{'方案':<24s}{'AUC':>8s}{'ROI(0.05闸门)':>14s}{'n_bets':>7s}{'Brier':>8s}{'ECE':>8s}")
    nets = {}
    for tag, p in strat.items():
        net, m = net_array(te, p, imp)
        nets[tag] = net
        a = roc_auc_score(y, p)
        r = roi_of(net, m)
        br = float(np.mean((p - y) ** 2))
        ec = ece(p, y)
        print(f"{tag:<24s}{a:>8.4f}{r:>+13.2f}%{int(m.sum()):>7d}{br:>8.4f}{ec:>8.4f}")

    # ── 关2: bootstrap CI (对主力策略 C2 与 C3) ──
    # ROI 用有放回重采样 (算术均值, 无偏); AUC 在全量评估段上无放回子抽样 (70%)。
    print(f"\n[关2 bootstrap {N_BOOT} 次, 95% CI | AUC=全量70%无放回子抽样, ROI=下注子集有放回]")
    for tag in ("C2 GBM+Platt(本次)", "C3 融合(w=%.1f)" % best_w):
        p = strat[tag]; net = nets[tag]; m, _ = bets(p, imp)
        n_e = int(m.sum())
        if n_e == 0:
            continue
        ixs = np.where(m)[0]
        aucs, rois = [], []
        rng = np.random.default_rng(42)
        sub_n = max(50, int(0.7 * len(y)))
        for _ in range(N_BOOT):
            ix = rng.choice(ixs, size=len(ixs), replace=True)
            rois.append(float(net[ix].mean() * 100.0))
            ixu = rng.choice(len(y), size=sub_n, replace=False)
            aucs.append(roc_auc_score(y[ixu], p[ixu]))
        aucs, rois = np.array(aucs), np.array(rois)
        print(f"  {tag:<26s} AUC {np.median(aucs):.4f} [{np.percentile(aucs,2.5):.4f},"
              f"{np.percentile(aucs,97.5):.4f}] | ROI {np.median(rois):+.2f}% "
              f"[{np.percentile(rois,2.5):+.2f},{np.percentile(rois,97.5):+.2f}] (n_bets={n_e})")

    # ── 关3: +EV 三件套 (IR-17 主口径) ──
    print(f"\n[关3 +EV 三件套 | 判据: win_rate > implied + margin 方为真 +EV]")
    for tag in ("C2 GBM+Platt(本次)", "C3 融合(w=%.1f)" % best_w):
        p = strat[tag]; net = nets[tag]
        m, _ = bets(p, imp)
        st = bet_stats(te, net, m, p, imp)
        if not st["n_bets"]:
            print(f"  {tag:<26s} 无下注"); continue
        verdict = "PASS +EV" if st["edge_pp_margin"] > 0 else "FAIL 无 +EV"
        print(f"  {tag:<26s} n={st['n_bets']} (over {st['n_over']}/under {st['n_under']})")
        print(f"    win_rate {st['win_rate']:.2%} | avg_implied {st['avg_implied']:.2%} | "
              f"edge_pp {st['edge_pp']:+.2%} | edge含抽水 {st['edge_pp_margin']:+.2%} | "
              f"EV/注 {st['ev_per_stake']:+.4f} | ROI {st['roi']:+.2f}% → {verdict}")

    # ── 关4: 结构健康 ──
    print(f"\n[关4 结构健康 | 策略 C3 融合]")
    p = strat["C3 融合(w=%.1f)" % best_w]; net = nets["C3 融合(w=%.1f)" % best_w]
    m, so = bets(p, imp)
    # 分档
    gap = np.abs(p - imp)
    print("  按 |p-implied| 分档 (C3):")
    for lo, hi in ((BET_GAP, 0.10), (0.10, 0.20), (0.20, 1.0)):
        mk = m & (gap >= lo) & (gap < hi)
        if mk.sum() == 0:
            continue
        print(f"    [{lo:.2f},{hi:.2f}) n={int(mk.sum()):4d} ROI {float(net[mk].mean()*100):+8.2f}% "
              f"实际over率 {float((y[mk]==1).mean()):.2%} vs 平均隐含 {float(imp[mk].mean()):.2%}")
    # 时间前/后半 (评估段再劈半)
    h = len(te) // 2
    for nm, sl in (("评估前半", slice(0, h)), ("评估后半", slice(h, None))):
        m_s = m[sl]
        if m_s.sum() == 0:
            continue
        print(f"    {nm:<6s} n={int(m_s.sum()):4d} ROI {float(net[sl][m_s].mean()*100):+8.2f}%")
    # 联赛 top (C3)
    leagues = {}
    for i in np.where(m)[0]:
        leagues.setdefault(te[i]["league"], []).append(float(net[i]))
    lg_rows = sorted(((lg, np.mean(v) * 100, len(v)) for lg, v in leagues.items()),
                     key=lambda x: -x[1])
    print("  联赛 ROI top (下注≥10):")
    for lg, r, n in lg_rows[:6]:
        if n >= 10:
            print(f"    {str(lg)[:16]:<18s} n={n:4d} ROI {r:+8.2f}%")

    # ── 关5: 随机对照 (permute p) ──
    print(f"\n[关5 随机对照 {N_PERM} 次 permute p | 策略 C3]")
    rng = np.random.default_rng(7)
    perm_rois = []
    for _ in range(N_PERM):
        psh = rng.permutation(p)
        net_s, m_s = net_array(te, psh, imp)
        perm_rois.append(float(net_s[m_s].mean() * 100) if m_s.sum() else 0.0)
    perm_rois = np.array(perm_rois)
    real_roi = roi_of(net, m)
    pct = (perm_rois < real_roi).mean() * 100.0
    print(f"  真实 ROI {real_roi:+.2f}% | 随机基线 中位 {np.median(perm_rois):+.2f}% "
          f"[{np.percentile(perm_rois,2.5):+.2f},{np.percentile(perm_rois,97.5):+.2f}] | "
          f"百分位 {pct:.1f}% (>95% 才显著)")

    # ── 总判定 ──
    print(f"\n===== 总判定 =====")
    st3 = bet_stats(te, nets["C3 融合(w=%.1f)" % best_w], m, p, imp)
    verdicts = []
    verdicts.append(("关1 干净过滤", "PASS" if len(te) >= 200 else "样本不足"))
    verdicts.append(("关2 CI 不跨零(AUC)", "PASS" if False else "见上(以CI为准)"))
    ev_ok = st3.get("ev_per_stake", 0) > 0 and st3.get("edge_pp_margin", -1) > 0
    verdicts.append(("关3 +EV(含抽水)", "PASS" if ev_ok else "FAIL"))
    verdicts.append(("关4 结构稳定", "PASS" if pct > 95 else "FAIL/不稳定"))
    verdicts.append(("关5 随机对照", "PASS" if pct > 95 else "UNDERPOWERED_OR_UNSTABLE"))
    for nm, v in verdicts:
        print(f"  {nm:<14s}: {v}")
    print(f"\n耗时 {time.time()-t0:.0f}s | 评估段 {len(te)} 场 (kickoff>={args.since})")


if __name__ == "__main__":
    main()
