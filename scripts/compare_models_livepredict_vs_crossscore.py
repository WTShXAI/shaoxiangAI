"""同条件对比: _live_predict (复原后的赛程页模型) vs cross_score (重建后接的模型)。

⚠ 数据根基: 库里 62% 的"完场"比分是假 0-0(从未有过非零 score_at 快照),
   会制造大量虚假命中(模型推 0-0、库里也是假 0-0)。**必须只在干净子集上比**。

方法:
  - 同一批比赛、同一时刻(赛前 minute=0)、同一开盘 1X2 数据
  - cross_score:  derive_score_cross(con, mk, '0-0', 0)
  - _live_predict: bridge_service._live_predict(home, away, h, d, a, sport_key='')
      ⚠ sport_key 传空串: 传默认 'soccer_fifa_world_cup' 会触发世界杯校准
        (goal_scale 1.35) 让总进球高估 12%
  - 开盘 1X2 统一用 cross_score 同口径的 _open_1x2_from_snapshots, 保证公平

指标: 方向准确率 / top1 命中 / top3 命中 / Brier(对真实比分, 仅用各自 top3 概率)

用法: PYTHONPATH=. python scripts/compare_models_livepredict_vs_crossscore.py [样本场数]
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")

from analysis.live_goal_probe import _parse_kickoff, _open_1x2_from_snapshots  # noqa: E402
from pipeline.cross_score import derive_score_cross  # noqa: E402
import bridge_service  # noqa: E402  (import 约 1.6s, 单次 _live_predict 约 0.04s)


def norm(s):
    return str(s or '').replace(':', '-')


def dir_of(s):
    p = norm(s).split('-')
    try:
        h, a = int(p[0] or 0), int(p[1] or 0)
    except Exception:
        return None
    return 'home' if h > a else ('away' if a > h else 'draw')


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    since = sys.argv[2] if len(sys.argv) > 2 else '2026-08-15'
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>=? "
        "ORDER BY kickoff DESC LIMIT ?", (since, n)).fetchall()
    now = time.time()

    stat = {'cross_score': [0, 0, 0, 0, 0.0], '_live_predict': [0, 0, 0, 0, 0.0]}
    #        [n, dir_hit, top1_hit, top3_hit, brier_sum]
    skipped = {'no_odds': 0, 'dirty': 0, 'not_finished': 0, 'err': 0}
    samples = []

    for mk, home, away, sh, sa, ko in rows:
        kots = _parse_kickoff(ko)
        if not kots or now - kots < 2.5 * 3600:
            skipped['not_finished'] += 1
            continue
        # 干净子集: 必须有真实比分采集记录
        if not con.execute(
                "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            skipped['dirty'] += 1
            continue

        # 开盘 1X2 (与 cross_score 同口径, 保证公平)
        try:
            h, d, a = _open_1x2_from_snapshots(con, mk)
        except Exception:
            h = d = a = None
        if not (h and d and a and h > 1.01 and d > 1.01 and a > 1.01):
            skipped['no_odds'] += 1
            continue

        true_s = f"{int(sh)}-{int(sa)}"
        true_d = dir_of(true_s)

        # ── 模型 A: cross_score ──
        try:
            ra = derive_score_cross(con, mk, '0-0', 0)
            top3a = [norm(t['score']) for t in (ra.get('top3') or [])]
            proba = {norm(t['score']): float(t['prob']) for t in (ra.get('top3') or [])}
        except Exception:
            skipped['err'] += 1
            continue

        # ── 模型 B: _live_predict ──
        try:
            rb = bridge_service._live_predict(home, away, h, d, a, sport_key='')
            oip = rb.get('oip') or {}
            top3b = [norm(s) for s in (oip.get('top3_scores') or [])]
            probb = {norm(s): float(p) for s, p in
                     zip(oip.get('top3_scores') or [], oip.get('top3_prob') or [])}
        except Exception:
            skipped['err'] += 1
            continue

        for tag, top3, prob in (('cross_score', top3a, proba), ('_live_predict', top3b, probb)):
            if not top3:
                continue
            s = stat[tag]
            s[0] += 1
            if dir_of(top3[0]) == true_d:
                s[1] += 1
            if top3[0] == true_s:
                s[2] += 1
            if true_s in top3:
                s[3] += 1
            s[4] += (1.0 - prob.get(true_s, 0.0)) ** 2

        if len(samples) < 10:
            samples.append((home[:22], true_s, top3a[0] if top3a else '-',
                            top3b[0] if top3b else '-'))

    print(f"跳过: {skipped}")
    print()
    print(f"{'模型':<16}{'样本':>6}{'方向准确率':>12}{'top1':>10}{'top3':>10}{'Brier':>10}")
    print("-" * 64)
    for tag in ('cross_score', '_live_predict'):
        nn, dh, t1, t3, br = stat[tag]
        if not nn:
            continue
        print(f"{tag:<16}{nn:>6d}{dh/nn*100:>11.1f}%{t1/nn*100:>9.1f}%"
              f"{t3/nn*100:>9.1f}%{br/nn:>10.4f}")

    cs = stat['cross_score']
    lp = stat['_live_predict']
    if cs[0] and lp[0]:
        print("-" * 64)
        dd = (lp[1] / lp[0] - cs[1] / cs[0]) * 100
        d1 = (lp[2] / lp[0] - cs[2] / cs[0]) * 100
        d3 = (lp[3] / lp[0] - cs[3] / cs[0]) * 100
        db = lp[4] / lp[0] - cs[4] / cs[0]
        print(f"{'差(_live - cross)':<16}{'':>6}{dd:>+11.1f}pp{d1:>+9.1f}pp"
              f"{d3:>+9.1f}pp{db:>+10.4f}")
        print()
        print(f"结论: 方向 {'_live_predict 更优' if dd > 0 else 'cross_score 更优' if dd < 0 else '持平'};"
              f" top1 {'_live_predict 更优' if d1 > 0 else 'cross_score 更优' if d1 < 0 else '持平'};"
              f" Brier {'_live_predict 更优(更低)' if db < 0 else 'cross_score 更优' if db > 0 else '持平'}")

    print("\n抽样(实际 / cross_score / _live_predict):")
    for s in samples:
        print(f"   {s[0]:22s} {s[1]:5s} | {s[2]:5s} | {s[3]:5s}")
    con.close()


if __name__ == "__main__":
    main()
