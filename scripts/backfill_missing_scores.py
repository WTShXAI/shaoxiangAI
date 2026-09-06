"""
补全缺失终比分：让"有赔率但未完赛 / live-only 无标签"的比赛成为可学习样本。

策略（local-first，web 兜底）：
  1. 从 football_data.db (本地历史结果库, 中文队名) 按 (主队,客队,日期±2天) 精确/规范化匹配。
  2. 命中 -> 回填 matches(score_home/away,status,result,is_override) + match_outcomes(插入标签行, source='backfill')。
  3. 未命中 -> 写入 residual CSV，等待联网补全。

用法:
  python scripts/backfill_missing_scores.py --dry-run        # 仅统计+产出 plan/residual CSV
  python scripts/backfill_missing_scores.py --execute        # 真正写库
"""
import sqlite3, argparse, csv, re, json
from datetime import datetime, timedelta

GQ = "data/events.db"
FD = "data/football_data.db"
PLAN_CSV = "data/backfill_plan.csv"
RESIDUAL_CSV = "data/backfill_residual.csv"

def norm(t):
    if not t:
        return ""
    t = str(t).strip().lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[\(\)\[\]（）\s]", "", t)
    # 去掉常见后缀/前缀噪声
    for suf in ["fc", "afc", "cf", "sk", "ac", "bk", "fk", "uk", "sc", "cfc", "utd", "united", "city"]:
        if t.endswith(suf) and len(t) > len(suf) + 1:
            t = t[: -len(suf)]
    return t

def load_fd():
    fd = sqlite3.connect(FD, timeout=60)
    f = fd.cursor()
    d = {}
    specs = [("matches", "home_team_name", "away_team_name"),
             ("historical_matches", "home_team", "away_team")]
    for tbl, hc, ac in specs:
        try:
            f.execute(f"SELECT {hc},{ac},match_date,home_score,away_score,final_result,league_name FROM {tbl}")
        except Exception as e:
            print("skip", tbl, e); continue
        for h, a, dt, hs, as_, fr, lg in f.fetchall():
            if hs is None:
                continue
            d[(norm(h), norm(a), str(dt)[:10])] = (hs, as_, fr, lg)
    fd.close()
    return d

def parse_date(ko):
    if not ko:
        return None
    ko = str(ko).replace("T", " ").replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(ko, fmt)
        except Exception:
            pass
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="写库；默认 dry-run")
    args = ap.parse_args()
    dry = not args.execute

    fd = load_fd()
    print(f"[fd] 结果库载入: {len(fd)} 行")

    con = sqlite3.connect(GQ, timeout=180)
    c = con.cursor()

    # targets: 任何有 odds_snapshots 的 match_key, 但 match_outcomes 缺少(有效 result + score)
    c.execute("""
    CREATE TEMP TABLE targets AS
    SELECT DISTINCT sf.match_key,
           m.kickoff, m.status, m.score_home, m.score_away, m.league,
           m.home, m.away
    FROM (SELECT match_key FROM odds_snapshots GROUP BY match_key) sf
    LEFT JOIN matches m ON m.match_key = sf.match_key
    LEFT JOIN match_outcomes mo ON mo.home||' vs '||mo.away = sf.match_key
    WHERE mo.result IS NULL OR mo.score_home IS NULL
    """)
    c.execute("SELECT COUNT(*) FROM targets")
    total = c.fetchone()[0]
    print(f"[targets] 有赔率但缺有效标签的比赛: {total}")

    c.execute("SELECT match_key, kickoff, status, home, away, league FROM targets")
    rows = c.fetchall()

    matched, unmatched, skipped = 0, 0, 0
    plan = []
    residual = []
    for mk, ko, st, home, away, lg in rows:
        if " vs " not in mk:
            unmatched += 1; continue
        h, a = mk.split(" vs ", 1)
        d = parse_date(ko)
        if d is None:
            unmatched += 1; residual.append((mk, "", st, "no_kickoff")); continue
        hit = None
        for off in (0, 1, -1, 2, -2):
            cand = (d + timedelta(days=off)).strftime("%Y-%m-%d")
            key = (norm(h), norm(a), cand)
            if key in fd:
                hit = fd[key]; break
            key2 = (norm(a), norm(h), cand)
            if key2 in fd:
                hs, as_, fr, lg2 = fd[key2]
                hit = (as_, hs, {"H": "A", "A": "H", "D": "D"}.get(fr, fr), lg2); break
        if hit:
            hs, as_, fr, lg2 = hit
            matched += 1
            plan.append((mk, h, a, str(ko)[:10], lg or lg2, hs, as_, fr))
        else:
            unmatched += 1
            residual.append((mk, str(ko)[:10], st, "no_fd_match"))

    # 写 plan / residual CSV
    with open(PLAN_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["match_key", "home", "away", "date", "league", "score_home", "score_away", "result"])
        for r in plan:
            w.writerow(r)
    with open(RESIDUAL_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["match_key", "date", "status", "reason"])
        for r in residual:
            w.writerow(r)

    print(f"\n[结果] 本地可补全(local): {matched} | 需联网(residual): {unmatched} | 跳过: {skipped}")
    print(f"[写出] plan={PLAN_CSV} ({len(plan)}行)  residual={RESIDUAL_CSV} ({len(residual)}行)")

    if dry:
        print("\n[DRY-RUN] 未写库。样例本地补全:")
        for r in plan[:8]:
            print("   ", r)
        print("样例 residual:")
        for r in residual[:8]:
            print("   ", r)
        con.close()
        return

    # ---- execute: 写库 ----
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    upd_m = 0
    ins_mo = 0
    for mk, h, a, dt, lg, hs, as_, fr in plan:
        # matches
        c.execute("""
        UPDATE matches SET score_home=?, score_away=?, status='finished', result=?,
               is_override=1, override_at=?
        WHERE match_key=? AND (score_home IS NULL OR score_away IS NULL)
        """, (hs, as_, fr, now, mk))
        upd_m += c.rowcount
        # match_outcomes (插入标签行; 若已存在同 match_key 则跳过)
        c.execute("SELECT 1 FROM match_outcomes WHERE home=? AND away=?", (h, a))
        if not c.fetchone():
            c.execute("""
            INSERT INTO match_outcomes (home,away,league,kickoff,score_home,score_away,result,source,is_valid,archived_at)
            VALUES (?,?,?,?,?,?,?,'backfill',1,?)
            """, (h, a, lg, dt, hs, as_, fr, now))
            ins_mo += 1
    con.commit()
    print(f"\n[EXECUTE] matches 更新 {upd_m} 行, match_outcomes 插入 {ins_mo} 行")
    con.close()

if __name__ == "__main__":
    main()
