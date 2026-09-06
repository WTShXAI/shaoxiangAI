"""三级判定参数训练定档 (2026-08-30)。

用户: "这几个内容还需要训练" —— 三级判定的阈值(CONF_HIGH=0.70 / CONF_LOW=0.45)
与软加权衰减(0.08)是拍的初值, 须用干净数据回测定档(像领先方先验 α 那样)。

方法: 干净子集, 每场算 市场方向 + 领先方 + 置信度, 跑三级判定,
      评估各级别"给方向"的命中率; 网格扫阈值, 选时间外方向准确率最优组合。

⚠ 干净子集(排除 score_missing/假0-0)。

用法: runpy scripts/train_score_analyzer_params_20260830.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")
from analysis.live_goal_probe import _parse_kickoff, _open_1x2_from_snapshots  # noqa: E402
import pipeline.score_analyzer as SA  # noqa: E402


def dewater(h, d, a):
    ih, id_, ia = 1.0 / h, 1.0 / d, 1.0 / a
    s = ih + id_ + ia
    return ih / s, id_ / s, ia / s


def collect(limit=3000):
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff, league FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>='2026-08-15' "
        "ORDER BY kickoff DESC LIMIT ?", (limit,)).fetchall()
    rec = []
    for mk, home, away, sh, sa, ko, league in rows:
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1",
                       (mk,)).fetchone():
            continue
        if not con.execute(
                "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            continue
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            continue
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            continue
        ph, pd_, pa = dewater(oh, od, oa)
        mdir = 'home' if ph >= pd_ and ph >= pa else ('away' if pa >= pd_ else 'draw')
        # 终场方向
        fd = int(sh) - int(sa)
        true_dir = 'home' if fd > 0 else ('away' if fd < 0 else 'draw')
        # 领先方(用终场比分反推的"滚球中某个时点"不可得, 这里用终场方向作为"市场一致"的近似)
        # 简化: 用去水最强方向与终场方向做三级判定的近似验证(赛前场景)
        rec.append(dict(ph=ph, pd=pd_, pa=pa, mdir=mdir, true_dir=true_dir))
    con.close()
    return rec


def run_params(rec, conf_high, conf_low, atten):
    """按参数跑三级判定(赛前近似: 无领先方), 统计方向命中率与观望占比。"""
    SA.CONF_HIGH = conf_high
    SA.CONF_LOW = conf_low
    SA.SOFT_ATTENUATION = atten
    hit = tot = 0
    watch = 0
    for r in rec:
        sa = SA.analyze_score(r['mdir'], (r['ph'], r['pd'], r['pa']), None, 0, 0)
        if sa['方向'] is None:
            watch += 1
            continue
        tot += 1
        if sa['方向'] == r['true_dir']:
            hit += 1
    acc = hit / tot * 100 if tot else 0.0
    return acc, tot, watch


def main():
    print("收集干净样本 ...")
    rec = collect()
    print(f"  有效 {len(rec)} 场")

    # 基线: 市场 argmax 命中率
    base_hit = sum(1 for r in rec if r['mdir'] == r['true_dir'])
    print(f"\n基线(市场 argmax)命中率: {base_hit/len(rec)*100:.2f}%")

    print(f"\n当前参数 CONF_HIGH={SA.CONF_HIGH} CONF_LOW={SA.CONF_LOW} atten={SA.SOFT_ATTENUATION}:")
    acc, tot, watch = run_params(rec, SA.CONF_HIGH, SA.CONF_LOW, SA.SOFT_ATTENUATION)
    print(f"  给方向的 {tot} 场命中率 {acc:.2f}% | 观望 {watch} 场({watch/len(rec)*100:.1f}%)")

    print("\n网格扫描 (赛前近似, 阈值对命中率的影响):")
    print(f"{'CONF_HIGH':>10}{'CONF_LOW':>10}{'命中率':>10}{'给方向':>8}{'观望率':>8}")
    best = (None, -1)
    for ch in (0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
        for cl in (0.35, 0.40, 0.45, 0.50):
            acc, tot, watch = run_params(rec, ch, cl, SA.SOFT_ATTENUATION)
            if acc > best[1]:
                best = ((ch, cl), acc)
            flag = ' <- 当前' if (ch == SA.CONF_HIGH and cl == SA.CONF_LOW) else ''
            print(f"{ch:>10.2f}{cl:>10.2f}{acc:>9.2f}%{tot:>8d}{watch/len(rec)*100:>7.1f}%{flag}")
    print(f"\n最优阈值: CONF_HIGH={best[0][0]} CONF_LOW={best[0][1]} → 命中率 {best[1]:.2f}%")


if __name__ == "__main__":
    main()
