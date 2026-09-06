# -*- coding: utf-8 -*-
"""
世界杯2026 缺场补录脚本 (data backfill)
总工: 赵统筹 | 2026-07-06

设计原则:
  - Single source of truth = data/api_cache/wc26__get_games.json (worldcup26.ir 缓存)
  - 数据驱动: 动态计算 DB 缺口, 不硬编码场次
  - 中文队名去重: 统一用中文队名录入, 避免中英双行
  - 安全: DRY_RUN 默认 True (只打印不写库); 真实模式先备份 DB; 幂等(已存在跳过)
  - C 类(API 字段缺失) 不编造, 仅报告

用法:
  python data/backfill_wc2026.py            # dry-run, 打印将要做的操作
  python data/backfill_wc2026.py --apply    # 真实写入 (先自动备份 DB)
"""
import json
import os
import sys
import shutil
import sqlite3
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "football_data.db")
API_PATH = os.path.join(ROOT, "data", "api_cache", "wc26__get_games.json")
LEAGUE_ID = 2000
LEAGUE_NAME = "世界杯"

# EN -> CN 队名映射 (覆盖 API 全部 48 队; 重复中文名统一用已录入简称)
EN2CN = {
    "Algeria": "阿尔及利亚", "Argentina": "阿根廷", "Australia": "澳大利亚",
    "Austria": "奥地利", "Belgium": "比利时", "Bosnia and Herzegovina": "波黑",
    "Brazil": "巴西", "Canada": "加拿大", "Cape Verde": "佛得角",
    "Colombia": "哥伦比亚", "Croatia": "克罗地亚", "Curaçao": "库拉索",
    "Czech Republic": "捷克", "Democratic Republic of the Congo": "民主刚果",
    "Ecuador": "厄瓜多尔", "Egypt": "埃及", "England": "英格兰", "France": "法国",
    "Germany": "德国", "Ghana": "加纳", "Haiti": "海地", "Iran": "伊朗",
    "Iraq": "伊拉克", "Ivory Coast": "科特迪瓦", "Japan": "日本", "Jordan": "约旦",
    "Mexico": "墨西哥", "Morocco": "摩洛哥", "Netherlands": "荷兰",
    "New Zealand": "新西兰", "Norway": "挪威", "Panama": "巴拿马",
    "Paraguay": "巴拉圭", "Portugal": "葡萄牙", "Qatar": "卡塔尔",
    "Saudi Arabia": "沙特", "Scotland": "苏格兰", "Senegal": "塞内加尔",
    "South Africa": "南非", "South Korea": "韩国", "Spain": "西班牙",
    "Sweden": "瑞典", "Switzerland": "瑞士", "Tunisia": "突尼斯",
    "Turkey": "土耳其", "United States": "美国", "Uruguay": "乌拉圭",
    "Uzbekistan": "乌兹别克",
}


def is_cn(s):
    return bool(s) and any('\u4e00' <= c <= '\u9fff' for c in s)


def compute_final(h, a):
    if h > a:
        return "H"
    if h < a:
        return "A"
    return "D"


def parse_api_game(g):
    """返回 (date_cn, home_cn, away_cn, hs, as_, matchday, match_time) 或 None(字段缺失)."""
    if not g.get("finished"):
        return None
    en_h = g.get("home_team_name_en")
    en_a = g.get("away_team_name_en")
    hs = g.get("home_score")
    as_ = g.get("away_score")
    if not en_h or not en_a or hs is None or as_ is None:
        return None  # C 类: 缺字段
    if en_h not in EN2CN or en_a not in EN2CN:
        return None  # 映射缺失, 跳过防错
    ld = g.get("local_date", "")
    try:
        dpart = ld.split()[0]            # '06/11/2026'
        dt = datetime.datetime.strptime(dpart, "%m/%d/%Y")
        date_cn = dt.strftime("%Y-%m-%d")
    except Exception:
        return None
    tpart = ld.split()[1] if len(ld.split()) > 1 else None
    return (date_cn, EN2CN[en_h], EN2CN[en_a], int(hs), int(as_),
            g.get("matchday"), tpart)


CN2EN = {v: k for k, v in EN2CN.items()}


def canon(name):
    """中英文队名 -> canonical 中文名 (同一队无论中/英录入都映射到同一 key)."""
    if name in EN2CN:
        return EN2CN[name]
    if name in CN2EN:
        return name
    return name


def build_db_index(con):
    """返回:
       any_rows: set of (date, canon_h, canon_a) 任意存在的行 (含英文行映射)
       final_rows: set of (date, canon_h, canon_a) final_result 已填的行
       cn_id: cn_name -> team_id
    """
    cur = con.cursor()
    cur.execute("SELECT match_date, home_team_name, away_team_name, home_score, "
                "away_score, final_result FROM matches WHERE league_name=?",
                (LEAGUE_NAME,))
    any_rows, final_rows = set(), set()
    for d, h, a, hs, as_, fr in cur.fetchall():
        if h and a and d and hs is not None and as_ is not None:
            key = (d, canon(h), canon(a))
            any_rows.add(key)
            if fr is not None:
                final_rows.add(key)
    cn_id = {}
    for col_n, col_i in (("home_team_name", "home_team_id"),
                         ("away_team_name", "away_team_id")):
        cur.execute(f"SELECT DISTINCT {col_n}, {col_i} FROM matches "
                    f"WHERE league_name=? AND {col_n} IS NOT NULL", (LEAGUE_NAME,))
        for name, tid in cur.fetchall():
            if is_cn(name):
                cn_id.setdefault(name, tid)
    return any_rows, final_rows, cn_id


def main():
    apply = "--apply" in sys.argv
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # 备份 (仅真实模式)
    if apply:
        bak = DB_PATH + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(DB_PATH, bak)
        print(f"[BACKUP] 已备份 DB -> {bak}")

    api = json.load(open(API_PATH, encoding="utf-8"))["games"]
    any_rows, final_rows, cn_id = build_db_index(con)

    parsed = [pg for pg in (parse_api_game(g) for g in api) if pg is not None]
    c_class = len(api) - len(parsed) - sum(1 for g in api if not g.get("finished"))

    to_insert, to_update = [], []
    for pg in parsed:
        date_cn, h_cn, a_cn, hs, as_, md, t = pg
        key = (date_cn, h_cn, a_cn)
        if key not in any_rows:
            to_insert.append(pg)
        elif key in any_rows and key not in final_rows:
            to_update.append(pg)

    print(f"[SCAN] API finished 可解析: {len(parsed)} | C类(缺字段)跳过: {c_class}")
    print(f"[SCAN] A类(整场缺失, 待INSERT): {len(to_insert)}")
    print(f"[SCAN] B类(有比分缺final_result, 待UPDATE): {len(to_update)}")
    print()

    if not apply:
        print("=== DRY-RUN 预览 (加 --apply 才真实写入) ===")
        for pg in to_insert:
            print(f"  INSERT {pg[0]} {pg[1]} vs {pg[2]} {pg[3]}-{pg[4]} "
                  f"md={pg[5]} -> {compute_final(pg[3],pg[4])}")
        for pg in to_update:
            print(f"  UPDATE {pg[0]} {pg[1]} vs {pg[2]} {pg[3]}-{pg[4]} "
                  f"-> final_result={compute_final(pg[3],pg[4])}")
        con.close()
        return

    # ---- 真实写入 ----
    cur.execute("SELECT COALESCE(MAX(match_id), 0) FROM matches")
    next_id = cur.fetchone()[0] + 1
    cur.execute("SELECT COALESCE(MAX(home_team_id), 0) FROM matches")
    next_tid = cur.fetchone()[0] + 1

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ins_n = upd_n = 0
    for pg in to_insert:
        date_cn, h_cn, a_cn, hs, as_, md, t = pg
        hid = cn_id.get(h_cn) or next_tid; next_tid += (0 if h_cn in cn_id else 1)
        aid = cn_id.get(a_cn) or next_tid; next_tid += (0 if a_cn in cn_id else 1)
        fr = compute_final(hs, as_)
        cur.execute(
            "INSERT INTO matches (match_id, match_date, match_time, league_id, "
            "league_name, home_team_id, home_team_name, away_team_id, away_team_name, "
            "home_score, away_score, final_result, status, matchday, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (next_id, date_cn, t, LEAGUE_ID, LEAGUE_NAME, hid, h_cn, aid, a_cn,
             hs, as_, fr, "finished", md, now, now))
        next_id += 1
        ins_n += 1
        print(f"  INSERT #{next_id-1} {date_cn} {h_cn} vs {a_cn} "
              f"{hs}-{as_} -> {fr}")

    for pg in to_update:
        date_cn, h_cn, a_cn, hs, as_, md, t = pg
        fr = compute_final(hs, as_)
        h_en = CN2EN.get(h_cn, h_cn)
        a_en = CN2EN.get(a_cn, a_cn)
        cur.execute(
            "UPDATE matches SET final_result=?, updated_at=? "
            "WHERE league_name=? AND match_date=? AND final_result IS NULL "
            "AND ((home_team_name=? AND away_team_name=?) "
            "     OR (home_team_name=? AND away_team_name=?))",
            (fr, now, LEAGUE_NAME, date_cn, h_cn, a_cn, h_en, a_en))
        upd_n += cur.rowcount
        print(f"  UPDATE {date_cn} {h_cn} vs {a_cn} -> final_result={fr} "
              f"(rows={cur.rowcount})")

    con.commit()
    con.close()
    print(f"\n[DONE] INSERT {ins_n} 场, UPDATE {upd_n} 场")
    print("[NEXT] 重跑 run_v4_honest_backtest.py 看准确率变化")


if __name__ == "__main__":
    main()
