"""赛前方向准确率诊断 (2026-08-30)。

疑点: football_data.db 上"市场去水 argmax" = 52.40%, 但 events.db 干净子集上
      `_live_predict` 只有 44.1%。一直拿这两个数互相比可能**不公平** ——
      前者是纯市场 argmax, 后者是 _live_predict 的完整加工输出。

本脚本在同一批 events.db 干净样本上, 并列评估:
  ① 市场去水 argmax        (基准, 纯市场)
  ② _live_predict 方向     (现生产)
  ③ cross_score 方向       (重建后被取代的)
  ④ Poisson GBM λ 方向     (新训, 校准前后)

并按联赛级别/样本特征分层, 看差距来自哪里。

用法: runpy scripts/diagnose_prematch_20260830.py [样本场数]
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
from pipeline.cross_score import derive_score_cross  # noqa: E402
from pipeline.poisson_gbm import predict_lambdas, direction_probs  # noqa: E402
import bridge_service  # noqa: E402

MIN_FINISHED_H = 2.5


def norm(s):
    return str(s or '').replace(':', '-')


def dir_of(s):
    p = norm(s).split('-')
    try:
        h, a = int(p[0] or 0), int(p[1] or 0)
    except Exception:
        return None
    return 'home' if h > a else ('away' if a > h else 'draw')


def dewater(h, d, a):
    ih, id_, ia = 1.0 / h, 1.0 / d, 1.0 / a
    s = ih + id_ + ia
    return (ih / s, id_ / s, ia / s)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff, league FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>='2026-08-15' "
        "ORDER BY kickoff DESC LIMIT ?", (n,)).fetchall()
    now = time.time()

    stat = defaultdict(lambda: [0, 0])       # tag -> [n, hit]
    by_bucket = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    skipped = defaultdict(int)
    t0 = time.time()

    for i, (mk, home, away, sh, sa, ko, league) in enumerate(rows):
        kots = _parse_kickoff(ko)
        if not kots or now - kots < MIN_FINISHED_H * 3600:
            skipped['recent'] += 1; continue
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1",
                       (mk,)).fetchone():
            skipped['fake'] += 1; continue
        if not con.execute(
                "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            skipped['no_score'] += 1; continue
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            oh = od = oa = None
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            skipped['no_odds'] += 1; continue

        true_d = dir_of(f"{int(sh)}-{int(sa)}")
        ph, pd_, pa = dewater(oh, od, oa)

        preds = {}
        # ① 市场 argmax
        preds['①市场argmax'] = int(max(range(3), key=lambda k: (ph, pd_, pa)[k]))
        # ③ cross_score
        try:
            rc = derive_score_cross(con, mk, '0-0', 0)
            t3 = [norm(t['score']) for t in (rc.get('top3') or [])]
            if t3:
                preds['③cross_score'] = {'home': 0, 'draw': 1, 'away': 2}[dir_of(t3[0])]
        except Exception:
            pass
        # ② _live_predict
        try:
            rl = bridge_service._live_predict(home, away, oh, od, oa, sport_key='')
            dm = {'主胜': 0, '平局': 1, '客胜': 2}.get(rl.get('direction'))
            if dm is not None:
                preds['②_live_predict'] = dm
        except Exception:
            pass
        # ④ GBM λ
        lam = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=league)
        if lam:
            pm = direction_probs(lam[0], lam[1])
            preds['④GBM(校准后)'] = int(max(range(3), key=lambda k: pm[k]))

        # 分层: 按热门程度(最大去水概率)
        fav = max(ph, pd_, pa)
        bucket = ('强热门>0.55' if fav > 0.55 else
                  '中0.42-0.55' if fav >= 0.42 else '弱<0.42')

        for tag, p in preds.items():
            stat[tag][0] += 1
            hit = 1 if p == {'home': 0, 'draw': 1, 'away': 2}[true_d] else 0
            stat[tag][1] += hit
            b = by_bucket[bucket][tag]
            b[0] += 1; b[1] += hit

        if (i + 1) % 200 == 0:
            print(f"  ... {i+1}/{len(rows)}  ({time.time()-t0:.0f}s)")

    print(f"\n跳过: {dict(skipped)}")
    print(f"\n===== 赛前方向准确率 (events.db 干净子集) =====")
    print(f"{'方案':<18}{'样本':>7}{'准确率':>10}")
    print("-" * 36)
    order = ['①市场argmax', '②_live_predict', '③cross_score', '④GBM(校准后)']
    for tag in order:
        s = stat.get(tag)
        if not s or not s[0]:
            continue
        print(f"{tag:<18}{s[0]:>7d}{s[1]/s[0]*100:>9.1f}%")

    print(f"\n===== 按热门程度分层 =====")
    for bk in ('强热门>0.55', '中0.42-0.55', '弱<0.42'):
        d = by_bucket.get(bk) or {}
        if not d:
            continue
        line = f"{bk:<14}"
        for tag in order:
            s = d.get(tag)
            line += f"{(s[1]/s[0]*100 if s and s[0] else 0):>10.1f}%" if s else f"{'-':>11}"
        print(f"{'':14}" + "".join(f"{t[:6]:>11}" for t in order))
        print(line)
        break
    print(f"{'':14}" + "".join(f"{t[:6]:>11}" for t in order))
    for bk in ('强热门>0.55', '中0.42-0.55', '弱<0.42'):
        d = by_bucket.get(bk) or {}
        if not d:
            continue
        line = f"{bk:<14}"
        for tag in order:
            s = d.get(tag)
            line += f"{(s[1]/s[0]*100 if s and s[0] else 0):>10.1f}%" if s and s[0] else f"{'-':>11}"
        print(line)
    con.close()


if __name__ == "__main__":
    main()
