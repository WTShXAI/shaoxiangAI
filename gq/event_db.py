#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gq/event_db.py — 完整赛事库 events.db (单一融合库, 供 AI 软件读取)

把前面所有分散的数据源融合成【一个】完整的赛事库, AI 软件只需读这一个库。
表结构与 GQ.db 完全对齐(同名同构): matches / odds_snapshots / odds_changes /
match_outcomes / match_analysis_cache / match_meta / prematch_conclusion /
pre_match_cs / cs_verification, 另加两张新表:
  - interface_doc : 接口地址/用法说明 (AI 可读, 见 _SEED_DOC) — 用了哪些接口、怎么调、返回什么
  - h2h           : 赛果页挖掘的两队历史交锋(含 overunder 历史胜负平, 总进球维度)

数据来源(与 GQ 采集器同源, 详见 interface_doc 表):
  - WS 实时盘口流 (C105)         → odds_snapshots
  - WS 实时比分流 (C103)         → matches(score) + match_outcomes(赛果)
  - WS 赛事事件流 (C102/C1021)   → matches(status/minute)
  - HTTP 比赛列表/结构           → matches
  - HTTP 赛事内容端点            → match_meta(前瞻/伤病) + h2h(赛果页)
  - GQ.db 历史                  → backfill_all_from_gq() 一次性融合

与 GQ.db 的关系:
  - events.db 是【新的唯一完整赛事库】, 表结构与 GQ.db 完全对齐(同名同构)。
    所有 reader(bridge_service / analysis/* ) 只需把路径 GQ.db → events.db 即可零 SQL 改动切换。
  - GQ.db 保留为历史归档(原文件不动, 可回滚)。
"""

import os
import sqlite3
import time

import gq.db as gqdb

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_DB = os.path.join(os.path.dirname(HERE), "data", "events.db")
GQ_DB = os.path.join(os.path.dirname(HERE), "data", "GQ.db")

# 额外表(不在 GQ.db 原有体系内)
EXTRA_TABLES = """
CREATE TABLE IF NOT EXISTS interface_doc (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    category TEXT,
    address TEXT,
    method TEXT,
    auth TEXT,
    params TEXT,
    returns TEXT,
    notes TEXT,
    example TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS h2h (
    match_key TEXT PRIMARY KEY,
    mid TEXT,
    h2h_json TEXT,
    updated_at REAL
);
"""

# GQ.db 全部业务表(用于 ATTACH 合并)
GQ_TABLES = [
    "matches", "odds_snapshots", "odds_changes", "match_outcomes",
    "match_analysis_cache", "match_meta",
    "prematch_conclusion", "pre_match_cs", "cs_verification",
]


def conn():
    c = sqlite3.connect(EVENTS_DB, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def _ensure_gq_schema():
    """在 events.db 上建全部 GQ.db 同名表(委托 gq.db.init_db, 不改全局 DB_PATH)。"""
    old = gqdb.DB_PATH
    gqdb.DB_PATH = EVENTS_DB
    try:
        gqdb.init_db()
    finally:
        gqdb.DB_PATH = old


def init_event_db():
    """建表(幂等): GQ 全同名表 + interface_doc + h2h + 接口说明 seed。"""
    _ensure_gq_schema()
    c = conn()
    c.executescript(EXTRA_TABLES)
    _seed_interface_doc(c)
    c.close()


_SEED_DOC = [
    ("WS实时盘口流(C105)", "ws",
     "动态域名 api-umc.*.com / api.*.com (接管乐鱼 H5 会话让浏览器自动建连, 不能硬编码)",
     "WebSocket", "H5 会话登录态 (gq/.env 的 GQ_H5_URL)",
     "帧 cmd=C105 的 cd 字段 = gzip+base64 (以 H4sI 开头)",
     "解码 → {mid, hls2{市场号:[行{ hv=盘口线, ol=[{ot, ov(字符串整数, ÷100000=小数赔), ov2(水位), os(1可用/2封盘/3锁)}] }]}",
     "全市场盘口(1X2/AH/OU/CS/角球/BTTS/OE/DNB/GOALS/WS_*) 落 odds_snapshots 表. OU=大小球/总进球 Over/Under; GOALS=精确总进球. 仅 os==1 写入.",
     "C105 cd 解码后 hls2['2'][0].ol=[{ot:'Over', ov:'209000', ov2:'0.92', os:1}] → OU_2.50 over=2.09"),
    ("WS实时比分流(C103)", "ws",
     "同一 WS 会话 (cmd=C103)", "WebSocket", "H5 会话",
     "帧 cd 为 dict, 含 mid + msc(比分列表)",
     "msc 形如 ['...','S0|2:1','S1|1:0','...'] → S0=全场, S1=半场",
     "全场比分→matches(score_home/away)+match_outcomes(赛果); 半场→matches(ht_*)+match_outcomes(ht_*)",
     "C103 msc=[...,'S0|2:1',...] → home=2,away=1,total=3"),
    ("WS赛事事件流(C102/C1021)", "ws",
     "同一 WS 会话 (cmd=C102 / C1021)", "WebSocket", "H5 会话",
     "帧 cd 含 mid + mmp(比赛分钟, 999=终场)",
     "mmp → matches(status: scheduled/live/finished, minute)",
     "用 mmp>=90 或 999 判定终场, 同步 match_outcomes 赛果",
     "C102 cd={mid:5514516, mmp:'75'} → minute=75, status=live"),
    ("HTTP比赛列表", "http",
     "https://api.wnbtmel.com (CUID=526002076777845380)",
     "GET/POST", "requestid + checkid (auto_collector._build_headers)",
     "无 / 或 standardMatchId",
     "比赛列表(含 mid/grp); 用于 mid→match_key(主 vs 客) 映射",
     "WS 帧只带 mid 不带队名, 需配合本接口做映射",
     "fetch_match_list() → [{mid, grp, ...}]"),
    ("HTTP比赛结构", "http",
     "https://api.wnbtmel.com",
     "POST", "requestid + checkid",
     "mids=[...]",
     "结构(含 mhn/man/tnjc/mgt) → Registry._ingest 建 match_key + 落 matches",
     "mid→队名映射; 乐鱼 WS 帧无队名",
     "fetch_match_structure([mid]) → [{mid, mhn, man, tnjc, mgt}]"),
    ("赛事内容端点(前瞻/伤病/赛果/情报)", "http",
     "POST https://api.wnbtmel.com/yewu11/v1/w/matchAnalysise/getMatchAnalysiseDataPB",
     "POST", "checkid + requestid (auto_collector._build_headers, 不用 cookie)",
     "parentMenuId + sonMenuId(必须为【数字 tab 索引】, 非字符串 id!) + standardMatchId(int)",
     "code=0000000; data=gzip+base64 → {basicInfoMap:{...}}",
     "★ sonMenuId 必须数字: 前瞻+伤病=parent2/son1; 赛果(H2H历史交锋)=parent2/son2; 情报=parent4/son2. 传字符串 id 恒返 0400500.",
     "parentMenuId=2,sonMenuId=2,standardMatchId=5530448 → matchHistoryBattleDTOMap(两队历史交锋)"),
    ("赛果页(H2H历史交锋)", "http",
     "同上 getMatchAnalysiseDataPB, parentMenuId=2, sonMenuId=2",
     "POST", "checkid + requestid",
     "standardMatchId(int)",
     "matchHistoryBattleDTOMap: {'1':主场视角,'2':客场视角} 各含 handicapResultList + matchHistoryBattleDetailDTOList(含 overunderResult*)",
     "注意: 赛果页返回的是【两队历史交锋】, 非单场终比分! 单场终比分来自 WS C103 / matches 表. 落 h2h 表.",
     "可用于 h2h 入库 + 校验单场终比分缺口"),
    ("单场终比分来源", "ws",
     "WS C103 比分帧 (msc 含 'S0|h:a') / matches 表 score_home/away",
     "WebSocket", "H5 会话",
     "mid",
     "实时/完场比分 → match_outcomes(score_home/away/result) + matches(score_home/away)",
     "与赛果页(H2H)不同, 这是本场实际比分",
     "C103 msc=[...,'S0|2:1',...] → home=2,away=1,total=3"),
    ("数据库: GQ.db (历史归档)", "db",
     "D:/Architecture/data/GQ.db",
     "SQLite", "busy_timeout=30000",
     "matches / odds_snapshots / match_outcomes / match_analysis_cache / match_meta / CS校验表",
     "原全市场盘口库 + 分析表; 历史数据已通过 backfill_all_from_gq() 融合进 events.db",
     "events.db 是新唯一完整赛事库(表结构与 GQ.db 完全对齐); GQ.db 仅留作历史归档, 原文件不动可回滚",
     "SELECT * FROM odds_snapshots WHERE market LIKE 'OU%'"),
    ("数据库: events.db (本库/完整赛事库)", "db",
     "D:/Architecture/data/events.db",
     "SQLite", "busy_timeout=30000, WAL",
     "interface_doc / matches / odds_snapshots / odds_changes / match_outcomes / match_analysis_cache / match_meta / h2h / prematch_conclusion / pre_match_cs / cs_verification",
     "融合全部维度的唯一赛事库, AI 软件直接读取分析。表结构与 GQ.db 完全对齐, reader 改路径即零改动切换",
     "全市场盘口(odds_snapshots) + 主表(matches) + 初盘/赛果(match_outcomes) + 复盘缓存(match_analysis_cache) + 内容(match_meta) + H2H(h2h) + 接口说明(interface_doc)",
     "SELECT * FROM odds_snapshots WHERE market LIKE 'OU%'; SELECT * FROM match_outcomes WHERE result='home'"),
]


def _seed_interface_doc(c):
    """写入接口说明(幂等, 已存在则跳过)。"""
    now = time.time()
    for r in _SEED_DOC:
        c.execute("""INSERT OR IGNORE INTO interface_doc
            (name, category, address, method, auth, params, returns, notes, example, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", r + (now,))
    c.commit()


# ───────────────────────── 写入: H2H (赛果页挖掘) ─────────────────────────
def record_h2h(match_key, mid, h2h_json):
    """写赛果页挖掘的两队历史交锋(幂等 upsert)。"""
    try:
        c = conn()
        c.execute("""INSERT OR REPLACE INTO h2h (match_key, mid, h2h_json, updated_at)
            VALUES (?,?,?,?)""", (match_key, str(mid), h2h_json, time.time()))
        c.commit()
        c.close()
    except Exception:
        pass


# ───────────────────────── 回填: 从 GQ.db 融合历史 ─────────────────────────
def backfill_all_from_gq(tables=None, recreate=False):
    """ATTACH GQ.db, 把 GQ.db 的表(结构+数据)合并进 events.db 同名表。

    ⚠️ 关键修复(2026-08-27): GQ.db 实际表结构经多次 ALTER 已漂移 —— 例如
    matches 多了 tag、match_outcomes 多了 is_virtual、match_analysis_cache 多了 20 个分析列,
    而 events.db 这些表是按 gq.db.init_db() 的【规范子集】结构建的。
    原 `INSERT OR IGNORE ... SELECT *` 按【列位置】插入 → 列数不符 →
    整表 INSERT 抛异常被吞 → 行数静默归零(matches/match_outcomes/match_analysis_cache 曾因此全空)。
    本函数改用 GQ 的【实际 DDL】重建 events.db 对应表(列数/顺序与 GQ 完全一致),
    再 SELECT * 才能保证位置对齐、完整拷贝。

    - tables:    仅合并指定表(默认 None = 合并 GQ 全部表)
    - recreate:  True 时先 DROP events.db 同名表, 再用 GQ 实际 DDL 重建(用于修复漂移表)
    - 返回各表合并行数 dict(失败表值为 'ERR:...')
    要求先调 init_event_db() 建好 interface_doc/h2h 等(不在 GQ 内的表不受影响)。
    """
    counts = {}
    e = conn()
    try:
        e.execute("ATTACH DATABASE ? AS gq", (GQ_DB,))
        if tables:
            gq_tables = list(tables)
        else:
            gq_tables = [r[0] for r in e.execute(
                "SELECT name FROM gq.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        for tbl in gq_tables:
            try:
                exists = e.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone()
                # 重建条件: 显式 recreate, 或 events.db 尚缺该表(首次用 GQ 实际 DDL 建)
                if recreate or not exists:
                    if exists:
                        e.execute(f"DROP TABLE IF EXISTS '{tbl}'")
                    ddl = e.execute(
                        "SELECT sql FROM gq.sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone()
                    if ddl and ddl[0]:
                        e.execute(ddl[0])   # GQ 实际 CREATE 语句 → 列数/顺序与 GQ 一致
                    else:
                        e.execute(f"CREATE TABLE IF NOT EXISTS '{tbl}' AS SELECT * FROM gq.'{tbl}' WHERE 0")
                before = e.execute(f"SELECT COUNT(*) FROM '{tbl}'").fetchone()[0]
                e.execute(f"INSERT OR IGNORE INTO '{tbl}' SELECT * FROM gq.'{tbl}'")
                after = e.execute(f"SELECT COUNT(*) FROM '{tbl}'").fetchone()[0]
                counts[tbl] = after - before
            except Exception as ex:
                counts[tbl] = f"ERR:{ex}"
        e.commit()
    finally:
        try:
            e.execute("DETACH DATABASE gq")
        except Exception:
            pass
        e.close()
    return counts


# ───────────────────────── 回填: 赛果页 H2H ─────────────────────────
def backfill_h2h_from_result_page(limit=None, only_missing=True, pause=0.4, retries=2):
    """逐场 HTTP 挖赛果页 H2H, 落地 events.db.h2h。需乐鱼 token 有效。
    仅对已完场(finished)且(默认)缺失 h2h 的比赛回填。返回写入行数。
    pause: 每场请求后的间隔秒数(乐鱼内容接口对连续快速请求会 read timeout, 2026-08-27 实测)
    retries: 单场失败后的重试次数(含超时/空返回)"""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from gq.content_collector import _fetch, RESULT_MENU
    e = conn()
    if only_missing:
        sql = ("SELECT m.mid, m.match_key FROM matches m "
               "WHERE m.mid IS NOT NULL AND m.status='finished' "
               "AND m.mid NOT IN (SELECT mid FROM h2h)")
    else:
        sql = "SELECT m.mid, m.match_key FROM matches m WHERE m.mid IS NOT NULL AND m.status='finished'"
    if limit:
        sql += " LIMIT %d" % int(limit)
    rows = e.execute(sql).fetchall()
    e.close()
    n = 0
    for (mid, match_key) in rows:
        for attempt in range(retries + 1):
            try:
                d = _fetch(mid, *RESULT_MENU)
                if d:
                    h2h = json.dumps(d, ensure_ascii=False)
                    record_h2h(match_key, mid, h2h)
                    n += 1
                    break
            except Exception:
                pass
            time.sleep(pause)   # 空返回/异常后也等一拍, 防连发超时
        time.sleep(pause)
    return n


# ───────────────────────── 统计 ─────────────────────────
def stats():
    c = conn()
    out = {}
    for tbl in GQ_TABLES + ["interface_doc", "h2h"]:
        try:
            out[tbl] = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except Exception:
            out[tbl] = "MISSING"
    c.close()
    return out


if __name__ == "__main__":
    init_event_db()
    print("stats:", stats())
