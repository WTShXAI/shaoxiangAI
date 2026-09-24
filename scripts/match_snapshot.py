r"""match_snapshot.py — 防未来信息数据快照 (反偷看加固, SYSTEM_BLUEPRINT §2.4/P-SNAPSHOT)

隔离库 data/snapshots.db (与 events.db 解耦)。
freeze_snapshot(match_key) 在 T=kickoff 冻结赛前可见字段, 生成不可变锚(hash)。
回测/生产共用同一读取路径(get), 杜绝 train-serve skew。

当前可用字段: match_key/home/away/league/kickoff + prematch_conclusion.verdict
             + match_meta.preview / news / injuries_home (P-SNAPSHOT-data Phase1, 2026-09-25 接入)
缺失字段(待采集层补齐, 见 WINDOW 规格 docs/WINDOW_PREP.md):
  - lineup (GQ 阵容端点"暂未接入", 全 0 行)
  - injury 客队侧(injuries_away 0 行) / weather / ref / schedule_density (全库无源)
  → 这些列存在但默认 NULL, 采集就绪后即可写入, 不改表结构。

纪律(DISCIPLINE.md §4): 冻结后不可变(同 match_key 不覆盖); 只读 events.db 取源。
用法:
  from scripts.match_snapshot import init, freeze, get
  init()
  freeze('match_key_xxx')   # 在 kickoff 时调用一次
  snap = get('match_key_xxx')
"""
import os
import sqlite3
import json
import hashlib
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "snapshots.db")
EVENTS = os.path.join(ROOT, "data", "events.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS match_snapshot (
  snapshot_id    TEXT PRIMARY KEY,
  match_key      TEXT,
  frozen_at      REAL,
  home           TEXT,
  away           TEXT,
  league         TEXT,
  kickoff        TEXT,
  verdict        TEXT,
  lineup         TEXT,
  injury         TEXT,
  weather        TEXT,
  ref            TEXT,
  schedule_density TEXT,
  preview        TEXT,
  news           TEXT,
  injury_home    TEXT,
  fields_json    TEXT,
  hash           TEXT
);
"""


TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS tr_snap_no_update BEFORE UPDATE ON match_snapshot
  BEGIN SELECT RAISE(ABORT, 'match_snapshot 不可变: 禁止 UPDATE'); END;
CREATE TRIGGER IF NOT EXISTS tr_snap_no_delete BEFORE DELETE ON match_snapshot
  BEGIN SELECT RAISE(ABORT, 'match_snapshot 不可变: 禁止 DELETE'); END;
"""


def _con():
    return sqlite3.connect(DB)


def init():
    con = _con()
    con.execute(SCHEMA)
    con.executescript(TRIGGERS)
    # 幂等增量迁移: 老库补列(不被 CREATE TABLE IF NOT EXISTS 覆盖)
    _cols = {r[1] for r in con.execute("PRAGMA table_info(match_snapshot)").fetchall()}
    for _c, _t in (("preview", "TEXT"), ("news", "TEXT"), ("injury_home", "TEXT")):
        if _c not in _cols:
            con.execute(f"ALTER TABLE match_snapshot ADD COLUMN {_c} {_t}")
    con.commit()
    con.close()


def _events_ro():
    return sqlite3.connect(f"file:{EVENTS}?mode=ro", uri=True)


def freeze(match_key):
    """冻结一场的赛前可见字段。已冻结则跳过(不可变)。返回 snapshot_id 或 None(已存在)。"""
    con = _con()
    existing = con.execute("SELECT snapshot_id FROM match_snapshot WHERE match_key=?", (match_key,)).fetchone()
    if existing:
        con.close()
        return None  # 不可变: 不覆盖已有冻结
    ec = _events_ro()
    row = ec.execute(
        "SELECT match_key, home, away, league, kickoff FROM matches WHERE match_key=?", (match_key,)
    ).fetchone()
    verdict = None
    preview = news = injury_home = None
    try:
        v = ec.execute(
            "SELECT verdict_code FROM prematch_conclusion WHERE match_key=?", (match_key,)
        ).fetchone()
        verdict = v[0] if v else None
    except Exception:
        pass
    # P-SNAPSHOT-data Phase1: 冻结赛前已知态(preview/news/injuries_home)，防赛后篡改
    try:
        m = ec.execute(
            "SELECT preview, news, injuries_home FROM match_meta WHERE match_key=?", (match_key,)
        ).fetchone()
        if m:
            preview, news, injury_home = m[0], m[1], m[2]
    except Exception:
        pass
    ec.close()
    if not row:
        con.close()
        return None
    mk, home, away, league, kickoff = row
    fields = {
        "match_key": mk, "home": home, "away": away, "league": league, "kickoff": kickoff,
        "verdict": verdict,
        "lineup": None, "injury": None, "weather": None, "ref": None, "schedule_density": None,
        "preview": preview, "news": news, "injury_home": injury_home,
    }
    fields_json = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    h = hashlib.sha256(fields_json.encode("utf-8")).hexdigest()
    sid = f"{match_key}@{int(datetime.now(timezone.utc).timestamp())}"
    con.execute(
        """INSERT INTO match_snapshot
           (snapshot_id, match_key, frozen_at, home, away, league, kickoff, verdict,
            lineup, injury, weather, ref, schedule_density, preview, news, injury_home,
            fields_json, hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, mk, datetime.now(timezone.utc).timestamp(), home, away, league, kickoff, verdict,
         None, None, None, None, None, preview, news, injury_home, fields_json, h),
    )
    con.commit()
    con.close()
    return sid


def get(match_key):
    con = _con()
    row = con.execute("SELECT * FROM match_snapshot WHERE match_key=?", (match_key,)).fetchone()
    con.close()
    return row


if __name__ == "__main__":
    init()
    print("snapshots DB ready:", DB)
