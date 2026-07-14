#!/usr/env python3
# -*- coding: utf-8 -*-
"""G3 · 分歧子集样本增厚 进度看板 (真实 bet_records 监控, 赛制架构分析师).

回答: 「分歧闸门 + value_layer edge + 跨庄最优价 + 单注封顶」安全路径,
      当前真实样本够不够放开半自动? 还差多少?

区分两层 (同一张 bet_records 表):
  - 模拟背书层 (sim) : actual_result 已填 -> 历史复模拟落库(来自 P1-3 live_pilot_guardian --write-db)
                       用于 bootstrap 统计功效验证, 已严格得出结论.
  - 真实累积层 (live): actual_result IS NULL -> 9111 灰度期 live_mode 落 PENDING 待人工复核,
                       滚动累积真实未来比赛样本(本看板核心追踪对象).

放量门槛 (Go/No-Go, 与路线图 G3 闸门一致):
  1) 胜率 >= 40%            (单靠堆样本救不了, n≈700 才转正, 须 G4/G5/G6 抬胜率)
  2) 真实 live 累积 n >= 500 (须 G2 9111 灰度期持续 live_mode)
  3) 增厚到 n=500 时 95%CI 下限 > 0

输出: deliverables/g3_progress.json + 终端摘要.
"""
import os
import sys
import json
import math
import sqlite3
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "football_data.db")
OUT = os.path.join(ROOT, "deliverables", "g3_progress.json")
BANKROLL = 3000.0
B = 20000


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    # 严格区分两层 (注: backtest 落库 notes=NULL/PENDING, --daemon 落库 notes='PENDING_LIVE')
    cur.execute("""SELECT bet_id, predicted_result, actual_result, is_correct,
                          home_odds, draw_odds, away_odds, kelly, expected_value, notes
                   FROM bet_records
                   WHERE source='prediction' AND bet_type='recommendation'""")
    rows = cur.fetchall()
    con.close()

    sim = []   # 已结算 (模拟背书)
    live = []  # 未结算 (真实累积待结算)
    for (bid, pr, ar, ic, ho, do, ao, k, ev, notes) in rows:
        if notes == "PENDING_LIVE":
            live.append((bid, pr, ar, ic, ho, do, ao, k, ev))
        elif ar is not None:
            sim.append((bid, pr, ar, ic, ho, do, ao, k, ev))
        # ar 为 NULL 且 notes!=PENDING_LIVE -> 旧残留 (忽略, 不入层)

    # ── 模拟背书层: 重放资金曲线 + 胜率 + Bootstrap CI ──
    sim_resolved = [r for r in sim if r[3] is not None]   # is_correct 非 NULL
    n_sim = len(sim_resolved)
    wins = sum(1 for r in sim_resolved if r[3] == 1)
    win_rate = wins / n_sim * 100 if n_sim else 0.0

    sim_sorted = sorted(sim_resolved, key=lambda r: r[0])
    equity = BANKROLL
    g_list = []
    for (bid, pr, ar, ic, ho, do, ao, k, ev) in sim_sorted:
        di = {"H": 0, "D": 1, "A": 2}.get(pr)
        if di is None or not k or k <= 0:
            continue
        odds = [ho, do, ao]
        stake = k * equity
        if stake <= 0 or stake > equity:
            continue
        eq_before = equity
        if ic == 1:
            pnl = stake * (odds[di] - 1)
            equity += pnl
        else:
            pnl = -stake
            equity -= stake
        if equity <= 0:
            equity = 1.0
        g_list.append(math.log(1.0 + pnl / eq_before))

    roi_point = (math.exp(len(g_list) * (sum(g_list) / len(g_list)) if g_list else 0) - 1) * 100 if g_list else 0.0

    def boot(gl, n_t):
        rng = random.Random(20260711)
        n = len(gl)
        if n == 0:
            return {"n": n_t, "roi_median": 0.0, "ci_low": 0.0, "ci_high": 0.0}
        rois = []
        for _ in range(B):
            s = [gl[rng.randrange(n)] for _ in range(n_t)]
            rois.append((math.exp(n_t * sum(s) / n_t) - 1) * 100)
        rois.sort()
        return {"n": n_t, "roi_median": rois[B // 2],
                "ci_low": rois[int(0.025 * B)], "ci_high": rois[int(0.975 * B)]}

    ci_cur = boot(g_list, len(g_list))
    ci_500 = boot(g_list, 500)

    # ── 真实累积层 ──
    n_live = len(live)

    # ── Go/No-Go 判定 ──
    verdict = []
    if win_rate < 40:
        verdict.append(f"胜率 {win_rate:.1f}% < 40% 门槛(须 G4 RLM真源/G5 booster/G6 drift 抬胜率)")
    if n_live < 500:
        verdict.append(f"真实 live 累积 {n_live} < 500 注(须 G2 9111 灰度期持续 live_mode 落 PENDING→executed)")
    if ci_500["ci_low"] <= 0:
        verdict.append(f"增厚到 n=500 时 95%CI 下限 {ci_500['ci_low']:+.1f}% 仍≤0(单靠堆样本不够, 须同步抬胜率)")
    go = not verdict

    out = {
        "meta": {
            "bankroll": BANKROLL, "bootstrap_B": B,
            "source": "bet_records(source='prediction', bet_type='recommendation')",
        },
        "sim_backtest_layer": {
            "n_total_records": len(rows),
            "n_sim_resolved": n_sim,
            "wins": wins,
            "win_rate_pct": round(win_rate, 2),
            "roi_point_estimate_pct": round(roi_point, 2),
            "ci_current_n": ci_cur,
            "ci_projection_n500": ci_500,
        },
        "live_accumulation_layer": {
            "n_pending_unresolved": n_live,
            "note": ("9111 灰度期 live_mode 落 PENDING 待人工复核→executed 后滚动累积; "
                     "当前表内均为模拟背书层, 真实 live 增量待 G2 启动"),
        },
        "go_no_go": {
            "win_rate_ok": win_rate >= 40,
            "live_n_ok": n_live >= 500,
            "ci500_ok": ci_500["ci_low"] > 0,
            "verdict": "GO ✅" if go else "NO-GO ❌: " + "; ".join(verdict),
        },
    }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[G3] 分歧子集 bet_records 总记录={len(rows)} "
          f"(模拟背书已结算={n_sim}, 真实live待结算={n_live})")
    print(f"[G3] 模拟层胜率={win_rate:.1f}%  点估计ROI={roi_point:+.1f}%  n={len(g_list)}注")
    print(f"[G3] 当前n Bootstrap 95%CI=[{ci_cur['ci_low']:+.1f}%, {ci_cur['ci_high']:+.1f}%]")
    print(f"[G3] n=500投影 95%CI=[{ci_500['ci_low']:+.1f}%, {ci_500['ci_high']:+.1f}%]")
    print(f"[G3] Go/No-Go: {out['go_no_go']['verdict']}")
    print(f"[G3] 写出 -> {OUT}")


if __name__ == "__main__":
    main()
