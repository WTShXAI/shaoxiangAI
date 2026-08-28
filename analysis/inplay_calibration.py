# -*- coding: utf-8 -*-
"""
inplay_calibration.py — 滚球(in-play) 动态校准模块

针对诊断报告《足球实时预测页面诊断报告》(烈焰骑士FC vs 河王FC 1-4 / 87') 的
根因一/二/五落地：

  IR-07  Poisson λ 动态校准 —— 进球后以「当前比分 + 剩余时间」做贝叶斯收缩后验 λ(t)，
          替代赛前静态 λ 在比赛中继续输出（客队已进 4 球但 λ=0.37 的失真根因）。
  IR-19  低级别联赛概率校准 —— 按联赛加载等渗(isotonic)校准器修正 1X2/OU 概率
          （平局高估/客胜长尾低估）；无校准文件时恒等回退，不伪造。

设计原则（IR-15 改动验证铁律 / 诚实边界）：
  - 自包含：内置 Poisson 助手，不引循环依赖（live_goal_probe 可 import 本模块）。
  - 0-0 时后验≈静态，predict_fulltime_outcome 行为不退化。
  - 任何 λ 与比分严重背离（某队已进≥3 但 λ<0.5）触发一致性告警，输出标记「不可信」。

用法：
  from analysis.inplay_calibration import (
      dynamic_team_lambda, simulate_inplay_1x2,
      isotonic_calibrate_1x2, isotonic_calibrate_ou,
      lambda_consistency_flag, train_league_calibration,
  )
"""
from __future__ import annotations

import math
import os
import re
from typing import Optional, Tuple

# ── Poisson 助手（自包含） ──────────────────────────────────────────────
_MAX_FACT = 170  # 大于此用 log 近似，避免溢出


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    if k < 0:
        return 0.0
    if k > _MAX_FACT:
        # Stirling 近似，防 math.factorial 溢出
        ln = k * math.log(lam) - lam - (k * math.log(k) - k)
        return math.exp(ln)
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def _match_probs(lh: float, la: float, maxg: int = 8) -> Optional[Tuple[float, float, float]]:
    """独立泊松对抗 → (P(home>away), P(draw), P(away>home))。"""
    if lh < 0 or la < 0:
        return None
    ph = pd = pa = 0.0
    for i in range(maxg):
        pi = _poisson_pmf(i, lh)
        if pi == 0:
            continue
        for j in range(maxg):
            pj = _poisson_pmf(j, la)
            if pj == 0:
                continue
            p = pi * pj
            if i > j:
                ph += p
            elif i < j:
                pa += p
            else:
                pd += p
    s = ph + pd + pa
    if s <= 0:
        return None
    return (ph / s, pd / s, pa / s)


# ── IR-07 动态 λ(t) ─────────────────────────────────────────────────────
def _remaining_fraction(minute: float, is_halftime: bool = False) -> float:
    """剩余时间占比（按全场 90' 计；中场场景 caller 应传真实已过分钟）。"""
    total = 90.0
    elapsed = max(0.0, float(minute))
    rem = (total - elapsed) / total
    return max(0.0, min(1.0, rem))


def dynamic_team_lambda(
    static_home: float,
    static_away: float,
    sh: int,
    sa: int,
    minute: float,
    is_halftime: bool = False,
    league: Optional[str] = None,
    prior_strength: float = 6.0,
) -> Tuple[float, float, float]:
    """以当前比分 + 剩余时间为条件，做贝叶斯收缩得到后验每-90' λ。

    后验 = (n*observed_rate + k*prior) / (n + k)
      - observed_rate = (已进数 / 已过分钟) * 90   （把已过时段的实际进球率年化到 90'）
      - n = 已发生总进球数（进球是强证据；0 球时 n→1，几乎全信先验）
      - k = prior_strength（赛前静态 λ 的等效先验样本量，默认 6）

    返回 (home_post_per90, away_post_per90, rem_frac)。
    """
    rem = _remaining_fraction(minute, is_halftime)
    elapsed = max(1.0, float(minute)) if float(minute) > 0 else 45.0
    total90 = 90.0

    sh = int(sh or 0)
    sa = int(sa or 0)

    home_obs = (sh / elapsed) * total90 if elapsed > 0 else static_home
    away_obs = (sa / elapsed) * total90 if elapsed > 0 else static_away

    # 有效样本：已发生进球数（至少 1，避免 0 球时把 observed 当 0 强拉）
    n = max(1.0, float(sh + sa))

    home_post = (n * home_obs + prior_strength * static_home) / (n + prior_strength)
    away_post = (n * away_obs + prior_strength * static_away) / (n + prior_strength)

    # 防爆炸：后验不超过 静态*3 + 2（经验上单队单场进 5+ 已极罕见）
    home_post = min(home_post, static_home * 3.0 + 2.0)
    away_post = min(away_post, static_away * 3.0 + 2.0)
    home_post = max(0.0, home_post)
    away_post = max(0.0, away_post)
    return home_post, away_post, rem


def simulate_inplay_1x2(
    home_post: float,
    away_post: float,
    sh: int,
    sa: int,
    minute: float,
    is_halftime: bool = False,
    maxg: int = 8,
) -> Tuple[float, float, float]:
    """后验 λ(t) → 终场 1X2 概率（已把当前比分 sh/sa 计入终场分布）。

    终场分布 = Poisson(home_post * rem) 叠加到 sh   vs   Poisson(away_post * rem) 叠加到 sa。
    """
    rem = _remaining_fraction(minute, is_halftime)
    lh = home_post * rem
    la = away_post * rem
    sh = int(sh or 0)
    sa = int(sa or 0)

    ph = pd = pa = 0.0
    for i in range(maxg):
        pi = _poisson_pmf(i, lh)
        if pi == 0:
            continue
        for j in range(maxg):
            pj = _poisson_pmf(j, la)
            if pj == 0:
                continue
            p = pi * pj
            fh, fa = sh + i, sa + j
            if fh > fa:
                ph += p
            elif fh < fa:
                pa += p
            else:
                pd += p
    s = ph + pd + pa
    if s <= 0:
        return (0.0, 0.0, 0.0)
    return (ph / s, pd / s, pa / s)


def lambda_consistency_flag(
    home_post: float,
    away_post: float,
    sh: int,
    sa: int,
    threshold_goals: int = 3,
    threshold_lambda: float = 0.5,
) -> Optional[str]:
    """IR-07 λ 与比分一致性告警。

    某队已进 ≥ threshold_goals 球但该队后验 λ < threshold_lambda（年化到单场期望仍严重偏低），
    说明 λ 与事实背离 → 标记该场概率输出「不可信」，不进入硬判定。
    """
    sh = int(sh or 0)
    sa = int(sa or 0)
    if sh >= threshold_goals and home_post < threshold_lambda:
        return f"home_lambda_inconsistent(sh={sh}, post={home_post:.2f}<{threshold_lambda})"
    if sa >= threshold_goals and away_post < threshold_lambda:
        return f"away_lambda_inconsistent(sa={sa}, post={away_post:.2f}<{threshold_lambda})"
    return None


# ── IR-19 低级别联赛概率校准 ───────────────────────────────────────────
def _safe_league_tag(league: Optional[str]) -> str:
    if not league:
        return "unknown"
    return re.sub(r"[^A-Za-z0-9_\u4e00-\u9fff]+", "_", str(league)).strip("_") or "unknown"


def _calib_path(league: Optional[str], calib_dir: Optional[str] = None) -> str:
    base = calib_dir or os.path.join(os.path.dirname(__file__), "..", "models")
    base = os.path.abspath(base)
    tag = _safe_league_tag(league)
    return os.path.join(base, f"inplay_1x2_isotonic_{tag}.joblib")


# ── FLB 小样本收缩先验 (IR-19 v2) ───────────────────────────────────────
# 经验贝叶斯收缩：per-league 校准向全局 default 收缩，权重 w=per_n/(per_n+K)。
# 小样本联赛 K 主导 → 偏向全局(携带正确 favorite-longshot 方向)；大样本 → 自身主导。
# 正确 FLB 方向 = 热门(胜率高)市场给的概率偏低、真实命中率更高 → 把概率调高；
#                 冷门(胜率低)市场给的概率偏高、真实命中率更低 → 把概率调低。
# 由大样本全局 default(8313 场) 学到并作为收缩目标，避免小样本 per-league 退化为 0/1 硬确定。
_FLB_PRIOR_K = 200.0


def _load_iso_bundle(path: str):
    """加载等渗校准 bundle。返回 (payload, n)，payload 为
    {"kind":"1x2","models":{home,draw,away}} 或 {"kind":"ou","model":m}，缺失则 (None,0)。"""
    import joblib  # type: ignore
    try:
        b = joblib.load(path)
    except Exception:
        return None, 0
    n = int(b.get("n", 0) or 0)
    if b.get("models"):
        return {"kind": "1x2", "models": b["models"]}, n
    if b.get("model") is not None:
        return {"kind": "ou", "model": b["model"]}, n
    return None, 0


def _apply_1x2(payload, p_home, p_draw, p_away):
    models = (payload or {}).get("models") or {}
    ch, cd, ca = models.get("home"), models.get("draw"), models.get("away")
    if ch is None or cd is None or ca is None:
        return None
    ph = float(ch.predict([[p_home]])[0])
    pd_ = float(cd.predict([[p_draw]])[0])
    pa = float(ca.predict([[p_away]])[0])
    s = ph + pd_ + pa
    if s <= 0:
        return None
    return (ph / s, pd_ / s, pa / s)


def _clip_norm_1x2(ph, pd_, pa):
    ph = min(max(ph, 0.01), 0.99)
    pd_ = min(max(pd_, 0.01), 0.99)
    pa = min(max(pa, 0.01), 0.99)
    s = ph + pd_ + pa
    if s <= 0:
        return (ph, pd_, pa)
    return (ph / s, pd_ / s, pa / s)


def _apply_ou(payload, p_over):
    m = (payload or {}).get("model")
    if m is None:
        return None
    return float(m.predict([[p_over]])[0])


def isotonic_calibrate_1x2(
    p_home: float,
    p_draw: float,
    p_away: float,
    league: Optional[str] = None,
    calib_dir: Optional[str] = None,
) -> Tuple[float, float, float, Optional[str]]:
    """按联赛对 1X2 概率做等渗校准，并向全局 default 收缩(FLB 小样本校正)。

    返回 (p_home, p_draw, p_away, note)。
    - 仅全局 default 可用(无 per-league 文件) → 走全局校准(覆盖长尾低级别联赛, 不恒等回退)。
    - per-league + 全局 default 均存在 → 经验贝叶斯收缩:
        w = per_n/(per_n+K), po = w*po_per + (1-w)*po_global
      小样本联赛 K 主导偏向全局(携带正确 FLB 方向); 大样本自身主导。
    - 输出裁剪 [0.01,0.99] 并重新归一化。
    - 既无 per-league 也无全局 default → 恒等回退(None note, 不伪造)。
    """
    path = _calib_path(league, calib_dir)
    per, per_n = _load_iso_bundle(path)
    gpath = os.path.join(os.path.dirname(path), "inplay_1x2_isotonic_default.joblib")
    if per is None:
        glob, _ = _load_iso_bundle(gpath)
        if glob is None:
            return (p_home, p_draw, p_away, None)
        out = _apply_1x2(glob, p_home, p_draw, p_away)
        if out is None:
            return (p_home, p_draw, p_away, None)
        ph, pd_, pa = _clip_norm_1x2(*out)
        return (ph, pd_, pa, "isotonic_calibrated(global_default)")
    out = _apply_1x2(per, p_home, p_draw, p_away)
    if out is None:
        return (p_home, p_draw, p_away, None)
    ph, pd_, pa = out
    glob, _ = _load_iso_bundle(gpath)
    if glob is not None and per_n > 0:
        go = _apply_1x2(glob, p_home, p_draw, p_away)
        if go is not None:
            w = per_n / (per_n + _FLB_PRIOR_K)
            gh, gd_, ga = go
            ph = w * ph + (1 - w) * gh
            pd_ = w * pd_ + (1 - w) * gd_
            pa = w * pa + (1 - w) * ga
            note = "isotonic_calibrated(league=%s, flb_shrink w=%.2f)" % (_safe_league_tag(league), w)
        else:
            note = "isotonic_calibrated(league=%s)" % _safe_league_tag(league)
    else:
        note = "isotonic_calibrated(league=%s)" % _safe_league_tag(league)
    ph, pd_, pa = _clip_norm_1x2(ph, pd_, pa)
    return (ph, pd_, pa, note)


def isotonic_calibrate_ou(
    p_over: float,
    league: Optional[str] = None,
    calib_dir: Optional[str] = None,
) -> Tuple[float, Optional[str]]:
    """按联赛对 OU 大球概率做等渗校准，并向全局 default 收缩(FLB 小样本校正)。

    无联赛文件 → 全局 default；再无 → 恒等回退。
    per-league + 全局 default 均存在 → 经验贝叶斯收缩 w=per_n/(per_n+K)，输出裁剪 [0.01,0.99]。
    """
    # 注意 _calib_path 生成的是 inplay_1x2_isotonic_<tag>.joblib（带 _isotonic_ 后缀），
    # 须替换完整前缀 inplay_1x2_isotonic_ → inplay_ou_，否则会生成错误的 inplay_ou_isotonic_<tag>.joblib
    path = _calib_path(league, calib_dir).replace("inplay_1x2_isotonic_", "inplay_ou_")
    per, per_n = _load_iso_bundle(path)
    gpath = os.path.join(os.path.dirname(path), "inplay_ou_default.joblib")
    if per is None:
        glob, _ = _load_iso_bundle(gpath)
        if glob is None:
            return (p_over, None)
        po = _apply_ou(glob, p_over)
        if po is None:
            return (p_over, None)
        po = min(max(po, 0.01), 0.99)
        return (po, "isotonic_calibrated_ou(global_default)")
    po = _apply_ou(per, p_over)
    if po is None:
        return (p_over, None)
    glob, _ = _load_iso_bundle(gpath)
    if glob is not None and per_n > 0:
        go = _apply_ou(glob, p_over)
        if go is not None:
            w = per_n / (per_n + _FLB_PRIOR_K)
            po = w * po + (1 - w) * go
            note = "isotonic_calibrated_ou(league=%s, flb_shrink w=%.2f)" % (_safe_league_tag(league), w)
        else:
            note = "isotonic_calibrated_ou(league=%s)" % _safe_league_tag(league)
    else:
        note = "isotonic_calibrated_ou(league=%s)" % _safe_league_tag(league)
    po = min(max(po, 0.01), 0.99)
    return (po, note)


def train_league_calibration(
    db_path: Optional[str] = None,
    calib_dir: Optional[str] = None,
    min_samples: int = 60,
) -> dict:
    """IR-19 训练入口：从 match_outcomes(league + result + 赛前 opening 1X2/OU) 按联赛训练等渗校准器。

    数据来源修正（真实库 schema）：matches 无 result 列、score 稀疏；标注赛果在 match_outcomes
    (result h/d/a + op_1x2_h/d/a + op_ou_over/under/line + score_home/away)。直接读 match_outcomes，
    无需 join odds_snapshots，样本量 8313(1X2)/7838(OU) 远多于 odds_snapshots 路径(1868)。

      - 1X2：per-league(≥min_samples 高保真) + 全局 default(覆盖长尾低级别联赛，避免恒等回退架空 IR-19)。
      - OU ：全局 default(line 混合池化；不同盘口线去水概率近似可比)。
    无数据/缺列则跳过该联赛（不伪造校准）。

    返回 {league: {n, saved}, 'default_1x2': {...}, 'default_ou': {...}} 摘要。
    """
    try:
        from sklearn.isotonic import IsotonicRegression  # type: ignore
    except Exception:
        return {"error": "sklearn not available"}
    import sqlite3
    import joblib  # type: ignore

    db_path = db_path or os.path.join(
        os.path.dirname(__file__), "..", "data", "events.db"
    )
    if not os.path.exists(db_path):
        return {"error": f"db not found: {db_path}"}
    out_dir = calib_dir or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "models")
    )
    os.makedirs(out_dir, exist_ok=True)

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    # 1X2 标注：league, result, 赛前 opening 1X2
    rows = cur.execute("""
        SELECT league, result, op_1x2_h, op_1x2_d, op_1x2_a
        FROM match_outcomes
        WHERE result IN ('home','draw','away')
          AND op_1x2_h>0 AND op_1x2_d>0 AND op_1x2_a>0
    """).fetchall()
    # OU 标注：opening OU + 真实总进球 (+ league, 用于 per-league 校准)
    ou_rows = cur.execute("""
        SELECT league, op_ou_over, op_ou_under, op_ou_line, score_home, score_away
        FROM match_outcomes
        WHERE op_ou_over>0 AND op_ou_under>0 AND op_ou_line>0
          AND score_home IS NOT NULL AND score_away IS NOT NULL
    """).fetchall()
    con.close()

    from collections import defaultdict
    by_league = defaultdict(list)
    for lg, res, oh, od, oa in rows:
        inv = 1.0 / oh + 1.0 / od + 1.0 / oa
        if inv <= 0:
            continue
        ph0, pd0, pa0 = 1.0 / oh / inv, 1.0 / od / inv, 1.0 / oa / inv
        y = 0 if res == "home" else (1 if res == "draw" else 2)
        by_league[lg].append((ph0, pd0, pa0, y))

    summary = {}

    def _fit_1x2(data):
        ph_x = [[d[0]] for d in data]
        pd_x = [[d[1]] for d in data]
        pa_x = [[d[2]] for d in data]
        y_arr = [d[3] for d in data]
        ch = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(ph_x, [1 if y == 0 else 0 for y in y_arr])
        cd = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(pd_x, [1 if y == 1 else 0 for y in y_arr])
        ca = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(pa_x, [1 if y == 2 else 0 for y in y_arr])
        return {"home": ch, "draw": cd, "away": ca}, len(data)

    # 全局 default（覆盖长尾低级别联赛）
    all_1x2 = [d for data in by_league.values() for d in data]
    if len(all_1x2) >= min_samples:
        models, n = _fit_1x2(all_1x2)
        joblib.dump({"models": models, "n": n, "global": True},
                    os.path.join(out_dir, "inplay_1x2_isotonic_default.joblib"))
        summary["default_1x2"] = {"n": n, "saved": True}
    else:
        summary["default_1x2"] = {"n": len(all_1x2), "saved": False, "note": "insufficient samples"}

    # per-league 1X2
    for lg, data in by_league.items():
        if len(data) < min_samples:
            summary[_safe_league_tag(lg)] = {"n": len(data), "saved": False,
                                             "note": "insufficient samples"}
            continue
        models, n = _fit_1x2(data)
        tag = _safe_league_tag(lg)
        joblib.dump({"models": models, "n": n, "global": False},
                    os.path.join(out_dir, f"inplay_1x2_isotonic_{tag}.joblib"))
        summary[tag] = {"n": n, "saved": True}

    # OU：per-league (≥min_samples 高保真) + 全局 default（line 混合池化, 覆盖长尾）
    by_league_ou = defaultdict(list)
    for lg, oo, ou, line, sh, sa in ou_rows:
        inv = 1.0 / oo + 1.0 / ou
        if inv <= 0:
            continue
        po0 = 1.0 / oo / inv
        total = int(sh or 0) + int(sa or 0)
        y_over = 1 if total > line else 0
        by_league_ou[lg].append((po0, y_over))

    all_ou = [d for data in by_league_ou.values() for d in data]
    if len(all_ou) >= min_samples:
        px = [[d[0]] for d in all_ou]
        py = [d[1] for d in all_ou]
        m = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(px, py)
        joblib.dump({"model": m, "n": len(all_ou), "global": True},
                    os.path.join(out_dir, "inplay_ou_default.joblib"))
        summary["default_ou"] = {"n": len(all_ou), "saved": True}
    else:
        summary["default_ou"] = {"n": len(all_ou), "saved": False, "note": "insufficient samples"}

    # per-league OU
    for lg, data in by_league_ou.items():
        if len(data) < min_samples:
            summary[_safe_league_tag(lg)] = {**summary.get(_safe_league_tag(lg), {}),
                                             "ou_saved": False, "note": "ou insufficient"}
            continue
        px = [[d[0]] for d in data]
        py = [d[1] for d in data]
        m = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(px, py)
        tag = _safe_league_tag(lg)
        joblib.dump({"model": m, "n": len(data), "global": False},
                    os.path.join(out_dir, f"inplay_ou_{tag}.joblib"))
        # 同一联赛可能 1X2 不足但 OU 足(或反之); 单独记 ou_saved
        if tag not in summary or not summary[tag].get("saved"):
            summary[tag] = {"n": len(data), "ou_saved": True}
        else:
            summary[tag]["ou_saved"] = True

    return summary


if __name__ == "__main__":
    # 快速自检：1-4 @ 87' 用例（复现诊断报告根因）
    sh, sa, minute = 1, 4, 87
    # 赛前静态 λ（诊断报告：主 2.05 / 客 0.37）
    static_home, static_away = 2.05, 0.37
    hp, ap, rem = dynamic_team_lambda(static_home, static_away, sh, sa, minute)
    ph, pd, pa = simulate_inplay_1x2(hp, ap, sh, sa, minute)
    flag = lambda_consistency_flag(hp, ap, sh, sa)
    print(f"[self-check] 1-4 @87': home_post={hp:.2f} away_post={ap:.2f} rem={rem:.3f}")
    print(f"  终场1X2: 主胜={ph*100:.1f}% 平={pd*100:.1f}% 客胜={pa*100:.1f}%")
    print(f"  λ一致性告警: {flag}")
    assert pa >= 0.6, f"客胜应≥60%, 实际{pa*100:.1f}%"
    print("  PASS: 1-4 @87' 客胜主导 (IR-07 修复生效)")
