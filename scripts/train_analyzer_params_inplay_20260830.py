"""滚球场景 · 三级判定参数定档 (2026-08-30)。

第 24 轮的参数训练是"赛前近似"(无领先方)，没测到领先方价值。
本脚本用**滚球时点**(从 odds_snapshots 的 score_at+captured_at 重建比分/分钟)
评估三级判定的方向命中率，扫 CONF_HIGH/CONF_LOW 网格定档。

方法:
  - 干净子集(排除 score_missing) + 有开盘 1X2 + 有滚球比分快照
  - 每场采样若干滚球时点(比分+真实分钟)，算 领先方 + 置信度，跑 analyze_score
  - 指标: 给方向的时点命中率(方向 vs 实际终场方向) + 观望占比
  - 网格扫 CONF_HIGH/CONF_LOW，选时间外方向准确率最优

用法: runpy scripts/train_analyzer_params_inplay_20260830.py [样本场数]
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")
from analysis.live_goal_probe import _parse_kickoff, _open_1x2_from_snapshots  # noqa: E402
import pipeline.score_analyzer as SA  # noqa: E402

HALFTIME_BREAK = 15
SAMPLE_MINUTES = (25, 45, 65, 80)


def true_minute(elapsed_min):
    if elapsed_min <= 45:
        return elapsed_min
    if elapsed_min <= 45 + HALFTIME_BREAK:
        return 45
    return elapsed_min - HALFTIME_BREAK


def dewater(h, d, a):
    ih, id_, ia = 1.0 / h, 1.0 / d, 1.0 / a
    s = ih + id_ + ia
    return ih / s, id_ / s, ia / s


def collect(limit=1500):
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>='2026-08-15' "
        "ORDER BY kickoff DESC LIMIT ?", (limit,)).fetchall()
    now = time.time()
    rec = []
    for mk, home, away, sh, sa, ko in rows:
        kots = _parse_kickoff(ko)
        if not kots or now - kots < 2.5 * 3600:
            continue
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1",
                       (mk,)).fetchone():
            continue
        if not con.execute("SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                           "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            continue
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            continue
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            continue
        snaps = con.execute(
            "SELECT score_at, captured_at FROM odds_snapshots WHERE match_key=? "
            "AND minute_at>0 AND score_at IS NOT NULL AND score_at!='' "
            "AND captured_at>? ORDER BY captured_at ASC", (mk, kots)).fetchall()
        if not snaps:
            continue
        # 采样滚球时点
        picked = {}
        for score_at, cap in snaps:
            tm = true_minute((cap - kots) / 60.0)
            for tgt in SAMPLE_MINUTES:
                if abs(tm - tgt) <= 4 and tgt not in picked:
                    picked[tgt] = score_at
        if not picked:
            continue
        ph, pd_, pa = dewater(oh, od, oa)
        mdir = 'home' if ph >= pd_ and ph >= pa else ('away' if pa >= pd_ else 'draw')
        fd = int(sh) - int(sa)
        true_dir = 'home' if fd > 0 else ('away' if fd < 0 else 'draw')
        for tgt, score_at in picked.items():
            try:
                csh, csa = (int(x) for x in str(score_at).replace(':', '-').split('-')[:2])
            except Exception:
                continue
            diff = csh - csa
            lead_side = 'home' if diff > 0 else ('away' if diff < 0 else None)
            lead_goals = abs(diff) if diff != 0 else 0
            rec.append(dict(ph=ph, pd=pd_, pa=pa, mdir=mdir, true_dir=true_dir,
                            lead_side=lead_side, lead_goals=lead_goals, minute=tgt))
    con.close()
    return rec


def run_params(rec, conf_high, conf_low):
    SA.CONF_HIGH = conf_high
    SA.CONF_LOW = conf_low
    hit = tot = watch = 0
    # 分档统计
    by_level = {'定方向': [0, 0], '软加权': [0, 0], '观望': [0, 0]}
    for r in rec:
        sa = SA.analyze_score(r['mdir'], (r['ph'], r['pd'], r['pa']),
                              r['lead_side'], r['lead_goals'], r['minute'])
        lv = sa['级别']
        by_level[lv][1] += 1
        if sa['方向'] is None:
            watch += 1
            continue
        tot += 1
        if sa['方向'] == r['true_dir']:
            hit += 1
            by_level[lv][0] += 1
    acc = hit / tot * 100 if tot else 0.0
    return acc, tot, watch, by_level


def main():
    print("收集滚球时点 ...")
    rec = collect()
    print(f"  有效滚球时点 {len(rec)} 个")
    if len(rec) < 100:
        print("样本不足")
        return

    # 基线: 市场 argmax 命中率
    base = sum(1 for r in rec if r['mdir'] == r['true_dir']) / len(rec) * 100
    # 基线: 领先方命中率
    lead_tot = sum(1 for r in rec if r['lead_side'])
    lead_hit = sum(1 for r in rec if r['lead_side'] and r['lead_side'] == r['true_dir'])
    print(f"基线: 市场 argmax {base:.2f}% | 领先方 {lead_hit/lead_tot*100:.2f}% (n={lead_tot})")

    print(f"\n当前参数 CONF_HIGH={SA.CONF_HIGH} CONF_LOW={SA.CONF_LOW}:")
    acc, tot, watch, bl = run_params(rec, SA.CONF_HIGH, SA.CONF_LOW)
    print(f"  给方向 {tot} 时点命中率 {acc:.2f}% | 观望 {watch}({watch/len(rec)*100:.1f}%)")
    for lv, (h, n) in bl.items():
        if n:
            print(f"    {lv}: 命中 {h}/{n} = {h/n*100:.1f}%")

    print("\n网格扫描:")
    print(f"{'CONF_HIGH':>10}{'CONF_LOW':>10}{'命中率':>9}{'给方向':>8}{'观望率':>8}")
    best = (None, -1)
    for ch in (0.60, 0.65, 0.70, 0.75, 0.80):
        for cl in (0.40, 0.45, 0.50, 0.55):
            acc, tot, watch, _ = run_params(rec, ch, cl)
            if acc > best[1]:
                best = ((ch, cl), acc)
            cur = ' <- 当前' if (ch == SA.CONF_HIGH and cl == SA.CONF_LOW) else ''
            print(f"{ch:>10.2f}{cl:>10.2f}{acc:>8.2f}%{tot:>8d}{watch/len(rec)*100:>7.1f}%{cur}")
    print(f"\n最优: CONF_HIGH={best[0][0]} CONF_LOW={best[0][1]} → {best[1]:.2f}%")


if __name__ == "__main__":
    main()
