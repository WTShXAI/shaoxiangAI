"""前瞻波胆评估器 — 多庄 sharp tilt vs 单庄 OIP 命中率累积
============================================================
随 leisu 每日采集 + GQ 出赛果, 自动累积评估:
  base = OIP(GQ乐鱼单庄1X2反解) ; tilt = OIP tilted 到 leisu 多庄 sharp 共识
  每场真实比分落进 top3 即命中. 累积到几十场看统计显著(目前 N<30 仅观察).

Ledger: data/prospective_score_ledger.json (append-only, 按 match_key 去重, 幂等可重跑).
依赖: pipeline.multibook_consensus.sharp_consensus_for_match / pipeline.dc_score_model.tilt_to_outcomes
"""
from __future__ import annotations
import sqlite3
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import numpy as np
from scipy.optimize import root
from scipy.stats import poisson

from pipeline.multibook_consensus import sharp_consensus_for_match, load_leisu_groups, analyze_match
from pipeline.dc_score_model import tilt_to_outcomes

GQ = "data/events.db"
LEISU_DB = "data/football_data.db"  # leisu_odds 表所在库
LEDGER = Path("data/prospective_score_ledger.json")
SIGNIFICANCE_N = 30  # 低于此 N 仅观察, 不下结论


# ---------- OIP helpers ----------
def _indep_1x2(lam, mu, maxg=8):
    h = poisson.pmf(np.arange(maxg + 1), lam)
    a = poisson.pmf(np.arange(maxg + 1), mu)
    M = np.outer(h, a)
    return np.tril(M, -1).sum(), np.trace(M), np.triu(M, 1).sum()


def oip_from_1x2(ph, pd_, pa, maxg=8):
    def obj(p):
        lam, mu = p
        h, d, a = _indep_1x2(lam, mu, maxg)
        return [h - ph, d - pd_]
    sol = root(obj, [1.3, 1.1], method="hybr")
    if sol.success:
        lam, mu = max(0.05, float(sol.x[0])), max(0.05, float(sol.x[1]))
    else:
        best, berr = (1.3, 1.1), 1e9
        for lam in np.linspace(0.2, 4.0, 80):
            for mu in np.linspace(0.2, 4.0, 80):
                h, d, a = _indep_1x2(lam, mu, maxg)
                err = abs(h - ph) + abs(d - pd_)
                if err < berr:
                    berr, best = err, (lam, mu)
        lam, mu = best
    h = poisson.pmf(np.arange(maxg + 1), lam)
    a = poisson.pmf(np.arange(maxg + 1), mu)
    M = np.outer(h, a)
    return M / M.sum()


def deoverround(oh, od, oa):
    inv = 1.0 / oh + 1.0 / od + 1.0 / oa
    return np.array([(1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv])


def top3_idx(M, k=3):
    flat = M.flatten()
    return [tuple(int(x) for x in np.unravel_index(i, M.shape)) for i in np.argsort(-flat)[:k]]


# ---------- GQ fetchers ----------
def iter_gq_finished():
    """yield (match_key, home, away, sh, sa) for finished matches with scores."""
    g = sqlite3.connect(GQ)
    rows = g.execute(
        """SELECT match_key, home, away, score_home, score_away
           FROM matches WHERE status='finished'
             AND score_home IS NOT NULL AND score_away IS NOT NULL""").fetchall()
    g.close()
    for mk, h, a, sh, sa in rows:
        yield (mk, h, a, int(sh), int(sa))


def get_gq_1x2(match_key):
    g = sqlite3.connect(GQ)
    rows = g.execute(
        "SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND market='1X2'",
        (match_key,)).fetchall()
    g.close()
    m = {}
    for sel, o in rows:
        s = (sel or "").lower()
        if s in ("home", "h", "1"): m["h"] = o
        elif s in ("draw", "d", "x", "0"): m["d"] = o
        elif s in ("away", "a", "2"): m["a"] = o
    return (m["h"], m["d"], m["a"]) if len(m) == 3 else None


def _bigrams(s):
    s = (s or "").replace(" ", "")
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def get_leisu_kickoff(home, away):
    """取该 leisu 场次的开赛 unix 秒(用于给模糊队名匹配加时间闸门)."""
    c = sqlite3.connect(LEISU_DB)
    r = c.execute(
        """SELECT kickoff_ts FROM leisu_odds
           WHERE home_raw=? AND away_raw=? AND kickoff_ts IS NOT NULL LIMIT 1""",
        (home, away)).fetchone()
    c.close()
    return int(r[0]) if r and r[0] else None


def _fuzzy_gq_by_kickoff(home, away):
    """队名变体兜底: 仅当 [同一开赛日] + [主客双方各有 2-gram 重叠] + [开赛时刻差 ≤90min]
    三闸门同时满足才认定同场.

    为何必须三重闸门: 裸 2-gram 双边匹配实测误报率 66%
    (阿尔堤斯→'AV阿尔塔FC'、韦伦桑丹斯基→'奥杜斯基克学院' 均为假匹配),
    直接采用会把错误赛果写进账本, 违反"数据禁虚拟"铁律. 加时间闸门后误报清零.
    """
    ko = get_leisu_kickoff(home, away)
    if ko is None:
        return None
    dt = datetime.fromtimestamp(ko)
    day = dt.strftime("%Y-%m-%d")
    g = sqlite3.connect(GQ)
    rows = g.execute(
        """SELECT match_key, home, away, score_home, score_away, status, kickoff
           FROM matches WHERE substr(kickoff,1,10)=?""", (day,)).fetchall()
    g.close()
    fh, fa = _bigrams(home), _bigrams(away)
    for mk, gh, ga, sh, sa, st, kf in rows:
        if not (fh & _bigrams(gh)) or not (fa & _bigrams(ga)):
            continue
        try:
            gdt = datetime.strptime(kf, "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            continue
        if abs((gdt - dt).total_seconds()) > 90 * 60:
            continue
        print(f"[fuzzy] leisu[{home} vs {away}] -> GQ[{gh} vs {ga}] @{kf} (队名变体, 时间闸门通过)")
        return (mk, sh, sa, st, kf)
    return None


def get_gq(home, away):
    """按 leisu 队名找 GQ 同场: 先双向 LIKE 精确兜底, 失败再走开赛时间闸门模糊匹配."""
    g = sqlite3.connect(GQ)
    r = g.execute(
        """SELECT match_key, score_home, score_away, status, kickoff
           FROM matches
           WHERE (home LIKE ? AND away LIKE ?) OR (home LIKE ? AND away LIKE ?)""",
        (f"%{home}%", f"%{away}%", f"%{away}%", f"%{home}%")).fetchone()
    g.close()
    if r:
        return r  # (match_key, sh, sa, status, kickoff)
    return _fuzzy_gq_by_kickoff(home, away)


def is_leisu_polluted(home, away):
    """雷速OCR混采闸: 同场多庄 home-odds 跨>40% 或 主客favorite矛盾 => 污染(initial/live混采).
    返回 True 表示该场 leisu_odds 不可信, 应从前瞻评估排除(铁律: 数据禁虚拟)."""
    c = sqlite3.connect(LEISU_DB)
    rows = c.execute(
        "SELECT odds_h,odds_d,odds_a FROM leisu_odds WHERE home_raw=? AND away_raw=? AND market='1X2'",
        (home, away)).fetchall()
    c.close()
    if not rows:
        return False
    hs = [h for h, d, a in rows if h]
    if not hs:
        return False
    ratio = max(hs) / min(hs)
    favs = set()
    for h, d, a in rows:
        fav = min(("H", h), ("D", d), ("A", a), key=lambda x: x[1])[0]
        favs.add(fav)
    return (len(favs) > 1) or (ratio > 1.4)


# ---------- ledger ----------
def load_ledger():
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    return {"module": "prospective_score_eval", "created": datetime.now(timezone.utc).isoformat(),
            "matches": []}


def save_ledger(L):
    LEDGER.write_text(json.dumps(L, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- core ----------
def evaluate_new():
    """从 leisu(已有 sharp 共识)出发, 模糊找 GQ 同场赛果+单庄1X2, 评估 tilt vs base.
    倒序循环规避 GQ/leisu 队名变体(如 古比斯/库普斯)导致的精确匹配失败."""
    L = load_ledger()
    seen = {r["match_key"] for r in L["matches"]}
    now = datetime.now(timezone.utc).isoformat()
    new = []
    for m in load_leisu_groups(market="1X2"):
        res = analyze_match(m)
        if not res.has_true_sharp:
            continue  # 本评估只测多庄 sharp tilt, 无 true sharp 跳过
        home, away = m["home"], m["away"]
        gq = get_gq(home, away)
        if not gq:
            continue  # GQ 无此场(结果未出或不在覆盖)
        if is_leisu_polluted(home, away):
            # 雷速OCR混采污染(initial/live错行) -> sharp共识不可信, 排除而非产出假信号
            print(f"[skip] {home} vs {away}: leisu odds polluted (initial/live mix), 排除")
            continue
        mk, sh, sa, status, kickoff = gq
        if status != "finished":
            continue  # 未完场: live 的中间比分绝不可当终场 (2026-07-29 修复: 圣米伦 live 0-0 被记成真=[0,0])
        if sh is None or sa is None:
            continue  # 赛果未出(仍 live/未来)
        if mk in seen:
            continue
        odds = get_gq_1x2(mk)
        if not odds:
            continue
        base_p = deoverround(*odds)
        oip = oip_from_1x2(*base_p)
        if oip is None:
            continue
        tilt_p = np.array([res.sharp_consensus["h"], res.sharp_consensus["d"], res.sharp_consensus["a"]])
        tilted = tilt_to_outcomes(oip, tilt_p)
        actual = (int(sh), int(sa))
        base_top = top3_idx(oip)
        tilt_top = top3_idx(tilted)
        bh = actual in base_top
        th = actual in tilt_top
        new.append({
            "match_key": mk, "home": home, "away": away, "actual": list(actual),
            "gq_1x2": [round(float(x), 2) for x in odds],
            "sharp_consensus": {k: round(float(v) * 100, 1) for k, v in res.sharp_consensus.items()},
            "base_top3": [list(x) for x in base_top],
            "tilt_top3": [list(x) for x in tilt_top],
            "base_hit": bh, "tilt_hit": th, "evaluated_at": now,
        })
        seen.add(mk)
    L["matches"].extend(new)
    save_ledger(L)
    return new


def summarize(L):
    ms = [m for m in L["matches"] if not m.get("polluted")]
    n_excluded = sum(1 for m in L["matches"] if m.get("polluted"))
    n = len(ms)
    if n == 0:
        return ("Ledger 无干净样本(有效 {n} 场, 已隔离 {x} 场污染数据).\n"
                "当前 leisu_odds 历史数据被初始/即时混采污染, 前瞻评估暂停 — "
                "须清表重采雷速后方可恢复.".format(n=0, x=n_excluded))
    bh = sum(m["base_hit"] for m in ms)
    th = sum(m["tilt_hit"] for m in ms)
    helped = sum(1 for m in ms if (not m["base_hit"] and m["tilt_hit"]))
    hurt = sum(1 for m in ms if (m["base_hit"] and not m["tilt_hit"]))
    tie = sum(1 for m in ms if m["base_hit"] == m["tilt_hit"])
    lines = []
    if n_excluded:
        lines.append(f"⚠️ 已隔离 {n_excluded} 场污染(leisu初始/即时混采)数据, 不计入.")
    lines.append(f"前瞻波胆评估 — 有效累积 {n} 场 (显著阈值 N≥{SIGNIFICANCE_N})")
    lines.append(f"  base(单庄OIP) top3 命中 = {bh}/{n} = {bh/n*100:.1f}%")
    lines.append(f"  tilt(多庄sharp) top3 命中 = {th}/{n} = {th/n*100:.1f}%")
    lines.append(f"  tilt 帮了 {helped} 场 / 害了 {hurt} 场 / 持平 {tie} 场")
    verdict = "观察期(N不足), 不下结论" if n < SIGNIFICANCE_N else (
        "tilt 显著更优" if th > bh else ("tilt 显著更差" if bh > th else "无显著差异"))
    lines.append(f"  初步判定: {verdict}")
    lines.append("=" * 64)
    for m in ms:
        flag = "▲" if m["tilt_hit"] and not m["base_hit"] else ("▼" if m["base_hit"] and not m["tilt_hit"] else " ")
        lines.append(f"  {flag} {m['home']} vs {m['away']} 真={m['actual']} "
                     f"sharpH={m['sharp_consensus']['h']}% base命中={m['base_hit']} tilt命中={m['tilt_hit']}")
    return "\n".join(lines)


def diagnose():
    """漏斗自诊断: 统计 leisu 各场卡在哪一环, 便于每日自动化直接定位样本增长瓶颈.
    (2026-07-30 加: 此前每次排查都要手写临时脚本, 固化进主流程.)"""
    from collections import Counter
    c = Counter()
    for m in load_leisu_groups(market="1X2"):
        home, away = m["home"], m["away"]
        if not analyze_match(m).has_true_sharp:
            c["NO_SHARP (leisu无真sharp庄)"] += 1
            continue
        gq = get_gq(home, away)
        if not gq:
            c["NO_GQ (乐鱼未覆盖此场)"] += 1
            continue
        if is_leisu_polluted(home, away):
            c["POLLUTED (雷速OCR混采)"] += 1
            continue
        mk, sh, sa, status, _ = gq
        if status != "finished":
            c["NOT_FINISHED (GQ仍live/未结算)"] += 1
        elif sh is None or sa is None:
            c["NO_SCORE (finished但无比分)"] += 1
        elif not get_gq_1x2(mk):
            c["NO_GQ_1X2 (缺单庄赔率)"] += 1
        else:
            c["OK (可评估)"] += 1
    total = sum(c.values())
    lines = ["", "=" * 64, f"漏斗自诊断 — leisu 共 {total} 组"]
    for k, v in sorted(c.items(), key=lambda x: -x[1]):
        lines.append(f"  {v:3d} ({v/total*100:4.1f}%)  {k}")
    ok = c.get("OK (可评估)", 0)
    if total:
        lines.append(f"  → 端到端转化率 {ok}/{total} = {ok/total*100:.1f}%")
    return "\n".join(lines)


def main():
    new = evaluate_new()
    L = load_ledger()
    print(f"本次新增评估 {len(new)} 场")
    print(summarize(L))
    print(diagnose())
    print(f"\nLedger: {LEDGER}")


if __name__ == "__main__":
    main()
