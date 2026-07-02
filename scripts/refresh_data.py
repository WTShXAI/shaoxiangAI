"""
哨响AI 数据刷新脚本 (refresh_data.py)
=====================================
安全地导入/刷新比赛数据。带自动备份、去重、进度输出、错误隔离。

用法:
    python scripts/refresh_data.py                  # 全部刷新 (五大联赛+其他+世界杯)
    python scripts/refresh_data.py --leagues        # 仅联赛新赛季
    python scripts/refresh_data.py --worldcup       # 仅世界杯比分
    python scripts/refresh_data.py --season 2025    # 指定赛季 (默认2025, 即2025/26)
    python scripts/refresh_data.py --no-backup      # 跳过备份 (不推荐)

设计要点 (基于源码调查的精确结论):
  1. 五大联赛用 fetch_current_season_matches (整赛季1次请求, 字段与DB完全一致)
  2. 世界杯用自定义去重逻辑: 先按API_id查, 再按(队名+日期)查, 命中则update比分
     —— 绝不用 sync_to_database (会制造重复行, DB已有合成ID 2130xxx污染)
  3. 每联赛独立 try/except, 单个失败不影响其他
  4. 导入前强制备份 (除非 --no-backup)
  5. 串行调用 API (采集器内置10/min限流+429退避, 不并行)
"""
from __future__ import annotations

import os
import sys
import shutil
import sqlite3
import logging
import argparse
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ── 路径设置 (复刻 serve.py 的核心修复: backend优先解决core冲突) ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for p in (BACKEND_DIR, PROJECT_ROOT, os.path.join(PROJECT_ROOT, "predictors", "components")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(PROJECT_ROOT)
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("refresh_data")

# ── 常量 ──
DB_PATH = os.path.join(PROJECT_ROOT, "data", "football_data.db")
WC_LEAGUE_ID = 2000
WC_LEAGUE_NAME = "世界杯"

# 五大联赛 (code, league_id, 中文名)
TOP5 = [("PL", 2021, "英超"), ("PD", 2014, "西甲"), ("SA", 2019, "意甲"),
        ("BL1", 2002, "德甲"), ("FL1", 2015, "法甲")]
# 其他联赛
OTHER = [("CL", 2001, "欧冠"), ("DED", 2003, "荷甲"),
         ("PPL", 2017, "葡超"), ("ELC", 2016, "英冠")]


def backup_database() -> bool:
    """备份数据库 (带时间戳)"""
    if not os.path.exists(DB_PATH):
        logger.error(f"数据库不存在: {DB_PATH}")
        return False
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = DB_PATH + f".bak_{ts}"
    try:
        # 先做 WAL checkpoint 确保一致
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        shutil.copy2(DB_PATH, bak)
        size_mb = os.path.getsize(bak) / 1024 / 1024
        logger.info(f"✅ 数据库已备份: {os.path.basename(bak)} ({size_mb:.1f} MB)")
        return True
    except Exception as e:
        logger.error(f"备份失败: {e}")
        return False


def get_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def import_league_season(collector, db_get_db, code: str, league_id: int,
                          cn_name: str, season: int) -> Tuple[int, int, int]:
    """导入一个联赛的指定赛季。返回 (拉取数, 新增数, 更新数)"""
    matches = collector.fetch_current_season_matches(league_id, code, season)
    total = len(matches)
    new_cnt = upd_cnt = 0
    conn = get_db_conn()
    try:
        for m in matches:
            ext_id = m.get("match_id")
            # 按 API id 查重
            existing = conn.execute(
                "SELECT match_id, home_score, status FROM matches WHERE match_id=?",
                (ext_id,)).fetchone()
            if existing:
                # 已存在: 若DB无比分但新数据已finished, 更新比分
                if (existing["home_score"] is None and m.get("status") == "finished"
                        and m.get("home_score") is not None):
                    conn.execute(
                        "UPDATE matches SET home_score=?, away_score=?, final_result=?, "
                        "status='finished', updated_at=datetime('now','localtime') WHERE match_id=?",
                        (m["home_score"], m["away_score"],
                         _result_code(m["home_score"], m["away_score"]), ext_id))
                    upd_cnt += 1
                continue
            # 新增
            conn.execute("""
                INSERT OR REPLACE INTO matches (match_id, match_date, match_time, league_id, league_name,
                    home_team_id, home_team_name, away_team_id, away_team_name,
                    status, matchday, home_score, away_score, halftime_home, halftime_away, minute)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ext_id, m.get("match_date"), m.get("match_time"), league_id, cn_name,
                  m.get("home_team_id"), m.get("home_team_name"),
                  m.get("away_team_id"), m.get("away_team_name"),
                  m.get("status", "scheduled"), m.get("matchday"),
                  m.get("home_score"), m.get("away_score"),
                  m.get("halftime_home"), m.get("halftime_away"), m.get("minute")))
            new_cnt += 1
        conn.commit()
    finally:
        conn.close()
    return total, new_cnt, upd_cnt


def _result_code(h: int, a: int) -> str:
    return "H" if h > a else ("A" if h < a else "D")


def import_worldcup() -> Tuple[int, int, int]:
    """导入世界杯2026已完赛比分 (自定义去重, 不用 sync_to_database)。
    返回 (拉取数, 新增数, 更新数)"""
    from data_collector.football_data_live import FootballDataLive
    fdl = FootballDataLive()
    finished = fdl.get_wc2026_finished()  # 原始API结构, 队名已翻译中文
    total = len(finished)
    new_cnt = upd_cnt = dup_cnt = 0
    conn = get_db_conn()
    try:
        for m in finished:
            try:
                api_id = int(m["id"])
                home = m["homeTeam"]["name"]
                away = m["awayTeam"]["name"]
                date = (m.get("utcDate") or "")[:10]
                hs = m["score"]["fullTime"]["home"]
                as_ = m["score"]["fullTime"]["away"]
                ht_h = m["score"]["halfTime"].get("home") if m["score"].get("halfTime") else None
                ht_a = m["score"]["halfTime"].get("away") if m["score"].get("halfTime") else None
            except (KeyError, TypeError, ValueError):
                continue
            if hs is None or as_ is None:
                continue  # 未完赛
            # 去重1: 按API id
            existing = conn.execute(
                "SELECT match_id, home_score FROM matches WHERE match_id=?", (api_id,)).fetchone()
            if existing:
                if existing["home_score"] is None:
                    conn.execute(
                        "UPDATE matches SET home_score=?, away_score=?, final_result=?, "
                        "status='finished', halftime_home=?, halftime_away=? WHERE match_id=?",
                        (hs, as_, _result_code(hs, as_), ht_h, ht_a, api_id))
                    upd_cnt += 1
                else:
                    dup_cnt += 1
                continue
            # 去重2: 按 (队名+日期) 兜底 (DB可能有合成ID的同一场)
            existing2 = conn.execute(
                "SELECT match_id, home_score FROM matches WHERE home_team_name=? "
                "AND away_team_name=? AND match_date=?", (home, away, date)).fetchone()
            if existing2:
                if existing2["home_score"] is None:
                    conn.execute(
                        "UPDATE matches SET home_score=?, away_score=?, final_result=?, "
                        "status='finished', halftime_home=?, halftime_away=? WHERE match_id=?",
                        (hs, as_, _result_code(hs, as_), ht_h, ht_a, existing2["match_id"]))
                    upd_cnt += 1
                else:
                    dup_cnt += 1
                continue
            # 新增
            conn.execute("""
                INSERT INTO matches (match_id, match_date, match_time, league_id, league_name,
                    home_team_id, home_team_name, away_team_id, away_team_name,
                    status, matchday, home_score, away_score, halftime_home, halftime_away)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (api_id, date, m.get("utcDate"), WC_LEAGUE_ID, WC_LEAGUE_NAME,
                  m["homeTeam"].get("id"), home, m["awayTeam"].get("id"), away,
                  "finished", m.get("matchday"), hs, as_, ht_h, ht_a))
            new_cnt += 1
        conn.commit()
    finally:
        conn.close()
    logger.info(f"  世界杯去重: {dup_cnt} 场已存在(跳过)")
    return total, new_cnt, upd_cnt


def main():
    parser = argparse.ArgumentParser(description="哨响AI 数据刷新")
    parser.add_argument("--leagues", action="store_true", help="仅刷新联赛新赛季")
    parser.add_argument("--worldcup", action="store_true", help="仅刷新世界杯比分")
    parser.add_argument("--season", type=int, default=2025, help="赛季 (默认2025=2025/26)")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份(不推荐)")
    args = parser.parse_args()

    do_leagues = args.leagues or (not args.leagues and not args.worldcup)
    do_worldcup = args.worldcup or (not args.leagues and not args.worldcup)

    print("=" * 56)
    print("  哨响AI 数据刷新")
    print(f"  赛季: {args.season} | 联赛={do_leagues} 世界杯={do_worldcup}")
    print("=" * 56)

    # 备份
    if not args.no_backup:
        if not backup_database():
            logger.error("备份失败, 终止 (用 --no-backup 强制跳过)")
            sys.exit(1)
    else:
        logger.warning("⚠ 已跳过备份 (风险自负)")

    # 初始化采集器
    from config.api_config import API_CONFIG
    from data_collector.main import FootballDataCollector
    api_key = API_CONFIG["primary"]["api_key"]
    if not api_key:
        logger.error("FOOTBALL_DATA_API_KEY 未配置, 无法采集")
        sys.exit(1)
    collector = FootballDataCollector(api_key)
    logger.info(f"采集器就绪 (API key: {api_key[:8]}...)")

    summary = []

    # 联赛
    if do_leagues:
        print("\n── 联赛新赛季导入 ──")
        targets = TOP5 + OTHER
        for code, lid, cn in targets:
            try:
                total, new, upd = import_league_season(
                    collector, None, code, lid, cn, args.season)
                logger.info(f"  {cn}({code}) S{args.season}: 拉{total}场, 新增{new}, 更新{upd}")
                summary.append((cn, total, new, upd))
            except Exception as e:
                logger.error(f"  {cn}({code}) 失败: {e}")

    # 世界杯
    if do_worldcup:
        print("\n── 世界杯2026比分刷新 ──")
        try:
            total, new, upd = import_worldcup()
            logger.info(f"  世界杯: 拉{total}场, 新增{new}, 更新{upd}")
            summary.append(("世界杯", total, new, upd))
        except Exception as e:
            logger.error(f"  世界杯失败: {e}")

    # 汇总
    print("\n" + "=" * 56)
    print("  导入汇总")
    print("=" * 56)
    print(f"  {'联赛':<8} {'拉取':>6} {'新增':>6} {'更新':>6}")
    tot_new = tot_upd = 0
    for name, t, n, u in summary:
        print(f"  {name:<8} {t:>6} {n:>6} {u:>6}")
        tot_new += n
        tot_upd += u
    print(f"  {'合计':<8} {'':>6} {tot_new:>6} {tot_upd:>6}")
    print("=" * 56)

    # 数据库现状
    conn = get_db_conn()
    total_matches = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    conn.close()
    print(f"\n  数据库总比赛数: {total_matches:,}")


if __name__ == "__main__":
    main()
