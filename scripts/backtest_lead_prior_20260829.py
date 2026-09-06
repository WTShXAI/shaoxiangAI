"""方向3 回测: 「领先方最终获胜」先验是否真的改善滚球波胆方向。

⚠ 时间切分 (杜绝循环论证):
   先验表 config/lead_result_prior.json 由
     `python scripts/stat_lead_vs_result_20260829.py 9000 --before=2026-08-26`
   生成(训练集 = 8-26 之前开赛的比赛)。本回测**只统计 8-26 及之后开赛的比赛**
   (测试集), 与训练集无交集。脚本会先校验先验表的 split 字段, 不匹配直接拒绝跑。

对比 (同一场同一时点, 只差先验开关):
   A 基线 = derive_score_cross 原输出 (先验关闭)
   B 先验 = 应用领先方经验先验后

指标:
   1. 方向准确率 —— 主推比分的胜负方向 == 实际终场方向 (用户抱怨的"反向"就是这个)
   2. top1 比分命中率 —— 主推比分 == 实际终场比分
   3. 分布 Brier 分数 (多分类, 对真实比分的概率)

用法: PYTHONPATH=. python scripts/backtest_lead_prior_20260829.py [测试场数]
"""
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")

import pipeline.cross_score as cs  # noqa: E402
from analysis.live_goal_probe import _parse_kickoff, HALFTIME_BREAK_MIN  # noqa: E402

SPLIT_DATE = "2026-08-26"     # 必须与生成先验表时的 --before 一致
# 2026-08-30: 默认只评测**干净**样本(有真实比分采集记录), 排除假 0-0。
CLEAN_ONLY = "--include-dirty" not in sys.argv
SAMPLE_MINUTES = (25, 45, 65, 80)


def _apply_capped(dist, sh, sa, minute, alpha_cap):
    """复刻 cs._apply_lead_prior, 但把收缩权重 α 截断到 alpha_cap (敏感性扫描用)。"""
    if not dist:
        return dist, None
    try:
        sh, sa, minute = int(sh), int(sa), int(minute)
    except Exception:
        return dist, None
    band = cs._band_of_minute(minute)
    if band is None:
        return dist, None
    # 与生产 cs._apply_lead_prior 保持一致: 平局不加权(无领先方, 先验不适用)
    lead_side = "home" if sh > sa else ("away" if sh < sa else "draw")
    if lead_side == "draw":
        return dist, None
    cell = (cs._load_lead_prior().get("table") or {}).get(
        f"{lead_side}|{min(abs(sh - sa), 3)}|{band}")
    if not cell or int(cell.get("n", 0)) < cs._LEAD_PRIOR_MIN_N:
        return dist, None

    def _dir(s):
        try:
            mh, ma = (int(x) for x in str(s).replace(':', '-').split('-'))
        except Exception:
            return None
        return "home" if mh > ma else ("away" if mh < ma else "draw")

    dir_prob = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for s, p in dist.items():
        d = _dir(s)
        if d:
            dir_prob[d] += p
    tot = sum(dir_prob.values()) or 1.0
    dir_prob = {k: v / tot for k, v in dir_prob.items()}
    emp = {k: float(cell.get(k, 0.0)) for k in ("home", "draw", "away")}
    n = float(cell.get("n", 0))
    alpha = min(n / (n + cs._LEAD_PRIOR_K), min(alpha_cap, cs._LEAD_PRIOR_ALPHA_CAP))

    out = {}
    for s, p in dist.items():
        d = _dir(s)
        if not d:
            out[s] = p
            continue
        pm, pe = dir_prob.get(d, 0.0), emp.get(d, 0.0)
        if pm <= 1e-9:
            ratio = min((pe / 1e-3) ** alpha if pe > 0 else 1.0, 50.0)
        else:
            ratio = (pe / pm) ** alpha
        out[s] = p * ratio
    t = sum(out.values()) or 1.0
    return {s: p / t for s, p in out.items()}, f"α={alpha:.2f}"


def true_minute(elapsed_min):
    if elapsed_min <= 45:
        return elapsed_min
    if elapsed_min <= 45 + HALFTIME_BREAK_MIN:
        return 45
    return elapsed_min - HALFTIME_BREAK_MIN


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300

    # ── 校验先验表切分, 防止拿错窗口导致循环论证 ──
    prior = cs._load_lead_prior()
    split = prior.get("split") or {}
    if split.get("before") != SPLIT_DATE:
        print(f"[拒绝执行] 先验表 split.before={split.get('before')!r}, 期望 {SPLIT_DATE!r}。"
              f"请先跑: python scripts/stat_lead_vs_result_20260829.py 9000 --before={SPLIT_DATE}")
        sys.exit(1)
    print(f"先验表: 训练窗口 kickoff < {SPLIT_DATE}, "
          f"样本 {prior.get('sample_matches')} 场 / {prior.get('sample_points')} 点")
    print(f"测试集: kickoff >= {SPLIT_DATE} (与训练集无交集)\n")

    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, score_home, score_away, kickoff FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND score_away IS NOT NULL "
        "AND kickoff IS NOT NULL AND kickoff >= ? ORDER BY kickoff DESC LIMIT ?",
        (SPLIT_DATE, n * 3)).fetchall()

    # --forcecap=X 临时覆盖生产的 _LEAD_PRIOR_ALPHA_CAP, 用于纯研究性 α 扫描
    force_cap = None
    for a in sys.argv[1:]:
        if a.startswith("--forcecap="):
            force_cap = float(a.split("=", 1)[1])
    if force_cap is not None:
        cs._LEAD_PRIOR_ALPHA_CAP = force_cap
        print(f"[forcecap] _LEAD_PRIOR_ALPHA_CAP 临时设为 {force_cap}")

    # ── α 敏感性扫描: 先验权重上限。α = n/(n+K) 在大样本格子会趋近 1,
    #    等于几乎完全用经验频率覆盖模型的盘口判断, 可能过度自信(Brier 变差)。
    #    这里对同一批样本扫多档 alpha_cap, 找方向准确率与 Brier 的平衡点。
    ALPHA_CAPS = [0.0, 0.3, 0.5, 0.7, 1.0]
    stat = defaultdict(lambda: defaultdict(float))
    by_lead = defaultdict(lambda: defaultdict(float))   # (lead_key, tag) -> metrics
    n_pts = 0
    n_matches = 0
    n_dirty = 0
    _orig_alpha_cap = [1.0]   # 用可变容器存当前 cap, 供 patched 函数读取

    for mk, fsh, fsa, kickoff in rows:
        if n_matches >= n:
            break
        kots = _parse_kickoff(kickoff)
        if not kots:
            continue
        # 2026-08-30: 排除假 0-0 (从未有过非零 score_at 快照)。
        #   脏样本会让"模型推0-0、库里也是假0-0"产生虚假命中, 虚高基线准确率、
        #   并稀释先验的真实增益。--include-dirty 可关掉(仅供对照)。
        if CLEAN_ONLY and not con.execute(
                "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            n_dirty += 1
            continue
        snaps = con.execute(
            "SELECT score_at, captured_at FROM odds_snapshots "
            "WHERE match_key=? AND minute_at>0 AND score_at IS NOT NULL AND score_at!='' "
            "AND captured_at>? ORDER BY captured_at ASC", (mk, kots)).fetchall()
        if not snaps:
            continue
        picked = {}
        for score_at, cap in snaps:
            tm = true_minute((cap - kots) / 60.0)
            for tgt in SAMPLE_MINUTES:
                if abs(tm - tgt) <= 4 and tgt not in picked:
                    picked[tgt] = score_at
        if not picked:
            continue
        n_matches += 1

        fd = int(fsh) - int(fsa)
        true_dir = "home" if fd > 0 else ("away" if fd < 0 else "draw")
        true_score = f"{int(fsh)}-{int(fsa)}"

        for tgt, score_at in picked.items():
            try:
                csh, csa = (int(x) for x in str(score_at).replace(':', '-').split('-')[:2])
            except Exception:
                continue
            diff = csh - csa
            lead_key = ("home" if diff > 0 else ("away" if diff < 0 else "draw")) + \
                       str(min(abs(diff), 3))

            # A 基线: 关闭先验
            orig_fn = cs._apply_lead_prior
            cs._apply_lead_prior = lambda d, a, b, m: (d, None)
            try:
                ra = cs.derive_score_cross(con, mk, f"{csh}-{csa}", tgt)
            except Exception:
                ra = None
            finally:
                cs._apply_lead_prior = orig_fn
            if not ra:
                continue
            n_pts += 1

            # B 先验: 扫多档 alpha_cap (直接调底层函数, 不走 derive_score_cross 以省时间)
            #   取基线返回的 dist(未加权, top20) 作为输入, 逐档重算
            base_dist = {str(it["score"]).replace(':', '-'): float(it["prob"])
                         for it in (ra.get("dist") or [])}
            variants = {"base": ra}
            for cap in ALPHA_CAPS:
                if cap <= 0:
                    continue
                _d = dict(base_dist)
                # 临时限制 alpha 上限: 复刻 _apply_lead_prior 但把 alpha 截断
                _d2, _note = _apply_capped(_d, csh, csa, tgt, cap)
                _ranked = sorted(_d2.items(), key=lambda x: -x[1])[:3]
                variants[f"a{cap}"] = {
                    "top3": [{"score": s, "prob": round(p, 4)} for s, p in _ranked],
                    "dist": [{"score": s, "prob": round(p, 4)} for s, p in
                             sorted(_d2.items(), key=lambda x: -x[1])[:20]],
                }

            for tag, r in variants.items():
                top = (r.get("top3") or [{}])[0].get("score")
                if top:
                    try:
                        mh, ma = (int(x) for x in str(top).replace(':', '-').split('-'))
                        d = "home" if mh > ma else ("away" if mh < ma else "draw")
                    except Exception:
                        d = None
                    if d is not None:
                        stat[tag]["dir_total"] += 1
                        by_lead[(lead_key, tag)]["dir_total"] += 1
                        if d == true_dir:
                            stat[tag]["dir_hit"] += 1
                            by_lead[(lead_key, tag)]["dir_hit"] += 1
                    stat[tag]["top1_total"] += 1
                    if str(top).replace(':', '-') == true_score:
                        stat[tag]["top1_hit"] += 1
                # Brier: 对真实比分的概率
                p_true = 0.0
                for item in (r.get("dist") or []):
                    if str(item.get("score")).replace(':', '-') == true_score:
                        p_true = float(item.get("prob", 0.0))
                        break
                stat[tag]["brier"] += (1.0 - p_true) ** 2

    tags = ["base"] + [f"a{c}" for c in ALPHA_CAPS if c > 0]
    print(f"测试样本: {n_matches} 场, {n_pts} 个滚球时点\n")
    print(f"{'方案':<10}{'方向准确率':>12}{'top1命中':>12}{'Brier':>10}")
    print("-" * 46)
    for tag in tags:
        s = stat[tag]
        dt = s["dir_total"] or 1
        tt = s["top1_total"] or 1
        print(f"{tag:<10}{s['dir_hit']/dt*100:>11.2f}%{s['top1_hit']/tt*100:>11.2f}%"
              f"{s['brier']/tt:>10.4f}")

    b = stat["base"]
    bdt = b["dir_total"] or 1
    btt = b["top1_total"] or 1
    print("-" * 46)
    for tag in tags[1:]:
        s = stat[tag]
        dd = (s["dir_hit"] / (s["dir_total"] or 1) - b["dir_hit"] / bdt) * 100
        dt1 = (s["top1_hit"] / (s["top1_total"] or 1) - b["top1_hit"] / btt) * 100
        db = (s["brier"] / (s["top1_total"] or 1) - b["brier"] / btt)
        print(f"{tag:<10}{dd:>+11.2f}pp{dt1:>+11.2f}pp{db:>+10.4f}")

    # ── 分层: 按当前领先情况看先验到底在哪起作用 ──
    print("\n=== 分层方向准确率 (按当前领先方+球数) ===")
    lead_keys = sorted({k[0] for k in by_lead})
    print(f"{'领先情况':<10}{'样本':>6}" + "".join(f"{t:>9}" for t in tags))
    for lk in lead_keys:
        row = f"{lk:<10}"
        nk = by_lead.get((lk, "base"), {}).get("dir_total", 0)
        row += f"{int(nk):>6}"
        for t in tags:
            s = by_lead.get((lk, t), {})
            tot = s.get("dir_total", 0) or 1
            row += f"{s.get('dir_hit', 0)/tot*100:>8.1f}%"
        print(row)

    print("\n结论判据: 方向准确率↑ 且 Brier 不恶化(或恶化可接受) → 采纳该 α; "
          "方向准确率↑但 Brier 明显↑ → 说明过度自信, 降 α。")
    con.close()


if __name__ == "__main__":
    main()
