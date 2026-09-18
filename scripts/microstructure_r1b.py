"""微观结构研究 R1b — 赛前赔率漂移对赛果的预测力(含市场基线对照)

【动机】R1 已证伪「分段动量/反转」可交易假设(1200 场次下接近五五开)。
        本脚本转向更直接的检验:赛前净漂移方向 能否预测赛果?
        并设「收盘赔率隐含概率」为基线 —— 若漂移无增量,两者应接近。

【前视约束】只使用赛前数据(captured_at < kickoff)预测赛果,天然无前视。
            严禁使用赛中快照。

【数据过滤】IR-04 假 0-0:使用 score_missing=0 的记录。
            格式铁律:比分以主队/客队分列,不解析字符串。

用法:
  D:/Architecture/.venv/Scripts/python.exe scripts/microstructure_r1b.py --n 3000
输出:
  reports/microstructure_r1b_YYYYMMDD.json
"""
import argparse
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_DB = os.path.join(ROOT, "data", "events.db")
OUT_DIR = os.path.join(ROOT, "reports")


def parse_kickoff(s):
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
        return datetime.strptime(s[:16], "%Y-%m-%d %H:%M").astimezone().timestamp()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3000)
    args = ap.parse_args()

    now = datetime.now(timezone.utc).astimezone()
    con = sqlite3.connect(f"file:{EVENTS_DB}?mode=ro", uri=True, timeout=15)
    cur = con.cursor()

    rows = cur.execute("""
        SELECT match_key, kickoff,
               CAST(score_home AS INTEGER), CAST(score_away AS INTEGER)
        FROM matches
        WHERE status='finished'
          AND score_home IS NOT NULL AND score_away IS NOT NULL
          AND COALESCE(score_missing,0)=0
        ORDER BY first_seen DESC LIMIT ?
    """, (args.n * 2,)).fetchall()

    print(f"[R1b] 候选完赛且比分可信 {len(rows)}")

    stat = {
        "n_matches": 0, "n_with_odds": 0,
        "drift_hit": 0, "drift_miss": 0,       # 漂移方向预测主胜 vs 实际
        "base_hit": 0, "base_miss": 0,         # 基线: 收盘最低赔率方
        "drift_on_nonhome": 0,
        "flat": 0,
    }

    for mk, ko_s, sh, sa in rows:
        if stat["n_with_odds"] >= args.n:
            break
        ko = parse_kickoff(ko_s)
        if ko is None:
            continue

        evs = cur.execute("""
            SELECT selection, from_odds, to_odds, captured_at
            FROM odds_changes
            WHERE match_key=? AND market='1X2' AND captured_at < ?
            ORDER BY captured_at
        """, (mk, ko)).fetchall()
        if len(evs) < 6:
            continue

        seq = defaultdict(list)
        for sel, fo, to, ts in evs:
            seq[sel].append((ts, fo, to))

        # 需要 home / away 都在
        sels = {s.lower(): v for s, v in seq.items()}
        if "home" not in sels or "away" not in sels:
            continue
        if len(sels["home"]) < 2 or len(sels["away"]) < 2:
            continue

        h_start, h_end = sels["home"][0][1], sels["home"][-1][2]
        a_start, a_end = sels["away"][0][1], sels["away"][-1][2]

        drift_h = h_end - h_start      # 负 = 主队赔率下降 = 资金看好主队
        drift_a = a_end - a_start

        stat["n_with_odds"] += 1
        home_win = sh > sa

        # ---- 信号1: 漂移方向 ----
        if abs(drift_h) < 1e-9 and abs(drift_a) < 1e-9:
            stat["flat"] += 1
        else:
            # 用「赔率下降的一方」作为预测
            pred_home = drift_h < drift_a   # 主队赔率相对降得更多 => 看好主队
            if pred_home == home_win:
                stat["drift_hit"] += 1
            else:
                stat["drift_miss"] += 1
                if home_win:
                    stat["drift_on_nonhome"] += 1

        # ---- 基线: 收盘赔率更低的一方 ----
        if abs(h_end - a_end) > 1e-9:
            base_home = h_end < a_end
            if base_home == home_win:
                stat["base_hit"] += 1
            else:
                stat["base_miss"] += 1

    con.close()

    d_n = stat["drift_hit"] + stat["drift_miss"]
    b_n = stat["base_hit"] + stat["base_miss"]
    d_acc = 100 * stat["drift_hit"] / d_n if d_n else 0
    b_acc = 100 * stat["base_hit"] / b_n if b_n else 0

    print(f"[R1b] 有效场次 {stat['n_with_odds']}  (其中漂移持平 {stat['flat']})")
    print()
    print(f"  信号:赛前净漂移方向  -> 预测主胜方向")
    print(f"      样本 {d_n:,}  命中 {stat['drift_hit']:,}  准确率 {d_acc:.2f}%")
    print(f"  基线:收盘赔率较低方  -> 预测主胜方向")
    print(f"      样本 {b_n:,}  命中 {stat['base_hit']:,}  准确率 {b_acc:.2f}%")
    print()
    dd = d_acc - b_acc
    print(f"  增量 = 漂移 - 基线 = {dd:+.2f}pp")
    if dd <= 0:
        verdict = "漂移方向相对收盘赔率无增量信息 —— 属读盘,非独立信号"
    elif dd < 2:
        verdict = f"漂移略优于基线 {dd:+.2f}pp,幅度小,需 OOS 确认后方可讨论"
    else:
        verdict = f"漂移优于基线 {dd:+.2f}pp,值得进一步 OOS 检验"
    print(f"  判定: {verdict}")
    print()
    print("  注: 两者均未扣抽水。准确率≠+EV,须过五道关方可论及交易性。")

    res = {
        "generated_at": now.isoformat(), "params": vars(args),
        "matches_with_odds": stat["n_with_odds"], "flat": stat["flat"],
        "drift": {"n": d_n, "hit": stat["drift_hit"], "accuracy_pct": round(d_acc, 3)},
        "baseline": {"n": b_n, "hit": stat["base_hit"], "accuracy_pct": round(b_acc, 3)},
        "increment_pp": round(dd, 3), "verdict": verdict,
        "caveat": "未扣抽水;准确率不等于 +EV;须过五道关",
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    jp = os.path.join(OUT_DIR, f"microstructure_r1b_{now.strftime('%Y%m%d')}.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n[R1b] -> {jp}")


if __name__ == "__main__":
    main()
