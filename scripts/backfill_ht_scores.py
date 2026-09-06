"""回填历史已完赛比赛的半场(HT)比分.

数据源: 乐鱼比赛详情接口 msc 的 'S1|' 项 = 半场比分 (S0| 为全场).
目标: finished + 有 mid + ht_score 仍为 NULL 的比赛, 逐场调 fetch_match_odds 取 S1,
      写回 matches.ht_score_home/away 与 match_outcomes.ht_score_home/away (若有该行).
分批 + 限流(sleep) 避免触发乐鱼 API 频率限制.

用法:
  python scripts/backfill_ht_scores.py --limit 50            # 试跑50场
  python scripts/backfill_ht_scores.py --batch-sleep 0.15    # 全量, 每50场sleep0.15s
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3, time, json, argparse
import gq.auto_collector as g

DB = "data/events.db"
OUT = "data/backfilled_ht_scores.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0=全量")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--batch-sleep", type=float, default=0.0, help="每 batch 休眠秒数")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute("""
        SELECT m.mid, m.match_key FROM matches m
        WHERE m.status='finished' AND m.mid IS NOT NULL
          AND m.ht_score_home IS NULL
        ORDER BY m.kickoff DESC
    """).fetchall()
    if args.limit:
        rows = rows[:args.limit]
    print(f"[INFO] 待回填场数: {len(rows)}")

    done, failed = [], []
    t0 = time.time()
    for i, (mid, mk) in enumerate(rows):
        try:
            dec = g.fetch_match_odds(mid)
            m_list = dec.get("data", []) if isinstance(dec, dict) else []
            if not m_list:
                failed.append((mid, "empty_detail")); continue
            m = m_list[0]
            sh, sa, hsh, hsa = g._score_from_msc(m.get("msc"))
            if hsh is None or hsa is None:
                failed.append((mid, "no_S1")); continue
            cur.execute("UPDATE matches SET ht_score_home=?, ht_score_away=? WHERE match_key=?",
                        (hsh, hsa, mk))
            cur.execute("UPDATE match_outcomes SET ht_score_home=?, ht_score_away=? WHERE mid=?",
                        (hsh, hsa, mid))
            con.commit()
            done.append((mid, int(hsh), int(hsa)))
        except Exception as e:
            failed.append((mid, str(e)[:60]))
        if args.batch_sleep and (i + 1) % args.batch_size == 0:
            print(f"  ...{i+1}/{len(rows)} 已用{time.time()-t0:.0f}s")
            time.sleep(args.batch_sleep)

    json.dump({"done": done, "failed": failed},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[DONE] 成功 {len(done)} 场, 失败 {len(failed)} 场, 总耗时 {time.time()-t0:.0f}s")
    if failed[:10]:
        print("  失败样例:", failed[:10])


if __name__ == "__main__":
    main()
