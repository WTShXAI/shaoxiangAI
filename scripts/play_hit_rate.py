"""
scripts/play_hit_rate.py — 四大玩法历史命中率回测 (哨响AI MVP 诚实指标)

数据来源: data/events.db `match_outcomes` 单表 (四个玩法的赔率与赛果同行, 天然对齐).

⚠ 为什么不用 odds_snapshots (2026-08-01 实测结论, 勿再重试):
  1. 该表只能按 match_key="home vs away" 用队名 join, 无日期 → 同队对多次交手串场
     (实测 66 个队对有重复行, 曼联vs切尔西 5 行).
  2. `minute_at` / `score_at` 两个赛前/滚球标记字段采集器**从未写入**
     (minute_at 全库仅 0 与 99; score_at 1176318 行为空串, 仅 5 行非空),
     所谓 "minute_at=0 赛前闸门" 是伪闸门, 滚球盘混入无法剔除.
  3. 实证: "3月1日 vs 十月十二日伊陶瓜" 赛前挂线 2.50→3.75→5.50 逐时飙升, 终场却 0-1.
  用它回测会得出「3.25 盘 81.7% 开大」这类物理上不可能的结论 (全库 ≥4 球仅占 22%).
  snapshots 仅适合「未开赛比赛取最早报价做实时展示」, 不适合历史回测.

方法: 对每场用同行赔率跑 ranked_predict, 取其 top1 推荐, 与真实赛果对比.
  - 1X2: 去水概率最高方 vs result(home/draw/away)
  - 让球(AH): ah.direction(主/客赢盘) vs win_side(line, score)
  - 大小球(OU): ou.direction(大/小) vs 实际总进球 超/低于 line
  - 波胆(CS): cs.ranked[0][0] 比分 vs 真实比分
输出: data/play_hit_rate.json (各玩法 {n, hit, rate, updated_at})
纯赛前→赛后 out-of-sample 回测, 数字真实可追溯, 不虚构.
"""
from __future__ import annotations
import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.ranked_predictor import predict
from pipeline.ah_eval import win_side
from pipeline.evaluation.ou_eval import ou_settle

_MAP1X2 = {"主胜": "home", "平局": "draw", "客胜": "away"}
DB = os.path.join(_ROOT, "data", "events.db")


def fetch_rows(limit=None):
    """取回测样本. AH/OU 赔率与赛果同行, 无需 join, 不存在串场风险."""
    con = sqlite3.connect(DB)
    cur = con.cursor()
    q = """
        SELECT home, away, result, score_home, score_away,
               op_1x2_h, op_1x2_d, op_1x2_a, op_cs,
               op_ah_line, op_ah_home, op_ah_away,
               op_ou_line, op_ou_over, op_ou_under
        FROM match_outcomes
        WHERE is_valid=1 AND result IS NOT NULL AND result!=''
          AND score_home IS NOT NULL AND score_away IS NOT NULL
          AND op_1x2_h IS NOT NULL AND op_1x2_d IS NOT NULL AND op_1x2_a IS NOT NULL
        ORDER BY rowid DESC
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    cur.execute(q)
    rows = cur.fetchall()
    con.close()
    cols = ["home", "away", "result", "sh", "sa", "h", "d", "a", "op_cs",
            "ah_line", "ah_home", "ah_away", "ou_line", "ou_over", "ou_under"]
    return [dict(zip(cols, r)) for r in rows]


def _row_ah_ou(r):
    """从同行字段取 AH/OU. ⚠ ah_line=0.0 是合法平手盘(占AH 73%), 只判 is None."""
    ah = ou = None
    try:
        if (r["ah_line"] is not None and r["ah_home"] and r["ah_away"]
                and float(r["ah_home"]) > 1.01 and float(r["ah_away"]) > 1.01):
            ah = {"line": float(r["ah_line"]), "home": float(r["ah_home"]),
                  "away": float(r["ah_away"])}
    except Exception:
        ah = None
    try:
        if (r["ou_line"] and float(r["ou_line"]) > 0 and r["ou_over"] and r["ou_under"]
                and float(r["ou_over"]) > 1.01 and float(r["ou_under"]) > 1.01):
            ou = {"line": float(r["ou_line"]), "over": float(r["ou_over"]),
                  "under": float(r["ou_under"])}
    except Exception:
        ou = None
    return ah, ou


def evaluate(rows):
    stat = {"1x2": {"n": 0, "hit": 0}, "ah": {"n": 0, "hit": 0},
            "ou": {"n": 0, "hit": 0}, "cs": {"n": 0, "hit": 0}}
    # naive 基线: 最笨的固定策略. 模型命中率必须与之并排展示 —— 否则会把
    # 「样本偏差」误读成「模型能力」(2026-08-01 实测: OU 模型61.5% vs 无脑买大60.7%,
    #  真实增量仅 +0.8pp, 小于标准误 ±1.6pp, 即模型对 OU 目前零增量).
    base = {"1x2": {"n": 0, "hit": 0}, "ah": {"n": 0, "hit": 0},
            "ou": {"n": 0, "hit": 0}, "cs": {"n": 0, "hit": 0}}
    BASE_DESC = {"1x2": "永远买主胜", "ah": "永远买主队",
                 "ou": "永远买大球", "cs": "永远买 1-1"}
    skip = {"ah_push": 0, "ou_push": 0, "no_ah": 0, "no_ou": 0}
    # 盘口合理性自检: OU 盘口均值应 ≈ 实际总进球均值, 偏离过大说明盘口口径错(如半场盘混入)
    audit = {"ou_line_sum": 0.0, "ou_n": 0, "goals_sum": 0, "goals_n": 0}
    for r in rows:
        try:
            sh, sa = int(r["sh"]), int(r["sa"])
        except Exception:
            continue
        audit["goals_sum"] += sh + sa
        audit["goals_n"] += 1
        ah, ou = _row_ah_ou(r)
        if not ah:
            skip["no_ah"] += 1
        if not ou:
            skip["no_ou"] += 1
        else:
            audit["ou_line_sum"] += ou["line"]
            audit["ou_n"] += 1
        try:
            res = predict(r["home"], r["away"], float(r["h"]), float(r["d"]), float(r["a"]),
                          ou_line=ou["line"] if ou else None,
                          ou_over=ou["over"] if ou else None,
                          ou_under=ou["under"] if ou else None,
                          op_cs=r["op_cs"],
                          ah_line=ah["line"] if ah else None,
                          ah_home=ah["home"] if ah else None,
                          ah_away=ah["away"] if ah else None)
        except Exception:
            continue
        m = res["markets"]

        # 1X2 (基线同分母: 永远买主胜)
        top1x2 = m["1x2"]["ranked"][0][0]
        if _MAP1X2.get(top1x2) == r["result"]:
            stat["1x2"]["hit"] += 1
        stat["1x2"]["n"] += 1
        base["1x2"]["n"] += 1
        base["1x2"]["hit"] += (r["result"] == "home")

        # AH — 走水(整数盘退款)不计入分母, 否则会人为压低命中率
        a = m.get("ah")
        if a and a.get("direction"):
            win = win_side(a["line"], sh, sa)
            if win == "走水":
                skip["ah_push"] += 1
            else:
                if a["direction"] == win:
                    stat["ah"]["hit"] += 1
                stat["ah"]["n"] += 1
                base["ah"]["n"] += 1
                base["ah"]["hit"] += (win == "主队")

        # OU — direction 是英文 OVER/UNDER/NEUTRAL (ou_eval SSoT), PUSH 不计分母
        o = m.get("ou", {})
        line = o.get("line")
        direction = o.get("direction")
        if line and direction in ("OVER", "UNDER"):
            settle = ou_settle(sh + sa, float(line))
            if settle == "PUSH":
                skip["ou_push"] += 1
            else:
                if direction == settle:
                    stat["ou"]["hit"] += 1
                stat["ou"]["n"] += 1
                base["ou"]["n"] += 1
                base["ou"]["hit"] += (settle == "OVER")

        # CS (基线同分母: 永远买 1-1, 足球最高频比分)
        csr = m.get("cs", {}).get("ranked")
        if csr and csr[0]:
            if csr[0][0] == f"{sh}-{sa}":
                stat["cs"]["hit"] += 1
            stat["cs"]["n"] += 1
            base["cs"]["n"] += 1
            base["cs"]["hit"] += (f"{sh}-{sa}" == "1-1")

    out = {}
    for k, v in stat.items():
        n = v["n"]
        rate = v["hit"] / n if n else 0.0
        se = math.sqrt(0.25 / n) if n else 0.0     # 二项标准误 (p=0.5 保守)
        bn, bh = base[k]["n"], base[k]["hit"]
        brate = bh / bn if bn else 0.0
        edge = rate - brate                        # 模型相对 naive 基线的真实增量
        # 增量显著性: 两个相关比例之差, 保守用 sqrt(2)*se 作为差值标准误
        edge_se = se * math.sqrt(2) if se else 0.0
        out[k] = {
            "n": n, "hit": v["hit"], "rate": round(rate, 4), "se_pp": round(se * 100, 2),
            "baseline_rate": round(brate, 4), "baseline_desc": BASE_DESC[k],
            "edge_pp": round(edge * 100, 2),
            "edge_significant": bool(edge_se and abs(edge) >= 1.96 * edge_se),
        }
    skip["ou_line_mean"] = round(audit["ou_line_sum"] / audit["ou_n"], 3) if audit["ou_n"] else None
    skip["goals_mean"] = round(audit["goals_sum"] / audit["goals_n"], 3) if audit["goals_n"] else None
    return out, skip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="回测样本上限(用于快速验证)")
    args = ap.parse_args()
    rows = fetch_rows(args.limit)
    print(f"[play_hit_rate] 回测样本: {len(rows)} 场 (四玩法赔率与赛果同行, 无 join 串场风险)")
    res, skip = evaluate(rows)
    out = {"updated_at": datetime.now(timezone.utc).isoformat(),
           "sample_limit": args.limit, "total_matches": len(rows),
           "skipped": skip, "plays": res}
    path = os.path.join(_ROOT, "data", "play_hit_rate.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[play_hit_rate] 已写入 {path}")
    for k, v in res.items():
        mark = "✅显著" if v["edge_significant"] else "⚠不显著"
        print(f"  {k}: n={v['n']} 命中={v['rate']:.1%} (±{v['se_pp']}pp) | "
              f"基线[{v['baseline_desc']}]={v['baseline_rate']:.1%} | "
              f"增量={v['edge_pp']:+.1f}pp {mark}")
    print(f"  [降级/排除] 无AH盘={skip['no_ah']} 无OU盘={skip['no_ou']} "
          f"AH走水={skip['ah_push']} OU走水={skip['ou_push']}")
    print(f"  [盘口自检] OU盘口均值={skip['ou_line_mean']} vs 实际总进球均值={skip['goals_mean']} "
          f"(两者应接近, 偏离>0.6 说明盘口口径可疑)")


if __name__ == "__main__":
    main()
