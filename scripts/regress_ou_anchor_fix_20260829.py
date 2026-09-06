# -*- coding: utf-8 -*-
"""回归验证: 2026-08-29 三 bug 修复 (开盘线 / 当前线 / 排序) 全量影响面。

对比修复前后 _open_total_from_snapshots 与 _roll_ou_anchor 的输出差异,
统计:
  1. 有多少场比赛开盘线发生变化 (验证修复不是只在个例生效, 也不是大面积误伤)
  2. 变化方向分布 (修复后开盘线是变大还是变小 —— 应偏向"取到更早的真开盘")
  3. 当前线 in-play 回退终场的比例 (Bug-3 影响面)
  4. 漂移方向翻转的场次 (这是最致命的 —— 方向反号)

只读, 不改数据。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.live_goal_probe import _extract_line_from_market, _ok_ou_line_value, _implied_total_from_pairs  # noqa: E402
from pipeline.cross_score import _roll_ou_anchor  # noqa: E402


def _old_open_line(con, match_key):
    """修复前逻辑: 纯 captured_at ASC 取最早 OU 线 (复刻原 _roll_ou_anchor.open_rows)。"""
    try:
        rows = con.execute(
            "SELECT market FROM odds_snapshots WHERE match_key=? AND market LIKE 'OU_%' "
            "AND market NOT LIKE '%_1H%' AND market NOT LIKE '%_2H%' "
            "ORDER BY captured_at ASC LIMIT 5",
            (match_key,),
        ).fetchall()
        for (mkt,) in rows:
            try:
                return float(mkt.split('_')[-1])
            except Exception:
                continue
    except Exception:
        pass
    return None


def _old_current_line(con, match_key):
    """修复前逻辑: 不限 minute_at 取最新帧, 排序 key=-abs(line-2.5) 取 [0] (复刻原逻辑)。"""
    try:
        rows = con.execute(
            "SELECT market, selection, odds FROM odds_snapshots "
            "WHERE match_key=? AND market LIKE 'OU_%' AND market NOT LIKE '%_1H%' "
            "AND market NOT LIKE '%_2H%' ORDER BY captured_at DESC LIMIT 30",
            (match_key,),
        ).fetchall()
        latest = {}
        for mkt, sel, odds in rows:
            if odds and 1.01 < odds < 1000.0:
                latest.setdefault(mkt, {})[sel] = odds

        def _line_of(m):
            try:
                return float(m.split('_')[-1])
            except Exception:
                return None

        lines = [(_line_of(m), v.get('over'), v.get('under')) for m, v in latest.items() if _line_of(m)]
        lines = [x for x in lines if x[0] is not None and x[0] >= 1.0]
        if not lines:
            return None
        lines.sort(key=lambda x: -abs(x[0] - 2.5))   # 原 bug: 加了负号
        return lines[0][0]
    except Exception:
        return None


def main():
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.db")
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")

    # 有 OU 快照的比赛全集
    mks = [r[0] for r in con.execute(
        "SELECT DISTINCT match_key FROM odds_snapshots WHERE market LIKE 'OU_%' "
        "AND market NOT LIKE '%_1H%' AND market NOT LIKE '%_2H%'"
    ).fetchall()]
    print(f"有 OU 快照的比赛数: {len(mks)}")

    n_open_changed = 0
    n_cur_changed = 0
    n_cur_none_now = 0      # 修复后当前线为 None (诚实降级)
    n_drift_flip = 0        # 漂移方向翻转 (最致命)
    n_drift_gone = 0        # 修复前 drift 有值, 修复后 None
    n_drift_new = 0         # 修复前 None, 修复后有值
    open_delta_hist = {}
    examples = []

    for i, mk in enumerate(mks):
        if i % 500 == 0 and i:
            print(f"  ... {i}/{len(mks)}")
        new = _roll_ou_anchor(con, mk, 57) or {}
        old_open = _old_open_line(con, mk)
        old_cur = _old_current_line(con, mk)
        new_open, new_cur = new.get('open_line'), new.get('current_line')

        if old_open is not None and new_open is not None and abs(old_open - new_open) > 1e-9:
            n_open_changed += 1
            d = round(new_open - old_open, 2)
            open_delta_hist[d] = open_delta_hist.get(d, 0) + 1
            if len(examples) < 10:
                examples.append((mk, old_open, new_open))
        if (old_cur is None) != (new_cur is None):
            if new_cur is None:
                n_cur_none_now += 1
        elif old_cur is not None and new_cur is not None and abs(old_cur - new_cur) > 1e-9:
            n_cur_changed += 1

        old_drift = (old_cur - old_open) if (old_cur is not None and old_open is not None) else None
        new_drift = new.get('drift')
        if old_drift is not None and new_drift is None:
            n_drift_gone += 1
        elif old_drift is None and new_drift is not None:
            n_drift_new += 1
        elif (old_drift is not None and new_drift is not None
              and abs(old_drift) >= 0.25 and abs(new_drift) >= 0.25
              and (old_drift > 0) != (new_drift > 0)):
            n_drift_flip += 1

    print("\n=== 回归结果 ===")
    print(f"开盘线发生变化      : {n_open_changed} 场 ({n_open_changed/max(1,len(mks))*100:.1f}%)")
    print(f"当前线发生变化      : {n_cur_changed} 场")
    print(f"当前线诚实降级(None): {n_cur_none_now} 场 (Bug-3 影响面: 原会回退终场)")
    print(f"漂移方向翻转        : {n_drift_flip} 场  ← 最致命, 修复前方向完全反号")
    print(f"漂移消失(降级)      : {n_drift_gone} 场")
    print(f"漂移新增            : {n_drift_new} 场")
    # ── 确定性验证: 修复前"取全表最早那条"依赖物理行序(id), 同帧多线时无业务语义。
    #    修复后应完全确定 (同场查 3 次结果一致)。
    n_nondet = 0
    det_sample = mks[:200]
    for mk in det_sample:
        rs = [(_roll_ou_anchor(con, mk, 57) or {}).get('open_line') for _ in range(3)]
        if len(set(str(x) for x in rs)) > 1:
            n_nondet += 1
    print(f"\n确定性验证 (抽样 {len(det_sample)} 场 × 查 3 次)")
    print(f"  结果不一致的场次: {n_nondet}  (修复前依赖 id 顺序, 修复后应为 0)")

    print("\n开盘线变化量分布 (新-旧, top10):")
    for d, c in sorted(open_delta_hist.items(), key=lambda x: -x[1])[:10]:
        print(f"  {d:+.2f} 球 : {c} 场")
    if examples:
        print("\n变化样例 (match_key, 旧开盘, 新开盘):")
        for mk, o, n in examples[:10]:
            print(f"  {mk}: {o} -> {n}")
    con.close()


if __name__ == "__main__":
    main()
