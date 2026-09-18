"""微观结构研究 R1 — 赔率漂移的动量/反转 与 跨市场领先-滞后

【P0 目标】从赔率动态中提取「独立于当前赔率水平」的预测信号。
           按项目铁律:真杠杆 = 开盘时用独立信息预测谁缩水,而不是读盘。

【前视约束(硬性)】
  预测时点 W 之前的数据只能用于预测 W 之后。本脚本用「时间分段」实现:
  段1 [最早, kickoff-6h) → 段2 [kickoff-6h, kickoff-1h) → 段3 [kickoff-1h, kickoff)
  只检验「前段 → 后段」的预测关系,严禁用后段信息解释前段。

【数据说明】
  data/events.db 的 odds_changes 是「快照差分」:采集器每次抓全盘口,与上次比较,
  变化部分落库。故同一 captured_at 会批量出现多个市场 —— 这不是逐笔 tick。

用法:
  D:/Architecture/.venv/Scripts/python.exe scripts/microstructure_r1.py --n 800
输出:
  reports/microstructure_r1_YYYYMMDD.md / .json
"""
import argparse
import json
import os
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_DB = os.path.join(ROOT, "data", "events.db")
OUT_DIR = os.path.join(ROOT, "reports")

SEG_H = [6.0, 1.0]  # 赛前分段边界(小时): [start, -6h) [-6h,-1h) [-1h, kickoff)
MIN_TICKS_PER_MARKET = 4  # 一个市场至少这么多变化才纳入


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)


def parse_kickoff(s):
    """events.db 的 matches.kickoff 是 text, 存在两种格式:
       '2026-09-16 16:30'          -> 视为本地时间(GMT+8)
       '2026-09-16T06:06:38+00:00' -> 带时区的 ISO
    返回 unix 秒(float), 解析失败返回 None。"""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        dt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M").astimezone()
        return dt.timestamp()
    except Exception:
        return None


def pick_matches(cur, n):
    """挑最近完赛且 tick 较活跃的场次。matches 表小(2.3 万行),全扫秒级。"""
    rows = cur.execute(
        """
        SELECT m.match_key, m.kickoff
        FROM matches m
        WHERE m.kickoff IS NOT NULL AND m.status IN ('finished','Finished','ft','FT')
        ORDER BY m.first_seen DESC
        LIMIT ?
        """,
        (n * 3,),
    ).fetchall()
    out = []
    for mk, ko_s in rows:
        ko = parse_kickoff(ko_s)
        if ko is not None:
            out.append((mk, ko))
    return out


def market_family(market):
    """把市场名归到大类。"""
    m = market.upper()
    if m.startswith("OU"):
        return "OU"
    if m.startswith("AH"):
        return "AH"
    if m.startswith("1X2") or m == "1X2":
        return "1X2"
    if m == "BTTS":
        return "BTTS"
    if m.startswith("CS"):
        return "CS"
    return "OTHER"


def seg_index(ts, kickoff):
    """返回该 tick 属于哪个赛前段(0,1,2);赛中的返回 None。"""
    h = (kickoff - ts) / 3600.0
    if h < 0:
        return None          # 已开赛
    if h >= SEG_H[0]:
        return 0
    if h >= SEG_H[1]:
        return 1
    return 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=800, help="目标场次数")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).astimezone()
    con = ro(EVENTS_DB)
    cur = con.cursor()

    cands = pick_matches(cur, args.n)
    print(f"[R1] 候选场次 {len(cands)}")

    # market -> 统计桶
    # 每场每市场: {family: {sel: {seg: [odds...]}}}
    agg = defaultdict(lambda: {"n": 0, "same": 0, "opp": 0, "flat": 0,
                               "drift1": [], "drift2": [], "drift3": []})
    used = 0
    tick_total = 0

    for mk, ko in cands:
        if used >= args.n:
            break
        rows = cur.execute(
            """SELECT market, selection, from_odds, to_odds, captured_at
               FROM odds_changes WHERE match_key=?
               ORDER BY captured_at""",
            (mk,),
        ).fetchall()
        if len(rows) < MIN_TICKS_PER_MARKET:
            continue

        # 按市场聚合成「每个市场的赔率时间序列」
        series = defaultdict(list)  # market -> [(ts, sel, from, to)]
        for market, sel, fo, to, ts in rows:
            series[market].append((ts, sel, fo, to))

        ok = False
        for market, evs in series.items():
            fam = market_family(market)
            if fam in ("CS", "OTHER"):
                continue
            # 每个 selection 独立成序列
            bysel = defaultdict(list)
            for ts, sel, fo, to in evs:
                bysel[sel].append((ts, fo, to))
            for sel, seq in bysel.items():
                if len(seq) < MIN_TICKS_PER_MARKET:
                    continue
                # 分段取「段末赔率」
                seg_last = {}
                seg_first = {}
                for ts, fo, to in seq:
                    s = seg_index(ts, ko)
                    if s is None:
                        continue
                    if s not in seg_first:
                        seg_first[s] = fo
                    seg_last[s] = to
                    tick_total += 1
                if 0 not in seg_last or 2 not in seg_last:
                    continue  # 需要首段与末段

                d1 = seg_last.get(0, seg_first.get(0)) - seg_first.get(0, 0)
                d2 = seg_last.get(1, seg_last[0]) - seg_last.get(0)
                d3 = seg_last[2] - seg_last.get(1, seg_last[0])

                b = agg[fam]
                b["drift1"].append(d1)
                b["drift2"].append(d2)
                b["drift3"].append(d3)
                # 动量/反转:段1 方向 vs 段3 方向
                if d1 == 0 or d3 == 0:
                    b["flat"] += 1
                elif (d1 > 0) == (d3 > 0):
                    b["same"] += 1
                else:
                    b["opp"] += 1
                b["n"] += 1
                ok = True
        if ok:
            used += 1

    con.close()

    # ---- 汇总 ----
    res = {"generated_at": now.isoformat(), "params": vars(args),
           "matches_used": used, "ticks_scanned": tick_total, "families": {}}

    print(f"[R1] 使用场次 {used} / 扫描变化 {tick_total:,}")
    print()
    print(f"{'市场':<6}{'样本':>8}{'动量%':>9}{'反转%':>9}{'持平%':>8}"
          f"{'Δ1均值':>11}{'Δ3均值':>11}{'Δ1→Δ3相关':>12}")
    print("-" * 74)

    for fam, b in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
        n = b["n"]
        if n < 30:
            continue
        same_pct = 100 * b["same"] / n
        opp_pct = 100 * b["opp"] / n
        flat_pct = 100 * b["flat"] / n
        m1 = st.mean(b["drift1"]) if b["drift1"] else 0
        m3 = st.mean(b["drift3"]) if b["drift3"] else 0
        # 段1 与 段3 的相关系数
        try:
            if len(b["drift1"]) > 1:
                corr = st.correlation(b["drift1"], b["drift3"])
            else:
                corr = None
        except Exception:
            corr = None
        res["families"][fam] = {
            "n": n, "momentum_pct": round(same_pct, 2), "reversal_pct": round(opp_pct, 2),
            "flat_pct": round(flat_pct, 2),
            "drift1_mean": round(m1, 4), "drift3_mean": round(m3, 4),
            "corr_seg1_seg3": round(corr, 4) if corr is not None else None,
        }
        cs = f"{corr:+.4f}" if corr is not None else "n/a"
        print(f"{fam:<6}{n:>8,}{same_pct:>8.2f}%{opp_pct:>8.2f}%{flat_pct:>7.2f}%"
              f"{m1:>+11.4f}{m3:>+11.4f}{cs:>12}")

    # ---- 结论 ----
    print()
    concl = []
    for fam, d in res["families"].items():
        mp = d["momentum_pct"]
        if mp >= 55:
            concl.append(f"{fam}: 动量 {mp:.1f}% (段1同向→段3同向) —— 漂移具持续性,可跟随")
        elif mp <= 45:
            concl.append(f"{fam}: 反转 {d['reversal_pct']:.1f}% —— 漂移倾向回摆,可反向")
        else:
            concl.append(f"{fam}: 动量/反转接近五五 ({mp:.1f}%) —— 无方向性信号")
    for c in concl:
        print("  •", c)
    res["conclusions"] = concl

    os.makedirs(OUT_DIR, exist_ok=True)
    jp = os.path.join(OUT_DIR, f"microstructure_r1_{now.strftime('%Y%m%d')}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n[R1] -> {jp}")


if __name__ == "__main__":
    main()
