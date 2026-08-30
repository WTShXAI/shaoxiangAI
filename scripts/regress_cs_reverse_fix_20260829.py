"""回归验证: 2026-08-29 波胆反向根因修复 (Bug-1/3/5/6)。

修复项:
  Bug-1 cross_score Phase 4: _best 冒号格式 '1:1' → int(split('-')[0]) 抛 ValueError
       被 except 吞 → 「即时盘去水方向校正」自上线起从未生效。
  Bug-3 live_goal_probe._current_inplay_ah_odds fallback DESC → ASC (防终场残盘泄漏)。
  Bug-5 events.db 61.8% 滚球快照 minute_at 卡死 45/90 → 所有 `minute_at<=? ORDER BY
       minute_at DESC` 退化为 id DESC, 恒取终场残盘 (致命信息泄漏)。
       修: 加 kickoff 推算的 captured_at 真实时基上限 + captured_at 二级排序键。
  Bug-6 cross_score._roll_ou_anchor 当前线用 abs(line-2.5) 硬编码选线 + LIMIT 30
       跨帧聚合 → 选到 min41.9~43.2 的临时残盘 OU_2.50。
       修: 只取最新一帧 + 同线漂移优先 + 抽水最低选主盘。

验证三问:
  Q1 影响面: 多少场的开盘线/当前线/1X2 读数发生变化?
  Q2 方向性: 变化是否系统性偏向某方向 (若是, 说明引入新偏差)?
  Q3 确定性: 同一输入重复计算是否一致?

用法: PYTHONPATH=. python scripts/regress_cs_reverse_fix_20260829.py [样本场数]
"""
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")


# ── 旧逻辑复刻 (修复前) ──────────────────────────────────────────────
def _old_current_1x2(con, mk, minute):
    rows = con.execute(
        "SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
        "AND minute_at>0 AND minute_at<=? ORDER BY minute_at DESC, id DESC LIMIT 3",
        (mk, max(1, minute))).fetchall()
    if not rows:
        rows = con.execute(
            "SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "AND minute_at>0 ORDER BY minute_at ASC, id ASC LIMIT 3", (mk,)).fetchall()
    d = {}
    for sel, odds in rows:
        if odds is None or odds <= 1.01 or odds > 1000.0:
            continue
        if sel not in d:
            d[sel] = odds
    return (d.get('home'), d.get('draw'), d.get('away')) if len(d) == 3 else None


def _old_roll_cur_line(con, mk, minute):
    """复刻修复前 _roll_ou_anchor 的当前线选择 (abs(line-2.5) + LIMIT 30 跨帧)。"""
    try:
        rows = con.execute(
            "SELECT market, selection, odds FROM odds_snapshots "
            "WHERE match_key=? AND market LIKE 'OU_%' AND market NOT LIKE '%_1H%' "
            "AND market NOT LIKE '%_2H%' AND minute_at>0 AND minute_at<=? "
            "ORDER BY minute_at DESC, captured_at DESC LIMIT 30", (mk, minute)).fetchall()
    except Exception:
        return None
    latest = {}
    for mkt, sel, odds in rows or []:
        if odds and 1.01 < odds < 1000.0:
            latest.setdefault(mkt, {})[sel] = odds

    def _line_of(m):
        # 修复前: float(mkt.split('_')[-1]) —— 变体盘 'OU_0.50_88' → 88.0
        try:
            return float(str(m).split('_')[-1])
        except Exception:
            return None

    cands = [(_line_of(m), v.get('over'), v.get('under'))
             for m, v in latest.items()
             if _line_of(m) is not None and v.get('over') and v.get('under')]
    if not cands:
        return None
    cands.sort(key=lambda x: abs(x[0] - 2.5))
    return cands[0][0]


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    con = sqlite3.connect(DB, timeout=60)

    from analysis.live_goal_probe import _current_inplay_odds, _inplay_cap_ts
    from pipeline.cross_score import _roll_ou_anchor

    # 采样: 有滚球快照 + 有 kickoff 的场次
    mks = con.execute(
        "SELECT DISTINCT s.match_key FROM odds_snapshots s "
        "JOIN matches m ON m.match_key=s.match_key "
        "WHERE s.minute_at>0 AND m.kickoff IS NOT NULL AND m.kickoff!='' "
        "ORDER BY s.match_key LIMIT ?", (n * 3,)).fetchall()

    # 每场取一个"有数据"的滚球分钟 (真实时基内)
    picked = []
    for (mk,) in mks:
        if len(picked) >= n:
            break
        ko = con.execute("SELECT kickoff FROM matches WHERE match_key=?", (mk,)).fetchone()
        if not ko or not ko[0]:
            continue
        try:
            kots = _inplay_cap_ts(con, mk, 1)  # 探测 kickoff 可解析
        except Exception:
            continue
        if kots is None:
            continue
        # 用该场快照跨度推算一个中间分钟
        span = con.execute(
            "SELECT MIN(captured_at), MAX(captured_at) FROM odds_snapshots "
            "WHERE match_key=? AND minute_at>0", (mk,)).fetchone()
        if not span or not span[0] or not span[1]:
            continue
        mid_min = int(max(5, min(85, (span[1] - kots + 60) / 60.0)))
        picked.append((mk, mid_min))

    print(f"样本: {len(picked)} 场 (每场取一个有数据的滚球分钟)\n")

    st = Counter()
    line_delta = Counter()
    for mk, minute in picked:
        # ── 1X2 滚球读数 ──
        try:
            new = _current_inplay_odds(con, mk, minute)
            new_x2 = new.get('x2') if new else None
        except Exception:
            new_x2 = None
        old_x2 = _old_current_1x2(con, mk, minute)
        if old_x2 != new_x2:
            st['1x2_changed'] += 1
            if old_x2 and not new_x2:
                st['1x2_new_none'] += 1
            elif new_x2 and not old_x2:
                st['1x2_old_none'] += 1
            else:
                # 方向是否翻转 (去水比较)
                try:
                    from analysis.live_goal_probe import _dewater_1x2
                    oh, od, oa = _dewater_1x2(*old_x2)
                    nh, nd, na = _dewater_1x2(*new_x2)
                    odir = 0 if (oh >= od and oh >= oa) else (2 if oa >= od else 1)
                    ndir = 0 if (nh >= nd and nh >= na) else (2 if na >= nd else 1)
                    if odir != ndir:
                        st['1x2_dir_flip'] += 1
                except Exception:
                    pass
        else:
            st['1x2_same'] += 1

        # ── OU 当前线 ──
        try:
            r = _roll_ou_anchor(con, mk, minute)
            new_line = r.get('current_line') if r else None
        except Exception:
            new_line = None
        old_line = _old_roll_cur_line(con, mk, minute)
        if old_line != new_line:
            st['curl_changed'] += 1
            if old_line is None and new_line is not None:
                st['curl_old_none'] += 1
            elif new_line is None and old_line is not None:
                st['curl_new_none(诚实降级)'] += 1
            else:
                try:
                    d = round(float(new_line) - float(old_line), 2)
                    line_delta[d] += 1
                except Exception:
                    pass
        else:
            st['curl_same'] += 1

    print("=== Q1 影响面 ===")
    for k, v in sorted(st.items()):
        print(f"  {k:28s} {v:5d}  ({v/len(picked)*100:.1f}%)")

    print("\n=== Q2 当前线变化幅度分布 (新-旧) ===")
    for d, v in sorted(line_delta.items()):
        print(f"  {d:+.2f} 球 : {v} 场")
    if not line_delta:
        print("  (无)")

    # ── Q3 确定性: 抽样 60 场 × 3 次 ──
    print("\n=== Q3 确定性验证 (60 场 × 3 次) ===")
    bad = 0
    for mk, minute in picked[:60]:
        sig = set()
        for _ in range(3):
            try:
                r = _roll_ou_anchor(con, mk, minute)
                c = _current_inplay_odds(con, mk, minute)
                sig.add((r.get('open_line') if r else None,
                         r.get('current_line') if r else None,
                         (c.get('x2') if c else None)))
            except Exception as e:
                sig.add(('EXC', str(e)[:40]))
        if len(sig) > 1:
            bad += 1
    print(f"  不一致场数: {bad} / {min(60, len(picked))}")

    con.close()
    print("\n结论: 见上方 Q1(影响面) / Q2(方向性, 须正负大致均衡) / Q3(确定性, 须为 0)")


if __name__ == "__main__":
    main()
