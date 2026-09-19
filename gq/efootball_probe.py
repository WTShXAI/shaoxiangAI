#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""电子盘口探针 (2026-09-19, 用户指令: 先监测电子盘口 → 深度学习赔率变化 → 重构哨响)
================================================================================
背景: 主采集器 auto_collector.fetch_match_list 有 _is_simulated_league 过滤,
电子足球/VR/梦幻对垒等模拟联赛被故意排除在采集队列外。本探针反其道:
专门采集模拟域 (引擎=软件, "游戏局"), 为引擎指纹诊断与深度学习积累语料。

隔离铁律: 写独立库 data/efootball.db, 零接触 events.db 生产表。
口径: 赔率原始 payload 全量 JSON 落盘 (不预解析), 供后续任意粒度重建。

分区 euid (从 H5 网络层实测, 2026-09-19):
  3020190 = 电子足球   3020101 = 真实足球(主采集器在用, 勿动)

用法:
  python gq/efootball_probe.py --once          # 单轮
  python gq/efootball_probe.py --loop          # 常驻 (60s/轮, 日志 gq/efootball_daemon.log)
"""
import argparse
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import auto_collector as ac  # 复用: token热加载/_api_post/_decode/风控/CUID

DB = os.path.join(HERE, os.pardir, "data", "efootball.db")
LOG = os.path.join(HERE, "efootball_daemon.log")
EUID_EFOOTBALL = "3020190"

DDL = """
CREATE TABLE IF NOT EXISTS ef_matches (
    mid TEXT PRIMARY KEY, league TEXT, home TEXT, away TEXT,
    mgt INTEGER, grp TEXT, score TEXT, final_score TEXT, first_seen REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS ef_odds_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mid TEXT, euid TEXT, score TEXT, minute TEXT, payload TEXT, captured_at REAL);
CREATE INDEX IF NOT EXISTS idx_efo_mid ON ef_odds_raw(mid, captured_at);
"""


def _s0_score(msc) -> str:
    """msc 列表 → 'h-a' 全场比分 (S0|a:b 码, 与主 feed ws_collector 同一套)。"""
    if not isinstance(msc, list):
        return ''
    for it in msc:
        s = str(it)
        if s.startswith('S0|'):
            return s[3:].replace(':', '-')
    return ''


def _log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _con():
    con = sqlite3.connect(DB, timeout=30)
    con.executescript(DDL)
    for col in ("score TEXT", "final_score TEXT"):  # 旧库迁移
        try:
            con.execute(f"ALTER TABLE ef_matches ADD COLUMN {col}")
        except Exception:
            pass
    for col in ("score TEXT", "minute TEXT"):  # 旧库迁移
        try:
            con.execute(f"ALTER TABLE ef_odds_raw ADD COLUMN {col}")
        except Exception:
            pass
    return con


def fetch_sim_list(euid: str):
    """同 ac.fetch_match_list 但 euid 指定模拟分区, 不过滤模拟联赛。"""
    body = {"cuid": ac.CUID, "sort": 1, "tid": "", "apiType": 1, "orpt": 0, "euid": euid}
    js = ac._api_post(ac.LIST_PATH, body)
    if not js or js.get("code") != "0000000":
        return []
    data = ac._decode(js.get("data", ""))
    if not data:
        return []
    items = []
    for grp in ("livedata", "nolivedata"):
        for it in data.get(grp, []):
            for mid in str(it.get("mids", "")).split(","):
                mid = mid.strip()
                if mid:
                    items.append({"mid": mid, "tn": it.get("tn", ""),
                                  "mgt": it.get("mgt", 0), "tid": it.get("tid", ""), "grp": grp})
    return items


def fetch_sim_odds(mid: str, euid: str):
    """同 ac.fetch_match_odds 但 euid 指定模拟分区; 返回解码 dict 或 None。"""
    body = {"cuid": ac.CUID, "cos": 0, "orpt": 0, "euid": euid,
            "mid": str(mid), "mcid": 0, "newUser": 0}
    js = ac._api_post(ac.ODDS_PATH, body)
    if not js or js.get("code") != "0000000":
        return None
    return ac._decode(js.get("data", ""))


def fetch_sim_names(mids: list):
    """队名/联赛名 (STRUCT_PATH 对未开赛也返回完整信息)。"""
    if not mids:
        return {}
    out = {}
    for i in range(0, len(mids), 20):
        chunk = mids[i:i + 20]
        body = {"mids": ",".join(chunk), "cuid": ac.CUID, "cos": 0, "orpt": 0, "euid": EUID_EFOOTBALL}
        js = ac._api_post(ac.STRUCT_PATH, body)
        if not js or js.get("code") != "0000000":
            continue
        data = ac._decode(js.get("data", ""))
        if not data:
            continue
        rows = data if isinstance(data, list) else data.get("data") or []
        for m in rows if isinstance(rows, list) else []:
            mid = str(m.get("mid") or "")
            if mid:
                out[mid] = {"home": m.get("mhn") or "", "away": m.get("man") or "",
                            "league": m.get("tnjc") or m.get("tn") or ""}
    return out


def cycle(euid: str):
    t0 = time.time()
    con = _con()
    items = fetch_sim_list(euid)
    now = time.time()
    fresh = [it for it in items if it["grp"] == "livedata"]
    names = fetch_sim_names([it["mid"] for it in items][:60])
    n_new = n_odds = 0
    for it in items:
        nm = names.get(it["mid"], {})
        cur = con.execute("SELECT last_seen FROM ef_matches WHERE mid=?", (it["mid"],)).fetchone()
        if not cur:
            n_new += 1
            con.execute("INSERT INTO ef_matches (mid, league, home, away, mgt, grp, first_seen, last_seen) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (it["mid"], nm.get("league") or it["tn"], nm.get("home", ""), nm.get("away", ""),
                         it["mgt"], it["grp"], now, now))
        else:
            con.execute("""UPDATE ef_matches SET grp=?, last_seen=?,
                league=CASE WHEN ?<>'' THEN ? ELSE league END,
                home=CASE WHEN home='' OR home IS NULL THEN ? ELSE home END,
                away=CASE WHEN away='' OR away IS NULL THEN ? ELSE away END
                WHERE mid=?""",
                        (it["grp"], now, nm.get("league") or it["tn"], nm.get("league") or it["tn"],
                         nm.get("home", ""), nm.get("away", ""), it["mid"]))
        if it["grp"] == "livedata":
            od = fetch_sim_odds(it["mid"], euid)
            if od is not None:
                md = (od.get("data") or [{}])
                md0 = md[0] if isinstance(md, list) and md else {}
                sc = _s0_score(md0.get("msc"))
                con.execute("INSERT INTO ef_odds_raw (mid, euid, score, minute, payload, captured_at) "
                            "VALUES(?,?,?,?,?,?)",
                            (it["mid"], euid, sc, str(md0.get("mst", "")),
                             json.dumps(od, ensure_ascii=False)[:200000], now))
                n_odds += 1
                if sc:
                    con.execute("UPDATE ef_matches SET score=? WHERE mid=?", (sc, it["mid"]))
        elif it["grp"] != "livedata":
            # 离开 live → 用最后已知比分定格终局 (e-football 8分钟一节, 生命周期短)
            row = con.execute("SELECT score, final_score FROM ef_matches WHERE mid=?", (it["mid"],)).fetchone()
            if row and row[0] and not row[1]:
                con.execute("UPDATE ef_matches SET final_score=? WHERE mid=?", (row[0], it["mid"]))
                _log(f"[完赛] {it['tn'][:24]} mid={it['mid']} 终场 {row[0]}")

    # 完赛消失守卫 (2026-09-19 实测: VS- 模拟局完赛后直接从列表消失, 不转 nolivedata):
    # live 状态 + 8 分钟未再出现 + 有最后比分 → 定格终局
    for mid, sc, tn in con.execute(
            "SELECT mid, score, league FROM ef_matches "
            "WHERE final_score IS NULL AND grp='livedata' AND score IS NOT NULL AND score<>'' "
            "AND last_seen < ?", (now - 480,)).fetchall():
        con.execute("UPDATE ef_matches SET final_score=? WHERE mid=?", (sc, mid))
        _log(f"[完赛·消失] {str(tn)[:24]} mid={mid} 终场 {sc}")
    con.commit()
    con.close()
    _log(f"cycle: 列表{len(items)} (live {len(fresh)}) | 新增{n_new} | 赔率快照{n_odds} | {time.time()-t0:.1f}s")
    return len(items), n_odds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--loop', action='store_true')
    ap.add_argument('--interval', type=int, default=60)
    ap.add_argument('--euid', default=EUID_EFOOTBALL)
    args = ap.parse_args()
    if args.once:
        cycle(args.euid)
        return
    if args.loop:
        _log(f"=== efootball probe daemon start (euid={args.euid}, {args.interval}s/轮) ===")
        while True:
            try:
                cycle(args.euid)
            except Exception as e:
                _log(f"[WARN] cycle 异常: {e}")
            time.sleep(args.interval)


if __name__ == '__main__':
    main()
