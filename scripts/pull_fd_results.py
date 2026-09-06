"""
scripts/pull_fd_results.py — 拉 football-data.org 免费档 13 赛事历史 finished 赛果

从 .env 读 FOOTBALL_DATA_API_KEY (不回显). 拉 13 赛事历史 finished 比赛(带终场比分),
落 `football_data.db.fd_matches` 表 (独立赛果源, 用于交叉验证 OU 派生线结论).

注意:
  - 免费档限 10 请求/分钟 -> 每次调用后 sleep 6.5s; 遇 429 退避 30s 重试.
  - 单调用 limit=500, 若返回满 500 视为截断 -> 按时间中点递归二分窗口, 直到单窗 <500.
  - 仅存有终场比分的 finished 比赛 (home_score/away_score 非 None).
  - 幂等: fd_match_id PRIMARY KEY, INSERT OR REPLACE.

用法:
  python scripts/pull_fd_results.py                 # 全量 13 赛事 × 2020-2026
  python scripts/pull_fd_results.py --codes SA,WC --years 2024,2025,2026   # 测试
"""
import os
import sys
import json
import time
import argparse
import sqlite3
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "football_data.db")
BASE = "https://api.football-data.org/v4"

COMPETITIONS = ["BSA", "ELC", "PL", "CL", "EC", "FL1", "BL1",
                "SA", "DED", "PPL", "CLI", "PD", "WC"]
DEFAULT_YEARS = [2020, 2021, 2022, 2023, 2024, 2025, 2026]


def load_key():
    env = {}
    with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env.get("FOOTBALL_DATA_API_KEY")


def api_get(key, url, tries=3):
    for t in range(tries):
        req = urllib.request.Request(url, headers={"X-Auth-Token": key})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("  [429 限流] 退避 30s ...")
                time.sleep(30)
                continue
            return e.code, {"error": e.read().decode("utf-8", "ignore")[:200]}
        except Exception as e:
            return -1, {"error": str(e)}
    return -2, {"error": "retries exhausted"}


def ensure_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS fd_matches (
            fd_match_id INTEGER PRIMARY KEY,
            competition_code TEXT,
            competition_name TEXT,
            home_team TEXT,
            away_team TEXT,
            utc_date TEXT,
            home_score INTEGER,
            away_score INTEGER,
            status TEXT,
            stage TEXT
        )
    """)


def parse_and_store(con, code, name, matches):
    n = 0
    for m in matches:
        hs = m.get("score", {}).get("fullTime", {}).get("home")
        aw = m.get("score", {}).get("fullTime", {}).get("away")
        if hs is None or aw is None:
            continue
        con.execute(
            """INSERT OR REPLACE INTO fd_matches
               (fd_match_id, competition_code, competition_name, home_team, away_team,
                utc_date, home_score, away_score, status, stage)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (m.get("id"), code, name,
             m.get("homeTeam", {}).get("name"),
             m.get("awayTeam", {}).get("name"),
             m.get("utcDate"),
             int(hs), int(aw),
             m.get("status"), m.get("stage")),
        )
        n += 1
    return n


def pull_window(key, con, code, name, df, dt, depth=0):
    url = (f"{BASE}/competitions/{code}/matches?status=FINISHED"
           f"&dateFrom={df}&dateTo={dt}&limit=500")
    st, m = api_get(key, url)
    if st != 200:
        print(f"  [WARN] {code} {df}~{dt} HTTP {st} {m.get('error','')[:120]}")
        return 0
    matches = m.get("matches", [])
    saved = parse_and_store(con, code, name, matches)
    # 截断检测: 返回满 500 且还有递归深度 -> 二分
    if len(matches) >= 500 and depth < 4:
        y0, y1 = int(df[:4]), int(dt[:4])
        mid_y = (y0 + y1) // 2
        mid = f"{mid_y:04d}-06-30"
        time.sleep(6.5)
        saved += pull_window(key, con, code, name, df, mid, depth + 1)
        time.sleep(6.5)
        saved += pull_window(key, con, code, name, f"{mid_y+1:04d}-01-01", dt, depth + 1)
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=",".join(COMPETITIONS),
                    help="逗号分隔赛事 code, 默认全 13")
    ap.add_argument("--years", default=",".join(str(y) for y in DEFAULT_YEARS),
                    help="逗号分隔年份, 每年来一次调用")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    years = [y.strip() for y in args.years.split(",") if y.strip()]

    key = load_key()
    if not key:
        print("NO_KEY"); sys.exit(1)

    con = sqlite3.connect(DB)
    ensure_table(con)
    total = 0
    for code in codes:
        # 取赛事名 (先查一次 competitions 太费配额, 直接拉首年带 name)
        name = code
        for y in years:
            df, dt = f"{y}-01-01", f"{y}-12-31"
            saved = pull_window(key, con, code, name, df, dt)
            con.commit()
            total += saved
            print(f"  {code} {y}: +{saved} (累计 {total})")
            time.sleep(6.5)
    con.close()
    print(f"\nDONE 总拉取存储 = {total} 场 -> {DB}::fd_matches")


if __name__ == "__main__":
    main()
