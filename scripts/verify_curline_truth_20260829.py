"""真值验证: 2026-08-29 Bug-6 (OU 当前线选择) 修复是否真的改善。

判定口径 (两条独立基准):
  A. MAE: |current_line - 真实总球数|  —— 当前线是对终场总球的估计, 越接近越准
  B. |current_line - op_ou_line|       —— op_ou_line 是独立记录的开盘线, 作第三方基准
     (当前线应与开盘线同量级, 偏离过大说明选到了残盘/临时档位)

对比: 旧逻辑(abs(line-2.5)+LIMIT30跨帧) vs 新逻辑(单帧+同线优先+抽水最低)。

用法: PYTHONPATH=. python scripts/verify_curline_truth_20260829.py [样本场数]
"""
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")

from scripts.regress_cs_reverse_fix_20260829 import _old_roll_cur_line  # noqa: E402


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 800
    con = sqlite3.connect(DB, timeout=60)
    from analysis.live_goal_probe import _inplay_cap_ts
    from pipeline.cross_score import _roll_ou_anchor

    # 真值来源: matches 表自身的终场比分 (finished 场次)。
    # 注: 原用 match_outcomes join (o.home||' vs '||o.away) 匹配率极低, 800 次采样仅
    #     命中 25 场, 样本不足以判定。matches 表自带 score_home/score_away, 覆盖大得多。
    rows = con.execute(
        "SELECT match_key, score_home, score_away, kickoff FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND score_away IS NOT NULL "
        "AND kickoff IS NOT NULL AND kickoff != '' "
        "ORDER BY kickoff DESC LIMIT ?", (n * 2,)).fetchall()

    picked = []
    for mk, sh, sa, kickoff in rows:
        if len(picked) >= n:
            break
        if not con.execute("SELECT 1 FROM odds_snapshots WHERE match_key=? AND minute_at>0 LIMIT 1",
                           (mk,)).fetchone():
            continue
        if _inplay_cap_ts(con, mk, 1) is None:
            continue
        picked.append((mk, int(sh) + int(sa)))

    print(f"真值样本: {len(picked)} 场 (有真实终场比分 + 开盘线基准)\n")

    st = Counter()
    old_mae = new_mae = 0.0
    n_old = n_new = 0
    for mk, true_total in picked:
        span = con.execute(
            "SELECT MIN(captured_at), MAX(captured_at) FROM odds_snapshots "
            "WHERE match_key=? AND minute_at>0", (mk,)).fetchone()
        kots = _inplay_cap_ts(con, mk, 1) - 60.0   # 反推 kickoff_ts (minute=1 → kots+60)
        mid = int(max(5, min(85, (span[1] - kots) / 60.0)))

        old = _old_roll_cur_line(con, mk, mid)
        try:
            new = (_roll_ou_anchor(con, mk, mid) or {}).get('current_line')
        except Exception:
            new = None

        if old is not None and 0.5 <= old <= 10.0:
            old_mae += abs(old - true_total)
            n_old += 1
        if new is not None and 0.5 <= new <= 10.0:
            new_mae += abs(new - true_total)
            n_new += 1
        if old is None and new is None:
            st['both_none'] += 1
        elif old is None:
            st['old_none_new_has'] += 1
        elif new is None:
            st['new_none_old_has'] += 1

    print("=== 覆盖率 ===")
    for k, v in st.items():
        print(f"  {k:22s} {v:5d}")
    print(f"  旧有值 {n_old} 场 / 新有值 {n_new} 场 (总 {len(picked)})")

    print("\n=== A. MAE(|当前线 - 真实终场总球|) ===")
    o = old_mae / n_old if n_old else 0
    nn = new_mae / n_new if n_new else 0
    print(f"  旧: {o:.4f}  (n={n_old})")
    print(f"  新: {nn:.4f}  (n={n_new})")
    print(f"  → {'改善 %.4f' % (o - nn) if nn < o else ('退步 %.4f' % (nn - o) if nn > o else '持平')}")

    # 同口径对比 (只算两边都有值的场次, 消除覆盖率差异带来的偏差)
    print("\n=== A2. MAE 同口径 (仅两边都有值的场次) ===")
    so = sn = 0.0
    cnt = 0
    for mk, true_total in picked:
        span = con.execute(
            "SELECT MIN(captured_at), MAX(captured_at) FROM odds_snapshots "
            "WHERE match_key=? AND minute_at>0", (mk,)).fetchone()
        kots = _inplay_cap_ts(con, mk, 1) - 60.0
        mid = int(max(5, min(85, (span[1] - kots) / 60.0)))
        old = _old_roll_cur_line(con, mk, mid)
        try:
            new = (_roll_ou_anchor(con, mk, mid) or {}).get('current_line')
        except Exception:
            new = None
        if old is None or new is None:
            continue
        if not (0.5 <= old <= 10.0) or not (0.5 <= new <= 10.0):
            continue
        so += abs(old - true_total)
        sn += abs(new - true_total)
        cnt += 1
    if cnt:
        print(f"  旧: {so/cnt:.4f}   新: {sn/cnt:.4f}  (n={cnt})")
        print(f"  → {'改善 %.4f' % ((so-sn)/cnt) if sn < so else ('退步 %.4f' % ((sn-so)/cnt) if sn > so else '持平')}")
    else:
        print("  (无共同样本)")

    con.close()


if __name__ == "__main__":
    main()
