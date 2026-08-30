"""方向3 前置统计: 当前比分领先方 vs 最终赛果 的历史频率。

目的: 验证"模型在滚球阶段仍锚定初盘市场定价(如 away 热门 1.86)而忽略实时比分领先方"
      到底是不是偏差。若是, 用历史频率作软先验; 若不是, 不动策略。

口径:
  - 真实分钟用 **captured_at + kickoff** 推算 (minute_at 是 61.8% 占位垃圾, 不可用)
  - 每个采样点: (领先方, 领先球数, 剩余时间区间) → 最终 (home胜/平/away胜)
  - 只取滚球中(minute 5~85)且有当前比分的快照

输出: 频率表 + 与"初盘市场热门方向"的对比基线。

用法: PYTHONPATH=. python scripts/stat_lead_vs_result_20260829.py [样本场数]
"""
import json
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")

from analysis.live_goal_probe import _parse_kickoff, HALFTIME_BREAK_MIN  # noqa: E402

SAMPLE_MINUTES = (20, 40, 60, 75)
# 2026-08-30: 默认只统计**干净**样本(有真实比分采集记录), 排除假 0-0。
#   --include-dirty 可关掉(仅供对照, 结论不可信)。
CLEAN_ONLY = "--include-dirty" not in sys.argv


def true_minute(elapsed_min):
    """墙钟 elapsed 分钟 → 真实比赛分钟 (扣中场休息)。"""
    if elapsed_min <= 45:
        return elapsed_min
    if elapsed_min <= 45 + HALFTIME_BREAK_MIN:
        return 45
    return elapsed_min - HALFTIME_BREAK_MIN


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    con = sqlite3.connect(DB, timeout=60)

    # 时间切分: --before=YYYY-MM-DD / --after=YYYY-MM-DD
    # 先验表用训练集生成, 回测用测试集, 杜绝"同一批数据既建先验又验证"的循环论证。
    before = after = None
    for a in sys.argv[1:]:
        if a.startswith("--before="):
            before = a.split("=", 1)[1]
        elif a.startswith("--after="):
            after = a.split("=", 1)[1]
    con = sqlite3.connect(DB, timeout=60)

    sql = ("SELECT match_key, home, away, score_home, score_away, kickoff FROM matches "
           "WHERE status='finished' AND score_home IS NOT NULL AND score_away IS NOT NULL "
           "AND kickoff IS NOT NULL AND kickoff != '' ")
    params = []
    if before:
        sql += "AND kickoff < ? "
        params.append(before)
    if after:
        sql += "AND kickoff >= ? "
        params.append(after)
    sql += "ORDER BY kickoff DESC LIMIT ?"
    params.append(n * 2)
    rows = con.execute(sql, params).fetchall()

    # agg[(lead_side, lead_goals, time_band)] = Counter(home/draw/away)
    agg = defaultdict(lambda: defaultdict(int))
    n_matches = 0
    n_points = 0
    n_dirty = 0

    for mk, home, away, fsh, fsa, kickoff in rows:
        if n_matches >= n:
            break
        kots = _parse_kickoff(kickoff)
        if not kots:
            continue
        # 2026-08-30: 排除假 0-0 (从未有过非零 score_at 快照 = 没采到比分却被翻完场)。
        #   若不排除, 这批"0-0"会让"平局领先"格子虚高, 先验学到假规律。
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

        # 按目标分钟取最接近的快照 (每场每个目标分钟只取一次)
        picked = {}
        for score_at, cap in snaps:
            em = (cap - kots) / 60.0
            tm = true_minute(em)
            for tgt in SAMPLE_MINUTES:
                if abs(tm - tgt) <= 3 and tgt not in picked:
                    picked[tgt] = score_at
        if not picked:
            continue
        n_matches += 1

        for tgt, score_at in picked.items():
            try:
                sh, sa = (int(x) for x in str(score_at).replace(':', '-').split('-')[:2])
            except Exception:
                continue
            diff = sh - sa
            lead_side = 'home' if diff > 0 else ('away' if diff < 0 else 'draw')
            lead_goals = min(abs(diff), 3)
            band = '5-30' if tgt <= 30 else ('31-55' if tgt <= 55 else '56-85')
            # 最终结果
            fd = int(fsh) - int(fsa)
            res = 'home' if fd > 0 else ('away' if fd < 0 else 'draw')
            agg[(lead_side, lead_goals, band)][res] += 1
            n_points += 1

    print(f"样本: {n_matches} 场 finished, {n_points} 个滚球采样点\n")
    print(f"{'领先方':<8}{'领先球':<8}{'时间带':<9}{'样本':>6}   "
          f"{'主胜':>7}{'平':>7}{'客胜':>7}")
    print("-" * 62)
    keys = sorted(agg.keys(), key=lambda k: (k[0], k[1], k[2]))
    for k in keys:
        c = agg[k]
        tot = sum(c.values())
        if tot < 20:
            continue
        h = c.get('home', 0) / tot
        d = c.get('draw', 0) / tot
        a = c.get('away', 0) / tot
        print(f"{k[0]:<8}{k[1]:<8}{k[2]:<9}{tot:>6}   "
              f"{h*100:>6.1f}%{d*100:>6.1f}%{a*100:>6.1f}%")

    # 关键对比: 领先方最终获胜率 vs 50% 基线
    print("\n=== 关键判据: 领先方最终获胜率 (基准 50% = 抛硬币) ===")
    for lead in ('home', 'away'):
        for g in (1, 2, 3):
            tot = h = 0
            for k, c in agg.items():
                if k[0] != lead or k[1] != g:
                    continue
                tot += sum(c.values())
                h += c.get(lead, 0)
            if tot >= 30:
                print(f"  {lead} 领先 {g} 球 → 最终 {lead} 胜 {h/tot*100:.1f}%  (n={tot})")

    # ── 导出先验表 JSON (禁硬编码进代码: 模型运行时读这个文件) ──
    out_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "lead_result_prior.json")
    table = {}
    for k, c in agg.items():
        tot = sum(c.values())
        if tot <= 0:
            continue
        table[f"{k[0]}|{k[1]}|{k[2]}"] = {
            "n": tot,
            "home": round(c.get("home", 0) / tot, 4),
            "draw": round(c.get("draw", 0) / tot, 4),
            "away": round(c.get("away", 0) / tot, 4),
        }
    payload = {
        "generated": "2026-08-29",
        "source": "scripts/stat_lead_vs_result_20260829.py",
        "note": "P(最终赛果 | 当前领先方, 领先球数, 时间带)。真实分钟用 captured_at+kickoff "
                "推算(minute_at 是 61.8% 占位垃圾, 不可用)。禁手改, 重跑脚本再生成。",
        "sample_matches": n_matches,
        "sample_points": n_points,
        "clean_only": CLEAN_ONLY,
        # 时间切分: 记录先验表的训练窗口, 回测必须用窗口外的比赛
        "split": {"before": before, "after": after},
        "hint": "回测此先验时, 必须只统计 split 窗口之外的比赛, 否则循环论证。",
        "bands": list(SAMPLE_MINUTES),
        "table": table,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n[导出] 先验表 → {out_path}  ({len(table)} 个格子)")

    con.close()


if __name__ == "__main__":
    main()
