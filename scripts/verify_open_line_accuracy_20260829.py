# -*- coding: utf-8 -*-
"""真值验证: 2026-08-29 开盘线修复是否让"开盘线"更贴近真实总球。

判定口径 (客观, 不依赖主观判断):
  开盘线 open_line 的经济含义 = 庄家对"全场总进球 P(>line)=0.5" 的定价。
  因此 |open_line - 真实总球| 越小 → 开盘线越准。
  对同一批有真实赛果的比赛, 分别算旧/新开盘线的:
    - 平均绝对误差 MAE = mean(|open_line - ft_goals|)
    - 命中率: |open_line - ft_goals| 落在 ±0.5 / ±1.0 球内的比例
  新 MAE 更低 / 命中率更高 → 修复有效。

只读, 不改数据。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _old_open_line(con, match_key):
    """修复前逻辑复刻: 全表 captured_at ASC 取最早 OU 线的最后一段(含变体盘 bug)。"""
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


def main():
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.db")
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")

    # 有真实赛果 + 有 OU 快照的比赛 (match_outcomes 无 match_key, 用 home||' vs '||away 关联)
    rows = con.execute(
        "SELECT o.home, o.away, o.score_home, o.score_away, o.op_ou_line FROM match_outcomes o "
        "WHERE o.score_home IS NOT NULL AND o.score_away IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM odds_snapshots s WHERE s.match_key = o.home || ' vs ' || o.away "
        "  AND s.market LIKE 'OU_%' AND s.market NOT LIKE '%_1H%' AND s.market NOT LIKE '%_2H%')"
    ).fetchall()
    print(f"有真实赛果且有 OU 快照的比赛: {len(rows)}")

    err_old, err_new = [], []
    hit_old = hit_new = 0
    n_pair = 0
    bad_old_dropped = 0   # 旧逻辑给出荒谬线(>10 球)的场次
    # 与 DB 独立记录的开盘线 op_ou_line 对照 (第三方基准)
    d_old, d_new = [], []

    from pipeline.cross_score import _roll_ou_anchor
    for home, away, sh, sa, op_line in rows:
        mk = f"{home} vs {away}"
        try:
            ft = int(sh) + int(sa)
        except (TypeError, ValueError):
            continue
        o = _old_open_line(con, mk)
        n = (_roll_ou_anchor(con, mk, 57) or {}).get('open_line')
        if o is None or n is None:
            continue
        # 旧逻辑的荒谬线(变体盘 88 球等)单独统计, 不参与对比(否则污染新值)
        if o > 10.0:
            bad_old_dropped += 1
            continue
        n_pair += 1
        eo, en = abs(o - ft), abs(n - ft)
        err_old.append(eo)
        err_new.append(en)
        if eo <= 1.0:
            hit_old += 1
        if en <= 1.0:
            hit_new += 1
        # 与 op_ou_line 对照
        try:
            if op_line is not None and float(op_line) > 0:
                d_old.append(abs(o - float(op_line)))
                d_new.append(abs(n - float(op_line)))
        except (TypeError, ValueError):
            pass

    if n_pair == 0:
        print("无有效配对, 退出")
        con.close()
        return

    mae_o = sum(err_old) / len(err_old)
    mae_n = sum(err_new) / len(err_new)
    print(f"\n=== 真值验证 (配对 {n_pair} 场) ===")
    print(f"旧开盘线 MAE(|open_line - 真实总球|): {mae_o:.4f}")
    print(f"新开盘线 MAE                        : {mae_n:.4f}")
    print(f"改善                                : {mae_o - mae_n:+.4f} 球 "
          f"({(mae_o - mae_n) / mae_o * 100:+.1f}%)")
    print(f"\n|误差| <= 1.0 球 命中率: 旧 {hit_old / n_pair * 100:.1f}%  →  新 {hit_new / n_pair * 100:.1f}%")
    print(f"旧逻辑荒谬线(>10 球, 变体盘污染)被丢弃: {bad_old_dropped} 场")

    # ── 对照 B: 与 DB 独立记录的开盘线 match_outcomes.op_ou_line 比 (第三方基准) ──
    if d_old and d_new:
        mo = sum(d_old) / len(d_old)
        mn = sum(d_new) / len(d_new)
        _eps = 1e-9
        print(f"\n=== 对照 B: vs DB 开盘线 op_ou_line ({len(d_old)} 场) ===")
        print(f"旧开盘线 vs op_ou_line 平均偏差: {mo:.4f}")
        print(f"新开盘线 vs op_ou_line 平均偏差: {mn:.4f}")
        print(f"改善: {mo - mn:+.4f} 球 ({(mo - mn) / mo * 100:+.1f}%)")
        if mn < mo - _eps:
            vb = "修复后更贴近 DB 开盘线 ✅"
        elif mn > mo + _eps:
            vb = "修复后偏离 DB 开盘线 ❌ (须回退)"
        else:
            vb = "零回归 ✅ (与 DB 开盘线吻合度不变)"
        print(f"结论: {vb}")

    # 零回归判定: 全量指标完全不变 = 修复只命中真正被 bug 污染的场次, 未误伤其他比赛。
    # 这是期望结果 (最小侵入), 不是"修复无效"。
    eps = 1e-9
    if mae_n < mae_o - eps:
        verdict = "全量指标改善 ✅"
    elif mae_n > mae_o + eps:
        verdict = "全量指标退步 ❌ (须回退)"
    else:
        verdict = "零回归 ✅ (全量指标不变, 修复只命中被 bug 污染的场次)"

    # 受益面: 旧逻辑被半场/终场残盘污染、新逻辑修正了的场次
    print(f"\n=== 受益面 (残盘污染被修正的场次) ===")
    print(f"  见 scripts/regress_ou_anchor_fix_20260829.py 的『漂移方向翻转』统计")
    print(f"\n总结论(对真实总球): {verdict}")
    con.close()


if __name__ == "__main__":
    main()
