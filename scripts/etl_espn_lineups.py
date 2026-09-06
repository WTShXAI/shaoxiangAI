#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
etl_espn_lineups.py — 从 ESPN 免费 API 拉取 WC2026 已结束场次阵容(L3数据源)
- scoreboard 区间拿 event id + 队名
- summary 取 rosters[].roster (26人, 含 starter/formationPlace)
- 建 wc_lineups 表, 经 canonical 映射对齐 matches 世界杯表
- 幂等: 仅管理 source='espn' 行
"""
import sqlite3, json, time, sys, urllib.request, urllib.error

DB = "data/football_data.db"
BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"

# ESPN 全名 -> matches.home_team_name (canonical 英文)
ESPN_MAP = {
    "Bosnia-Herzegovina": "Bosnia-H.",
    "South Korea": "Korea Republic",
    "DR Congo": "Congo DR",
    "Curacao": "Curaçao",
    "Korea Republic": "Korea Republic",
    "Ivory Coast": "Ivory Coast",
    # 其余 ESPN 全名与 matches 英文队名一致
}
# matches 残留中文 -> canonical
ZH_FIX = {"乌兹别克斯坦": "Uzbekistan", "佛得角": "Cape Verde",
          "沙特阿拉伯": "Saudi Arabia", "西班牙": "Spain"}

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))

def to_sys_name(espn_name):
    n = ESPN_MAP.get(espn_name, espn_name)
    n = ZH_FIX.get(n, n)
    return n

def main():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS wc_lineups (
        match_key TEXT PRIMARY KEY,
        home TEXT, away TEXT,
        home_formation TEXT, away_formation TEXT,
        home_starters TEXT, away_starters TEXT,
        home_bench TEXT, away_bench TEXT,
        source TEXT DEFAULT 'espn'
    )""")

    # 1) scoreboard 区间
    print("拉取 ESPN scoreboard 区间 ...")
    sb = fetch(f"{BASE}/scoreboard?dates=20260611-20260707")
    events = sb.get("events", [])
    ft = []
    for e in events:
        st = e.get("status", {}).get("type", {})
        if st.get("state") != "post":
            continue
        comp = e.get("competitions", [{}])[0]
        cs = comp.get("competitors", [])
        if len(cs) < 2:
            continue
        names = [c.get("team", {}).get("displayName") for c in cs]
        ft.append((e["id"], names[0], names[1]))
    print(f"  FT 场次: {len(ft)}")

    # 2) 批量拉 summary 取 rosters
    inserted = 0
    skipped = 0
    for eid, hn, an in ft:
        try:
            sumj = fetch(f"{BASE}/summary?event={eid}")
        except Exception as ex:
            print(f"  [warn] event {eid} summary 失败: {ex}")
            skipped += 1
            continue
        rosters = sumj.get("rosters", [])
        if len(rosters) < 2:
            skipped += 1
            continue
        row = {r.get("homeAway"): r for r in rosters}
        ha = row.get("home", {})
        aa = row.get("away", {})
        def parse(rc):
            roster = rc.get("roster", []) or []
            starters = [p for p in roster if p.get("starter")]
            bench = [p for p in roster if not p.get("starter")]
            form = rc.get("formation")
            s_names = [p.get("athlete", {}).get("displayName") for p in starters]
            b_names = [p.get("athlete", {}).get("displayName") for p in bench]
            return form, s_names, b_names
        h_form, h_s, h_b = parse(ha)
        a_form, a_s, a_b = parse(aa)
        c.execute("""INSERT OR REPLACE INTO wc_lineups
            (match_key,home,away,home_formation,away_formation,home_starters,away_starters,home_bench,away_bench,source)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (eid, to_sys_name(hn), to_sys_name(an),
             h_form, a_form,
             json.dumps(h_s, ensure_ascii=False), json.dumps(a_s, ensure_ascii=False),
             json.dumps(h_b, ensure_ascii=False), json.dumps(a_b, ensure_ascii=False),
             "espn"))
        inserted += 1
        time.sleep(0.15)

    c.commit()
    print(f"  wc_lineups 入库: {inserted} 场, 跳过: {skipped}")

    # 3) 对齐 matches 覆盖率
    print("\n=== 对齐 matches 世界杯 覆盖率 ===")
    sys_names = set()
    for r in c.execute("select distinct home_team_name from matches where league_name='世界杯'"):
        sys_names.add(r[0])
    espn_names = set()
    for r in c.execute("select distinct home,away from wc_lineups"):
        espn_names.add(r[0]); espn_names.add(r[1])
    matched = espn_names & sys_names
    print(f"  ESPN 队名数: {len(espn_names)}, 命中系统: {len(matched)}/{len(espn_names)}")
    miss = espn_names - sys_names
    if miss:
        print(f"  未命中(需补映射): {sorted(miss)}")
    # 能对齐的 (home,away) 对阵数
    pairs = c.execute("select home,away from wc_lineups").fetchall()
    hit = 0
    for h, a in pairs:
        n = c.execute("select count(*) from matches where league_name='世界杯' and ((home_team_name=? and away_team_name=?) or (home_team_name=? and away_team_name=?))",
                      (h, a, a, h)).fetchone()[0]
        hit += (1 if n > 0 else 0)
    print(f"  可对齐 matches 的对阵: {hit}/{len(pairs)}")
    c.close()

if __name__ == "__main__":
    main()
