"""
scripts/backfill_snapshots_20260830.py — 历史分析快照回填
============================================================
对已完场比赛重跑 _live_predict, 快照其分析 + 立即用真实赛果 resolve,
一次性成训练集(不必等前端手动累积)。

用法:
  python scripts/backfill_snapshots_20260830.py 100   # 回填最近100场已完场
"""
import sqlite3, sys
sys.path.insert(0, 'D:/Architecture')

from analysis.live_goal_probe import _open_1x2_from_snapshots
from pipeline.analysis_snapshot import record_snapshot, resolve_snapshot

DB = 'D:/Architecture/data/events.db'


def main(limit=100):
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute('''SELECT match_key, home, away, league, score_home, score_away, kickoff
        FROM matches WHERE status='finished' AND score_home IS NOT NULL
        AND (score_missing IS NULL OR score_missing=0)
        ORDER BY kickoff DESC LIMIT ?''', (limit,)).fetchall()

    # 延迟 import _live_predict (重, 避免启动慢)
    from bridge_service import _live_predict
    n_rec = 0
    n_res = 0
    n_skip = 0
    for mk, home, away, league, sh, sa, ko in rows:
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            oh = od = oa = None
        if not (oh and od and oa and oh > 1.01):
            n_skip += 1
            continue
        try:
            r = _live_predict(home, away, oh, od, oa, league=league)
        except Exception:
            n_skip += 1
            continue
        oip = r.get("oip") or {}
        sa = r.get("score_analysis") or {}
        top3 = oip.get("top3_scores")
        top3p = oip.get("top3_prob")
        try:
            ok = record_snapshot(
                con, match_key=mk, home=home, away=away, league=league,
                phase="pre", current_score="", current_minute=0,
                odds_h=oh, odds_d=od, odds_a=oa,
                ou_line=None, ou_over=None, ou_under=None,
                direction=r.get("direction"), market_direction=r.get("direction"),
                score_top1=(top3[0] if top3 else None),
                score_top3=([str(s) for s in top3] if top3 else None),
                score_top3_prob=([float(p) for p in top3p] if top3p else None),
                sa_level=sa.get("级别"), sa_direction=sa.get("方向"),
                sa_confidence=sa.get("置信度"), sa_note=sa.get("分歧标注"),
                induce_label=None, model_tag="_live_predict_backfill",
            )
            if ok:
                n_rec += 1
            n_res += resolve_snapshot(con, mk)
        except Exception:
            n_skip += 1
            continue
    print(f"回填完成: 记录 {n_rec} 条, 解析 {n_res} 条, 跳过 {n_skip} 场")
    con.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("limit", nargs="?", type=int, default=100)
    args = ap.parse_args()
    main(args.limit)
