"""
events.db 滚球实时赔率数据库
========================
追踪每场比赛每个市场的赔率变化, 记录每一次快照.

表设计:
  matches           — 比赛元信息 (id, home, away, kickoff, league, status, score, minute)
  odds_snapshots    — 赔率快照 (match_id, captured_at, market, selection, odds, score_at, minute_at)
  odds_changes      — 赔率变化 (match_id, market, selection, from_odds, to_odds, change, dt_sec)
"""
from __future__ import annotations

import os, sqlite3, time, json, re
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

# ── 事故② 根治: conn() 委托 core.db_manager 单写者, 消除每次连接 PRAGMA journal_mode=WAL 抢写锁 ──
from core.db_manager import get_manager


_CS_OTHER_RE = re.compile(r'^(其他|other|any\s*other|others)$', re.IGNORECASE)
_CS_PAIR_RE = re.compile(r'^(\d{1,3})\s*[-:.]\s*(\d{1,3})$')


def normalize_cs_score(raw):
    """把波胆比分标签规范成英文冒号格式 '0:0'。

    接受 '0-0' / '0:0' / '0.0' / '1-2' 等 → '0:0' / '1:2'。
    庄家"其他/any other"类选项 → 原样保留(保留多出的赔率, 不归一为数字比分)。
    非法(无数字对) → 返回 None(调用方跳过, 不写入)。
    2026-08-17 铁律: CS 比分统一用英文冒号格式, 禁止 '-' 或 '.' 分隔。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if _CS_OTHER_RE.match(s):
        return s
    m = _CS_PAIR_RE.match(s)
    if not m:
        return None
    return f"{int(m.group(1))}:{int(m.group(2))}"


# ── HT 半场比分污染清洗 (2026-08-27 发现, 铁证级) ──
# GQ matches/match_outcomes 的 ht_score_home/away 约 66% 被回填成全场比分
# (ht_total == ft_total; finished 64.9% / live 94.1% / filtered 100%)。
# 仅当 ht_total < ft_total 时, ht_score 才是真实半场比分, 否则视为不可用。
# 任何读取 ht_score 做标签/特征的代码必须先过此函数或 HT_CLEAN_RULE。
HT_CLEAN_RULE = "(ht_score_home + ht_score_away) < (score_home + score_away)"  # SQL 谓词, 直接拼进 WHERE


def clean_ht_score(ht_home, ht_away, ft_home, ft_away):
    """返回 (ht_home, ht_away) 若半场比分可信, 否则 None。

    可信条件: 半场总进球 < 全场总进球 (ht_total < ft_total)。
    66% 的 GQ ht_score 满足 ht_total == ft_total (被回填为全场), 必须剔除。
    任一值为 None → 返回 None (缺失即不可用)。
    """
    try:
        if ht_home is None or ht_away is None or ft_home is None or ft_away is None:
            return None
        if (ht_home + ht_away) < (ft_home + ft_away):
            return (ht_home, ht_away)
        return None
    except (TypeError, ValueError):
        return None


def ht_is_clean(ht_home, ht_away, ft_home, ft_away) -> bool:
    """布尔版: ht_score 是否可信 (ht_total < ft_total 且均非 None)。"""
    return clean_ht_score(ht_home, ht_away, ft_home, ft_away) is not None

# SSoT: 全系统统一写 data/events.db(完整赛事库, 表结构与 GQ.db 完全对齐);
# GQ.db 仅留作历史归档(原文件不动, 可回滚).
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "events.db")

# ── V7.1 复盘链路: 赛事分析缓存表 DDL (独立于 match_outcomes, 用 mid 关联) ──
# 字段分两组:
#   分析快照  — SSoT 直接来自 _live_predict 返回的 r 对象, 赛前写入
#   赛后修正  — 懒修正 / backfill 时读 match_outcomes 赛果回填
_MAC_DDL = """
CREATE TABLE IF NOT EXISTS match_analysis_cache (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mid TEXT NOT NULL,
    captured_at REAL NOT NULL,
    -- 分析快照 (SSoT: 直接来自 _live_predict 返回的 r)
    verdict TEXT,                   -- 1X2 方向: 主胜/平局/客胜 (r.direction)
    pred_score_home INTEGER,        -- OIP top1 推荐比分主队进球 (r.oip.top3_scores[0])
    pred_score_away INTEGER,       -- OIP top1 推荐比分客队进球
    edge REAL,                     -- value_layer.best_edge_pct (%)
    stake_suggestion TEXT,         -- 下注方向: H/D/A/PASS (value_layer.best_direction)
    stake_amount REAL DEFAULT 1.0,-- 注码额(单位), 默认1; BET时取 r 内 scenario.stake
    confidence REAL,               -- market_conf (0..1)
    odds_type TEXT,                -- 初盘分类(赛后修正时从 match_outcomes 回填)
    snapshot_ref TEXT,             -- 快照引用: 所用 1X2 赔率 oh/od/oa
    -- 赛后修正 (懒修正 / backfill)
    result_actual TEXT,            -- match_outcomes.result: home/draw/away
    verdict_hit TEXT,              -- hit / miss / miss_draw
    score_err REAL,                -- |pred_h - score_h| + |pred_a - score_a|
    stake_pnl REAL,                -- 硬结算: 命中(odds-1)*stake, 未中 -stake; PASS=0
    deviation_note TEXT,           -- 简短中文偏差说明
    corrected_at REAL,             -- 修正时间戳(NULL=未修正)
    UNIQUE(mid)
);
CREATE INDEX IF NOT EXISTS idx_mac_mid ON match_analysis_cache(mid);
"""


@contextmanager
def conn(readonly: bool = False):
    """获取 SQLite 连接 (委托 core.db_manager 单写者, 消除坏页根因).

    写路径委托 ``core.db_manager.get_manager().writer()`` —— 全进程唯一 writer 连接 +
    ``threading.RLock`` 串行化, 连接级仅设 ``busy_timeout``; ``journal_mode=WAL`` 只在
    ``init_db()`` 非 WAL 时设一次 (见 ``GQConnectionManager.init_db``), **绝不每次连接
    抢写锁** → 坏页根因消除。嵌套 ``with conn()`` 因 RLock 可重入, 仅最外层提交。

    Args:
        readonly: ``True`` 时以 ``mode=ro`` URI 打开, 仅用于 SELECT / health / smoke,
            绝不写真实 events.db (坏页自愈 SOP 要求只读旁路, 严禁写真实库)。
    """
    if readonly:
        # 只读旁路: mode=ro 打开, 零写锁风险; 库不存在/损坏时回退普通打开(仍不主动写)
        try:
            _uri = Path(DB_PATH).as_uri() + "?mode=ro"
            c = sqlite3.connect(_uri, timeout=30, uri=True)
        except (sqlite3.Error, ValueError):
            c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA busy_timeout=30000")
        except sqlite3.Error:
            pass
        try:
            yield c
        finally:
            try:
                c.close()
            except Exception:
                pass
        return
    # 写路径: 委托单写者 (嵌套调用因 RLock 可重入, 仅最外层提交)
    mgr = get_manager(db_path=DB_PATH)
    with mgr.writer() as c:
        yield c


def _ensure_pragmas():
    """一次性设置持久化 journal_mode=WAL + synchronous=NORMAL (幂等, 失败静默不崩).

    只在进程启动(init_db)时调用一次; journal_mode/synchronous 存于文件头持久生效,
    后续所有连接(含 bridge/离线脚本)自动继承 WAL, 无需每次连接重复设置.
    """
    try:
        c = sqlite3.connect(DB_PATH, timeout=30)
        try:
            mode = c.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != 'wal':
                c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.commit()
        finally:
            c.close()
    except Exception:
        pass  # 设置失败不致命: 库可能临时被锁, 下轮/下次启动再设


def init_db():
    """建表 — 已存在则跳过"""
    _ensure_pragmas()  # 一次性设置 WAL + NORMAL (持久化), conn() 不再每次抢锁设置
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS matches (
            match_key TEXT PRIMARY KEY,
            home TEXT NOT NULL,
            away TEXT NOT NULL,
            league TEXT DEFAULT '',
            kickoff TEXT,
            status TEXT DEFAULT 'scheduled',  -- scheduled/live/finished
            score_home INTEGER,
            score_away INTEGER,
            minute INTEGER,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key TEXT NOT NULL,
            captured_at REAL NOT NULL,
            market TEXT NOT NULL,           -- 1x2 / ah / ou / cs
            selection TEXT NOT NULL,         -- home/draw/away  或 1-0 等
            odds REAL NOT NULL,
            line REAL,                       -- 让球盘口/大小球
            score_at TEXT DEFAULT '',        -- 快照时比分
            minute_at INTEGER DEFAULT 0      -- 快照时比赛分钟
        );
        CREATE INDEX IF NOT EXISTS idx_snap_match ON odds_snapshots(match_key, market, selection, captured_at);

        CREATE TABLE IF NOT EXISTS odds_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key TEXT NOT NULL,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            from_odds REAL,
            to_odds REAL NOT NULL,
            change REAL NOT NULL,            -- to - from (正=升, 负=降)
            captured_at REAL NOT NULL,
            score_at TEXT DEFAULT '',
            minute_at INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_chg_match ON odds_changes(match_key, market, selection, captured_at);

        -- 初盘→赛果对照库 (初盘分类+赛果归档, 供策略组分析)
        CREATE TABLE IF NOT EXISTS match_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mid TEXT NOT NULL UNIQUE,               -- 比赛ID
            home TEXT NOT NULL,                     -- 主队
            away TEXT NOT NULL,                     -- 客队
            league TEXT DEFAULT '',                 -- 联赛
            kickoff TEXT,                           -- 开赛时间(ISO)
            score_home INTEGER,                     -- 主队终场进球
            score_away INTEGER,                     -- 客队终场进球
            ht_score_home INTEGER,                  -- 主队半场(HT)进球 (msc S1|)
            ht_score_away INTEGER,                  -- 客队半场(HT)进球
            result TEXT,                            -- 'home' / 'draw' / 'away'
            -- 初盘 1X2
            op_1x2_h REAL, op_1x2_d REAL, op_1x2_a REAL,
            -- 初盘 AH (首条盘口线)
            op_ah_line REAL, op_ah_home REAL, op_ah_away REAL,
            -- 初盘 OU (首条大小线)
            op_ou_line REAL, op_ou_over REAL, op_ou_under REAL,
            -- 初盘波胆 (JSON: [[\"1-0\",8.5],[\"2-0\",9.0],...] top10)
            op_cs TEXT DEFAULT '[]',
            odds_type TEXT,                         -- 初盘分类标签
            captured_at REAL NOT NULL,              -- 初盘采集时间
            archived_at REAL NOT NULL               -- 赛果归档时间
        );
        CREATE INDEX IF NOT EXISTS idx_mo_result ON match_outcomes(result, odds_type);

        -- V7.1 复盘链路: 赛事分析缓存表 (独立于 match_outcomes, 用 mid 关联)
        CREATE TABLE IF NOT EXISTS match_analysis_cache (
            analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
            mid TEXT NOT NULL,
            captured_at REAL NOT NULL,
            verdict TEXT,
            pred_score_home INTEGER,
            pred_score_away INTEGER,
            edge REAL,
            stake_suggestion TEXT,
            stake_amount REAL DEFAULT 1.0,
            confidence REAL,
            odds_type TEXT,
            snapshot_ref TEXT,
            result_actual TEXT,
            verdict_hit TEXT,
            score_err REAL,
            stake_pnl REAL,
            deviation_note TEXT,
            corrected_at REAL,
            UNIQUE(mid)
        );
        CREATE INDEX IF NOT EXISTS idx_mac_mid ON match_analysis_cache(mid);

        -- WS 全市场采集器补充表: 赛事内容(前瞻/情报/伤病/阵容), 由 ws_collector 周期刷新写入
        -- match_key 与 matches 一致("主 vs 客"); mid 冗余存一份便于跨表关联.
        CREATE TABLE IF NOT EXISTS match_meta (
            match_key      TEXT PRIMARY KEY,
            mid            TEXT,
            preview        TEXT DEFAULT '',   -- 赛事前瞻(分析文案)
            news           TEXT DEFAULT '',   -- 情报/新闻(球队动态)
            injuries_home  TEXT DEFAULT '',   -- 主队伤病
            injuries_away  TEXT DEFAULT '',   -- 客队伤病
            lineup_home    TEXT DEFAULT '',   -- 主队阵容(预计首发)
            lineup_away    TEXT DEFAULT '',   -- 客队阵容(预计首发)
            updated_at     REAL
        );
        CREATE INDEX IF NOT EXISTS idx_meta_mid ON match_meta(mid);
        """)

        # P0a: 幂等新增 is_valid 标记列 (脏行不删, 保留可审计)
        try:
            _cols = [r[1] for r in c.execute("PRAGMA table_info(match_outcomes)").fetchall()]
            if "is_valid" not in _cols:
                c.execute("ALTER TABLE match_outcomes ADD COLUMN is_valid INTEGER DEFAULT 1")
        except Exception:
            pass

        # P1a: 幂等新增 source 标记列 (gq=原始复盘库 / wc=WC验证导入, 默认 gq)
        # 与 is_valid 同构: 仅新增列, 不触碰既有 gq 数据; wc 批次显式置 'wc'。
        try:
            _cols = [r[1] for r in c.execute("PRAGMA table_info(match_outcomes)").fetchall()]
            if "source" not in _cols:
                c.execute("ALTER TABLE match_outcomes ADD COLUMN source TEXT DEFAULT 'gq'")
        except Exception:
            pass

        # 迁移: 新增 mid 列 (时间轴按 mid 关联列表接口)
        try:
            c.execute("ALTER TABLE matches ADD COLUMN mid TEXT")
        except Exception:
            pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_matches_mid ON matches(mid)")

        # 迁移: 新增半场比分列 (msc S1|; 2026-08-03 用户要求采集完整数据)
        try:
            c.execute("ALTER TABLE matches ADD COLUMN ht_score_home INTEGER")
            c.execute("ALTER TABLE matches ADD COLUMN ht_score_away INTEGER")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE match_outcomes ADD COLUMN ht_score_home INTEGER")
            c.execute("ALTER TABLE match_outcomes ADD COLUMN ht_score_away INTEGER")
        except Exception:
            pass

        # P2: 人工纠偏锁 (零回归核心) — 被人工锁定终场的比赛, 采集器绝不覆盖其赛果/状态.
        # matches 持有纠偏标记 + 可信终场快照; match_outcomes 标记防止历史复盘库被错档覆盖.
        try:
            _mcols = [r[1] for r in c.execute("PRAGMA table_info(matches)").fetchall()]
            if "is_override" not in _mcols:
                c.execute("ALTER TABLE matches ADD COLUMN is_override INTEGER DEFAULT 0")
            if "override_data" not in _mcols:
                c.execute("ALTER TABLE matches ADD COLUMN override_data TEXT")
            if "override_at" not in _mcols:
                c.execute("ALTER TABLE matches ADD COLUMN override_at REAL")
        except Exception:
            pass
        try:
            _ocols = [r[1] for r in c.execute("PRAGMA table_info(match_outcomes)").fetchall()]
            if "is_override" not in _ocols:
                c.execute("ALTER TABLE match_outcomes ADD COLUMN is_override INTEGER DEFAULT 0")
        except Exception:
            pass


# ── 虚拟/电子盘屏蔽 (从根源拦截, 与 gq/auto_collector._is_simulated_league 同源) ──
# 真实联赛名绝不含 "分钟"(虚拟盘单场 8 分钟标记) 或这些虚拟杯名, 误杀安全。
_VIRTUAL_LEAGUE_MARKERS = ("瓦尔哈拉", "瓦尔基里", "梦幻对垒")


def is_virtual_league(league: str) -> bool:
    """判定虚拟/模拟盘(电子盘): VS- 电竞 + 8分钟虚拟杯 + 已知虚拟杯名。

    用于采集器根拦截与 DB 写入层 chokepoint, 使虚拟盘永不入 events.db。
    """
    if not league:
        return False
    if league.startswith("VS-"):
        return True
    if "分钟" in league:
        return True
    if any(k in league for k in _VIRTUAL_LEAGUE_MARKERS):
        return True
    return False


def virtual_league_sql(col: str = "m.league") -> str:
    """返回可在 WHERE 中 AND 的虚拟盘排除 SQL 片段 (与 is_virtual_league 规则一致)。"""
    return (
        f"{col} NOT LIKE 'VS-%' "
        f"AND {col} NOT LIKE '%分钟%' "
        f"AND {col} NOT LIKE '%瓦尔哈拉%' "
        f"AND {col} NOT LIKE '%瓦尔基里%' "
        f"AND {col} NOT LIKE '%梦幻对垒%'"
    )


def upsert_match(match_key: str, home: str, away: str, league: str = "",
                 kickoff: str = "", status: str = "live",
                 score_home: Optional[int] = None,
                 score_away: Optional[int] = None,
                 ht_score_home: Optional[int] = None,
                 ht_score_away: Optional[int] = None,
                 minute: int = 0) -> None:
    """新增或更新比赛元信息

    防御: 队名缺失/空串/占位(' vs ') 的比赛拒绝写入, 避免产生无法按队名解析的空壳行
    (SQLite 的 NOT NULL 拦不住空字符串 '', 故此处显式拦截).
    """
    if not home or not str(home).strip() or not away or not str(away).strip() \
       or str(home).strip() == "vs" or str(away).strip() == "vs":
        return
    # 从根源拦截虚拟/电子盘: 永不写入 events.db matches
    if is_virtual_league(league):
        return
    now = time.time()
    with conn() as c:
        cur = c.execute("SELECT is_override FROM matches WHERE match_key=?", (match_key,))
        _row = cur.fetchone()
        if _row is None:
            c.execute("""INSERT INTO matches
                (match_key, home, away, league, kickoff, status,
                 score_home, score_away, ht_score_home, ht_score_away, minute, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (match_key, home, away, league, kickoff, status,
                 score_home, score_away, ht_score_home, ht_score_away, minute, now, now))
        elif _row[0]:
            # ── 人工纠偏锁: 已锁定的比赛, 采集器绝不覆盖其赛果/状态/分钟 ──
            # 仅刷新 last_seen 标记"仍被采集器观测到", 但冻结所有赛果字段 (零回归).
            c.execute("UPDATE matches SET last_seen=? WHERE match_key=?", (now, match_key))
            return
        else:
            # ── 半场比分(HT)防 clobber ──
            # HT 比分是瞬态值: 仅半场窗口(45')的 msc 含 'S1|' 项, 完场后 msc 只剩 'S0|'
            # 终场比分, _score_from_msc 解析返回 ht=None. upsert_match 在完场后每轮 re-sweep
            # 都会被调用, 若无条件覆盖会把已采集的 HT 比分丢失(实证: 211 场 outcomes 有 ht
            # 但 matches 丢失). 故仅当新解析值非空才覆盖; 旧值非空而新值为空 → 保留旧值.
            _ht_row = cur.execute(
                "SELECT ht_score_home, ht_score_away FROM matches WHERE match_key=?",
                (match_key,)).fetchone()
            _keep_ht_h = ht_score_home if ht_score_home is not None else \
                (_ht_row[0] if _ht_row else None)
            _keep_ht_a = ht_score_away if ht_score_away is not None else \
                (_ht_row[1] if _ht_row else None)
            c.execute("""UPDATE matches SET
                league=?, kickoff=?, status=?, score_home=?, score_away=?,
                ht_score_home=?, ht_score_away=?, minute=?, last_seen=?
                WHERE match_key=?""",
                (league, kickoff, status, score_home, score_away,
                 _keep_ht_h, _keep_ht_a, minute, now, match_key))


def upsert_match_meta(match_key: str, mid: str = None, **fields) -> None:
    """写入/更新赛事内容(前瞻/情报/伤病/阵容).

    仅更新传入的非空字段, 不传的字段保持原值(避免清空已采集内容).
    fields 键: preview / news / injuries_home / injuries_away / lineup_home / lineup_away
    """
    ALLOWED = {"preview", "news", "injuries_home", "injuries_away",
               "lineup_home", "lineup_away"}
    sets, vals = [], []
    now = time.time()
    for k, v in fields.items():
        if k in ALLOWED and v not in (None, ""):
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets and mid is None:
        return
    sets.append("updated_at=?")
    vals.append(now)
    with conn() as c:
        cur = c.execute("SELECT match_key FROM match_meta WHERE match_key=?", (match_key,))
        if cur.fetchone() is None:
            cols = ["match_key"] + ([ "mid" ] if mid is not None else [])
            qmarks = ["?"] * len(cols)
            if sets:
                cols += [s.split("=")[0] for s in sets]
                qmarks += ["?"] * len(sets)
            c.execute(f"INSERT INTO match_meta ({','.join(cols)}) VALUES ({','.join(qmarks)})",
                      [match_key] + ([mid] if mid is not None else []) + vals)
        else:
            if mid is not None:
                sets.insert(0, "mid=?")
                vals.insert(0, mid)
            c.execute(f"UPDATE match_meta SET {','.join(sets)} WHERE match_key=?",
                      vals + [match_key])


def record_snapshot(match_key: str, market: str, selection: str,
                    odds: float, line: Optional[float] = None,
                    score_at: str = "", minute_at: int = 0) -> Optional[dict]:
    """记录一次赔率快照, 检测变化, 返回变化信息

    Returns:
        None — 首次或无变化
        dict  — {from_odds, to_odds, change} 如果有变化
    """
    now = time.time()
    change_info = None
    with conn() as c:
        # 找上次赔率
        cur = c.execute("""SELECT odds FROM odds_snapshots
            WHERE match_key=? AND market=? AND selection=?
            ORDER BY captured_at DESC LIMIT 1""",
            (match_key, market, selection))
        row = cur.fetchone()
        prev_odds = row["odds"] if row else None

        # 插入新快照
        c.execute("""INSERT INTO odds_snapshots
            (match_key, captured_at, market, selection, odds, line, score_at, minute_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (match_key, now, market, selection, odds, line, score_at, minute_at))

        # 变化检测 (有上次的赔率 且 不一致)
        if prev_odds is not None and abs(prev_odds - odds) > 0.001:
            change = round(odds - prev_odds, 4)
            c.execute("""INSERT INTO odds_changes
                (match_key, market, selection, from_odds, to_odds, change, captured_at, score_at, minute_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (match_key, market, selection, prev_odds, odds, change, now, score_at, minute_at))
            change_info = {"from": prev_odds, "to": odds, "change": change}

    return change_info


def get_recent_changes(match_key: str, limit: int = 50) -> list[dict]:
    """最近 N 条赔率变化"""
    with conn() as c:
        rows = c.execute("""SELECT * FROM odds_changes
            WHERE match_key=? ORDER BY captured_at DESC LIMIT ?""",
            (match_key, limit)).fetchall()
        return [dict(r) for r in rows]


def get_latest_odds(match_key: str) -> dict:
    """取某场比赛所有市场最新赔率"""
    with conn() as c:
        rows = c.execute("""SELECT market, selection, odds, line, captured_at, score_at, minute_at
            FROM odds_snapshots s1
            WHERE match_key=? AND captured_at = (
                SELECT MAX(captured_at) FROM odds_snapshots s2
                WHERE s2.match_key=s1.match_key AND s2.market=s1.market AND s2.selection=s1.selection
            )""", (match_key,)).fetchall()
        return {f"{r['market']}/{r['selection']}": dict(r) for r in rows}


def apply_override(match_key: str, home: str, away: str, status: str,
                   score_home, score_away, ht_score_home=None, ht_score_away=None,
                   minute: int = 90, league: str = "", source: str = "manual",
                   mid: Optional[str] = None) -> bool:
    """人工纠偏锁 (完整方案·自动化核心).

    将可信终场写入 matches 并置 is_override=1 + override_data(JSON), 采集器(upsert_match/
    _sweep_finished)后续永不覆盖. 若 match_outcomes 存在同 mid 行, 同步锁定其赛果,
    防止历史复盘库被错档覆盖. 幂等: 重复调用安全.
    """
    now = time.time()
    payload = {"status": status, "score_home": score_home, "score_away": score_away,
               "ht_score_home": ht_score_home, "ht_score_away": ht_score_away,
               "minute": minute, "source": source, "ts": now}
    payload_s = json.dumps(payload, ensure_ascii=False)
    with conn() as c:
        cur = c.execute("SELECT 1 FROM matches WHERE match_key=?", (match_key,))
        if cur.fetchone() is None:
            c.execute("""INSERT INTO matches
                (match_key, home, away, league, status, score_home, score_away,
                 ht_score_home, ht_score_away, minute, is_override, override_data, override_at,
                 first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                (match_key, home, away, league, status, score_home, score_away,
                 ht_score_home, ht_score_away, minute, payload_s, now, now, now))
        else:
            c.execute("""UPDATE matches SET
                status=?, score_home=?, score_away=?, ht_score_home=?, ht_score_away=?,
                minute=?, is_override=1, override_data=?, override_at=?
                WHERE match_key=?""",
                (status, score_home, score_away, ht_score_home, ht_score_away,
                 minute, payload_s, now, match_key))
        # 同步锁定 match_outcomes (若存在同 mid, 用可信赛果覆盖历史错档)
        if mid:
            cur2 = c.execute("SELECT 1 FROM match_outcomes WHERE mid=?", (mid,))
            if cur2.fetchone():
                c.execute("""UPDATE match_outcomes SET
                    score_home=?, score_away=?, ht_score_home=?, ht_score_away=?, is_override=1
                    WHERE mid=?""",
                    (score_home, score_away, ht_score_home, ht_score_away, mid))
    return True


def reassert_overrides() -> int:
    """防御性重断言 (自动化纠偏闭环).

    对所有 is_override=1 的 matches 行, 用 override_data 重写可信赛果, 防止任何直接 DB
    篡改/异常漂移. 采集器本身已被 upsert_match 守卫跳过, 本函数作为每日 recheck 的兜底
    重断言, 确保锁定终场长期不被破坏. 返回重断言行数.
    """
    n = 0
    with conn() as c:
        rows = c.execute(
            "SELECT match_key, override_data FROM matches "
            "WHERE is_override=1 AND override_data IS NOT NULL").fetchall()
        for r in rows:
            try:
                d = json.loads(r["override_data"])
                c.execute("""UPDATE matches SET
                    status=?, score_home=?, score_away=?, ht_score_home=?, ht_score_away=?, minute=?
                    WHERE match_key=?""",
                    (d.get("status"), d.get("score_home"), d.get("score_away"),
                     d.get("ht_score_home"), d.get("ht_score_away"), d.get("minute"),
                     r["match_key"]))
                n += 1
            except Exception:
                pass
    return n


def stats() -> dict:
    """全局统计"""
    with conn() as c:
        m = c.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        s = c.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
        ch = c.execute("SELECT COUNT(*) FROM odds_changes").fetchone()[0]
    return {"matches": m, "snapshots": s, "changes": ch}


# ════════════════════════════════════════════════════════════════════════
# 初盘→赛果对照库
# ════════════════════════════════════════════════════════════════════════

def classify_odds_type(op_1x2_h: Optional[float], op_1x2_d: Optional[float],
                       op_1x2_a: Optional[float],
                       op_ah_line: Optional[float] = None,
                       op_ou_line: Optional[float] = None) -> str:
    """P0a 修复: 仅基于有效的 1X2 赔率做单标签分类, 禁用 AH/OU 线值推断主客强弱。

    返回单一标签 (供策略组按 1X2 强度聚类):
        strong_home   主胜赔 <1.5                (强主)
        home_fav      主胜赔 <2.0 且为三项最低    (主优)
        slight_home   主胜赔 <2.4 且为三项最低    (略主)
        strong_away   客胜赔 <1.5                (强客)
        away_fav      客胜赔 <2.0 且为三项最低    (客优)
        slight_away   客胜赔 <2.4 且为三项最低    (略客)
        low_draw      平赔   <3.2 且为三项最低    (低平)
        balanced      其余                       (均衡)
        unknown       1X2 三字段全 None (绝不回退到污染线值)
    op_ah_line / op_ou_line 仅保留参数兼容, 不再参与分类。
    """
    # 缺失值用极大值兜底, 不污染"最小值"判定
    h = op_1x2_h if op_1x2_h is not None else 99.0
    d = op_1x2_d if op_1x2_d is not None else 99.0
    a = op_1x2_a if op_1x2_a is not None else 99.0

    if h == 99.0 and d == 99.0 and a == 99.0:
        return "unknown"   # 1X2 全缺 → 未知, 不含任何强弱/高低OU误导信息

    mn = min(h, d, a)
    if h == mn and h < 1.5:
        return "strong_home"
    if a == mn and a < 1.5:
        return "strong_away"
    if h == mn and h < 2.0:
        return "home_fav"
    if a == mn and a < 2.0:
        return "away_fav"
    if h == mn and h < 2.4:
        return "slight_home"
    if a == mn and a < 2.4:
        return "slight_away"
    if d == mn and d < 3.2:
        return "low_draw"
    return "balanced"


OPEN_WINDOW_SEC = 120.0  # 初盘时间窗(秒), 与 pipeline/opening_line.py SSoT 对齐


def _market_family(mkt: str) -> str:
    if mkt == "1X2":
        return "1X2"
    if mkt.startswith("AH_"):
        return "AH"
    if mkt.startswith("OU_"):
        return "OU"
    if mkt == "CS":
        return "CS"
    return "OTHER"


def get_opening_odds(match_key: str, window_sec: float = OPEN_WINDOW_SEC) -> dict:
    """取一场比赛各市场的初盘赔率(开赛时间窗内的首批快照)。

    ⚠ 旧实现用 `GROUP BY market, selection` + MIN(captured_at), 不限时间窗,
    会把**赛中**才挂出来的盘口线(进球后线位一路爬到 OU 5.50/6.50)也当成
    "初盘梯队"返回, 平均梯队条数虚高到 9.7 条、均线位被抬到 3.19。
    加 120s 窗后梯队回落到 3.4 条、均线位 3.02(中位 2.75), 抽水 5.62%。
    窗口在 60~600s 之间结果一致 -> 该阈值稳健。
    时间窗按"每个市场族各自的最早采集时刻"计算, 避免某族晚上线被整体裁掉。
    """
    with conn() as c:
        rows = c.execute("""
            SELECT market, selection, odds, line, captured_at
            FROM odds_snapshots
            WHERE match_key=?
            ORDER BY captured_at ASC
        """, (match_key,)).fetchall()

    # 每个市场族各自的开窗起点
    fam_t0: dict = {}
    for r in rows:
        fam = _market_family(r["market"])
        ts = r["captured_at"]
        if ts is None:
            continue
        if fam not in fam_t0 or ts < fam_t0[fam]:
            fam_t0[fam] = ts

    # 按市场整理
    result: dict = {"1X2": {}, "AH": {}, "OU": {}, "CS": []}
    seen: set = set()
    for r in rows:
        mkt = r["market"]
        odds = r["odds"]
        line = r["line"]
        sel = r["selection"]
        ts = r["captured_at"]
        fam = _market_family(mkt)
        # 只保留本市场族开窗内的快照
        t0 = fam_t0.get(fam)
        if window_sec is not None and t0 is not None and ts is not None:
            if ts > t0 + window_sec:
                continue
        # 同一 (market, selection) 只取窗内最早那条 (rows 已按时间升序)
        if (mkt, sel) in seen:
            continue
        seen.add((mkt, sel))
        if mkt == "1X2":
            result["1X2"][sel] = odds
        elif mkt.startswith("AH_"):
            line_k = mkt[3:]  # "0.50"
            if line_k not in result["AH"]:
                result["AH"][line_k] = {"home": 0, "away": 0, "line": line}
            if sel == "home":
                result["AH"][line_k]["home"] = odds
            elif sel == "away":
                result["AH"][line_k]["away"] = odds
        elif mkt.startswith("OU_"):
            line_k = mkt[3:]
            if line_k not in result["OU"]:
                result["OU"][line_k] = {"over": 0, "under": 0, "line": line}
            if sel == "over":
                result["OU"][line_k]["over"] = odds
            elif sel == "under":
                result["OU"][line_k]["under"] = odds
        elif mkt == "CS":
            result["CS"].append((sel, odds))
    # CS 按赔率升序, 取前10
    result["CS"].sort(key=lambda x: x[1])
    result["CS"] = result["CS"][:10]
    return result


def record_match_outcome(mid: str, home: str, away: str, league: str = "",
                         kickoff: str = "",
                         score_home: Optional[int] = None,
                         score_away: Optional[int] = None,
                         ht_score_home: Optional[int] = None,
                         ht_score_away: Optional[int] = None,
                         match_key_override: Optional[str] = None) -> Optional[dict]:
    """比赛结束(HOMEWIN/DRAW/away得分已定) → 归档初盘+赛果对照 (P0a 边界校验版)。

    仅当 match_outcomes 中不存在该 mid 时才写入(幂等)。
    P0a 改动:
      - league 含"友谊"字样 → 直接跳过不归档(早退), 不污染复盘库。
      - op_ah_line 合法范围 abs(line)<=5 否则置 None; op_ou_line 合法范围 line<=10 否则 None。
      - 1X2 三字段全缺 / 线值污染 → is_valid=0 (仍归档, 保留可审计)。
      - classify_odds_type 仅基于有效 1X2 分类, 不再传 AH/OU 线值。
    所有写库 try/except 包裹, 失败仅 log, 不影响采集器主循环。
    返回写入的记录 dict, 若已存在/跳过/数据不全/失败 返回 None。
    """
    try:
        if score_home is None or score_away is None:
            return None
        # P0a: 友谊赛直接跳过, 不归档
        if "友谊" in (league or ""):
            return None

        with conn() as c:
            # P2: 人工纠偏锁 — 被锁定的比赛, 赛果归档也跳过 (防止历史复盘库被错档覆盖).
            # 双重判定: 该 mid 在 match_outcomes 自身已锁, 或对应的 matches 行被锁.
            _mo_ov = c.execute(
                "SELECT is_override FROM match_outcomes WHERE mid=?", (mid,)).fetchone()
            if _mo_ov and _mo_ov[0]:
                return None
            if match_key_override:
                _mk_ov = c.execute(
                    "SELECT is_override FROM matches WHERE match_key=?", (match_key_override,)).fetchone()
                if _mk_ov and _mk_ov[0]:
                    return None
            # ── HT 比分复用(防归档时丢失) ──
            # 完场 sweep 时 msc 已无 'S1|', 实时解析 ht=None; 若 matches 表已存该场 HT
            # 比分(采集器在半场窗口捕获), 复用之, 使下方回填(旧值空+新值可用)能生效.
            if (ht_score_home is None or ht_score_away is None) and mid:
                _mh = c.execute(
                    "SELECT ht_score_home, ht_score_away FROM matches "
                    "WHERE mid=? OR match_key=?", (mid, match_key_override or "")).fetchone()
                if _mh and _mh[0] is not None and _mh[1] is not None:
                    ht_score_home = ht_score_home if ht_score_home is not None else _mh[0]
                    ht_score_away = ht_score_away if ht_score_away is not None else _mh[1]
            cur = c.execute(
                "SELECT score_home, score_away, ht_score_home, ht_score_away "
                "FROM match_outcomes WHERE mid=?", (mid,))
            _exist = cur.fetchone()
            if _exist:
                # ── 幂等 + 单调校正 ──
                # 历史 bug: 采集器把"短暂误判 finished"的比赛提前归档(如 1-0),
                # 之后比赛继续踢到 2-0, matches 已更新但 match_outcomes 永久锁死.
                # 进球数只增不减 => 仅当新观测总进球更多时才校正, 脏数据回退一律忽略.
                _osh, _osa, _oht_h, _oht_a = _exist
                _upd, _params = [], []
                if _osh is not None and _osa is not None and \
                        (score_home + score_away) > (_osh + _osa):
                    if score_home > score_away:
                        _res = "home"
                    elif score_home == score_away:
                        _res = "draw"
                    else:
                        _res = "away"
                    _upd += ["score_home=?", "score_away=?", "result=?"]
                    _params += [score_home, score_away, _res]
                # 半场比分回填: 旧值为空且新值可用 -> 补齐(不覆盖已有值)
                if _oht_h is None and ht_score_home is not None and ht_score_away is not None:
                    _upd += ["ht_score_home=?", "ht_score_away=?"]
                    _params += [ht_score_home, ht_score_away]
                if _upd:
                    _params.append(mid)
                    c.execute(f"UPDATE match_outcomes SET {', '.join(_upd)} WHERE mid=?",
                              tuple(_params))
                    print(f"[DB] 赛果校正 mid={mid} {home} vs {away}: "
                          f"{_osh}-{_osa} -> {score_home}-{score_away} "
                          f"(HT {_oht_h}-{_oht_a} -> {ht_score_home}-{ht_score_away})")
                return None  # 已归档(可能已校正), 不重复 INSERT

            match_key = match_key_override or f"{home} vs {away}"
            opening = get_opening_odds(match_key)

            # 取 1X2 初盘
            h1 = opening["1X2"].get("home")
            d1 = opening["1X2"].get("draw")
            a1 = opening["1X2"].get("away")

            # ── 只取"全场"盘口 key ──
            # get_opening_odds 用 mkt[3:] 切 key, 半场盘口 market='AH_1H_2.50'/'OU_1H_2.50'
            # 会产出 key='1H_2.50' 混进同一字典。旧代码直接 float(key) 抛 ValueError,
            # 导致整个 record_match_outcome 异常 -> 该场比赛彻底不归档(库内 420 场受影响)。
            # op_ah_line/op_ou_line 语义本就是"全场初盘首条线", 故过滤掉非数值 key。
            def _fulltime_keys(d: dict):
                out = []
                for k in d.keys():
                    try:
                        out.append((float(k), k))
                    except (TypeError, ValueError):
                        continue  # '1H_2.50' 等半场 key, 跳过
                return out

            # ── 主盘线 (main line) 选取 ──
            # 旧实现取"线位最小"的一条, 那是梯队最底端的深盘(如 OU 0.5/1.5),
            # 大球恒热 + 抽水更高 -> 制造出"无脑买大 +15% ROI"的假 edge。
            # 正确做法: 庄家同一时刻挂整条梯队, 主盘 = 去水后两边概率最接近 50/50
            # 的那条(抽水最低、信息量最大)。参见 pipeline/opening_line.py 同款逻辑。
            def _pick_main(d: dict, keys, side_a: str, side_b: str):
                """返回 (rec, line_from_key); 无有效两边赔率时返回 (None, None)。"""
                best, best_gap, best_lv = None, 9e9, None
                for lv, k in keys:
                    rec = d.get(k) or {}
                    a, b = rec.get(side_a), rec.get(side_b)
                    try:
                        a, b = float(a), float(b)
                    except (TypeError, ValueError):
                        continue
                    if a <= 1.01 or b <= 1.01:
                        continue
                    inv = 1.0 / a + 1.0 / b
                    gap = abs((1.0 / a) / inv - 0.5)
                    if gap < best_gap:
                        best_gap, best, best_lv = gap, rec, lv
                return best, best_lv

            # 取 AH 主盘 (全场); 无有效两边赔率时回退到 |line| 最小
            ah_pairs = _fulltime_keys(opening["AH"])
            ah_first, ah_lv = _pick_main(opening["AH"], ah_pairs, "home", "away")
            if not ah_first and ah_pairs:
                ah_lv, _k = sorted(ah_pairs, key=lambda t: abs(t[0]))[0]
                ah_first = opening["AH"].get(_k) or {}
            ah_first = ah_first or {}
            ah_line = ah_first.get("line")
            if ah_line is None:
                ah_line = ah_lv  # 快照 line 列为空时, 用 market key 解析出的线位兜底
            ah_home = ah_first.get("home")
            ah_away = ah_first.get("away")

            # 取 OU 主盘 (全场); 无有效两边赔率时回退到 line 最小
            ou_pairs = _fulltime_keys(opening["OU"])
            ou_first, ou_lv = _pick_main(opening["OU"], ou_pairs, "over", "under")
            if not ou_first and ou_pairs:
                ou_lv, _k = sorted(ou_pairs, key=lambda t: t[0])[0]
                ou_first = opening["OU"].get(_k) or {}
            ou_first = ou_first or {}
            ou_line = ou_first.get("line")
            if ou_line is None:
                ou_line = ou_lv
            ou_over = ou_first.get("over")
            ou_under = ou_first.get("under")

            # P0a 边界校验: 污染线值置 None
            ah_polluted = ah_line is not None and abs(ah_line) > 5
            ou_polluted = ou_line is not None and ou_line > 10
            if ah_polluted:
                ah_line = None
            if ou_polluted:
                ou_line = None

            # 赛果
            if score_home > score_away:
                result = "home"
            elif score_home == score_away:
                result = "draw"
            else:
                result = "away"

            # P0a: 仅基于有效 1X2 分类, 不再传 AH/OU 线值
            odds_type = classify_odds_type(h1, d1, a1)

            # P0a: 线值边界校验不过 → is_valid=0 (仍归档保留可审计);
            # 1X2 全缺由 odds_type='unknown' 标记, 不混入 is_valid。
            is_valid = 0 if (ah_polluted or ou_polluted) else 1

            # 波胆JSON
            op_cs = json.dumps([[s, o] for s, o in opening["CS"]], ensure_ascii=False)

            now = time.time()
            c.execute("""INSERT INTO match_outcomes
                (mid, home, away, league, kickoff, score_home, score_away,
                 ht_score_home, ht_score_away, result,
                 op_1x2_h, op_1x2_d, op_1x2_a,
                 op_ah_line, op_ah_home, op_ah_away,
                 op_ou_line, op_ou_over, op_ou_under,
                 op_cs, odds_type, is_valid, captured_at, archived_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, home, away, league, kickoff, score_home, score_away,
                 ht_score_home, ht_score_away, result,
                 h1, d1, a1,
                 ah_line, ah_home, ah_away,
                 ou_line, ou_over, ou_under,
                 op_cs, odds_type, is_valid, now, now))
            return {
                "mid": mid, "home": home, "away": away,
                "kickoff": kickoff,
                "score": f"{score_home}-{score_away}",
                "result": result,
                "odds_type": odds_type,
                "is_valid": is_valid,
                "op_1x2": f"{h1}/{d1}/{a1}",
                "op_ah": f"{ah_line} {ah_home}/{ah_away}",
                "op_ou": f"{ou_line} O{ou_over}/U{ou_under}",
                "op_cs_count": len(opening["CS"]),
            }
    except Exception as e:
        print(f"[DB][WARN] record_match_outcome 失败 mid={mid}: {e}")
        return None


def outcomes_stats() -> dict:
    """初盘→赛果对照统计"""
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM match_outcomes").fetchone()[0]
        by_result = {}
        for r in c.execute("SELECT result, COUNT(*) FROM match_outcomes GROUP BY result").fetchall():
            by_result[r[0]] = r[1]
        by_type = {}
        for r in c.execute("SELECT odds_type, COUNT(*) FROM match_outcomes GROUP BY odds_type ORDER BY COUNT(*) DESC LIMIT 8").fetchall():
            by_type[r[0]] = r[1]
        return {"total": total, "by_result": by_result, "by_type": by_type}


def reclassify_outcomes() -> dict:
    """P0a: 遍历已有 match_outcomes, 用修复后的 classify_odds_type 重算 odds_type,
    按边界校验重设 is_valid, 并清理污染线值(置 NULL)。脏行不删, 保留可审计。
    返回统计 dict。
    """
    init_db()  # 确保 is_valid 列存在
    total = updated = 0
    with conn() as c:
        rows = c.execute(
            "SELECT id, op_1x2_h, op_1x2_d, op_1x2_a, op_ah_line, op_ou_line "
            "FROM match_outcomes"
        ).fetchall()
        for r in rows:
            total += 1
            h, d, a = r["op_1x2_h"], r["op_1x2_d"], r["op_1x2_a"]
            ah, ou = r["op_ah_line"], r["op_ou_line"]

            # 清理污染线值
            ah_clean = ah if (ah is None or abs(ah) <= 5) else None
            ou_clean = ou if (ou is None or ou <= 10) else None
            ah_polluted = ah is not None and abs(ah) > 5
            ou_polluted = ou is not None and ou > 10

            # 修复后: 仅基于有效 1X2 分类
            odds_type = classify_odds_type(h, d, a)
            # 线值边界校验不过 → is_valid=0; 1X2 全缺由 odds_type='unknown' 标记
            is_valid = 0 if (ah_polluted or ou_polluted) else 1

            c.execute(
                "UPDATE match_outcomes SET odds_type=?, is_valid=?, "
                "op_ah_line=?, op_ou_line=? WHERE id=?",
                (odds_type, is_valid, ah_clean, ou_clean, r["id"]))
            updated += 1

    # 统计
    with conn() as c:
        valid = c.execute("SELECT COUNT(*) FROM match_outcomes WHERE is_valid=1").fetchone()[0]
        by_type = {}
        for r in c.execute(
            "SELECT odds_type, COUNT(*) FROM match_outcomes GROUP BY odds_type ORDER BY COUNT(*) DESC"
        ).fetchall():
            by_type[r[0]] = r[1]
        strong_away = c.execute(
            "SELECT COUNT(*) FROM match_outcomes WHERE odds_type='strong_away'"
        ).fetchone()[0]
        high_ou = c.execute(
            "SELECT COUNT(*) FROM match_outcomes WHERE odds_type LIKE '%high_ou%'"
        ).fetchone()[0]
        ah_bad = c.execute(
            "SELECT COUNT(*) FROM match_outcomes WHERE op_ah_line IS NOT NULL AND abs(op_ah_line)>5"
        ).fetchone()[0]
        ou_bad = c.execute(
            "SELECT COUNT(*) FROM match_outcomes WHERE op_ou_line IS NOT NULL AND op_ou_line>10"
        ).fetchone()[0]
    return {"total": total, "updated": updated, "valid": valid,
            "by_type": by_type, "strong_away_rows": strong_away,
            "high_ou_rows": high_ou, "remaining_ah_polluted": ah_bad,
            "remaining_ou_polluted": ou_bad}


# ══════════════════════════════════════════════════════════════════════
# V7.1 复盘链路 — 赛事分析缓存 / 赛后修正 / 批量补算
#   SSoT: 分析快照直接来自 _live_predict 返回的 r 对象, 绝不重造分析逻辑
#   所有写/修正函数均向上抛异常, 由调用方(bridge_service) try/except 包裹
# ══════════════════════════════════════════════════════════════════════

def ensure_analysis_cache():
    """惰性确保 match_analysis_cache 表存在 (幂等)。

    WS3: 额外幂等 ALTER 扩列 (strongest_signal/signal_score/deviation_pct/
    roi/label/reviewed_by/review_note/override_result/override_verdict),
    用于赛果复盘自动复核的最强信号/偏差/ROI/标签闭环。已存在则跳过。
    """
    with conn() as c:
        c.executescript(_MAC_DDL)
        # 幂等扩列: 仅新增, 不改动既有列
        _MAC_ALTER_COLS = [
            "strongest_signal TEXT",        # 最强信号 (analysis_center 综合评分>=7 的信号)
            "signal_score REAL",            # 信号强度分 (0..10)
            "deviation_pct REAL",           # 跨庄分歧 % (leyu 跨庄共识, 替代单庄自共识≈0)
            "roi REAL",                     # 真实下注 ROI (bet_records 已结算 PnL, 无则 NULL)
            "label TEXT",                   # 盘口标签 (handicap_labels, 如 强弱/对攻/闷平)
            "signal_color TEXT",            # 三色情绪标签主色 red/yellow/green
            "signal_tag TEXT",              # 三色标签全文 如 "🔴 冷平预警"
            "signal_meaning TEXT",          # 标签一句话释义
            # ── 2026-08-08 自动复核接入: 结构化赛前结论 (按 GQ match_key 键) ──
            "match_key TEXT",              # GQ match_key (新自动复核行键; 旧 mid 行留 NULL)
            "predicted_direction TEXT",     # 赛前结论方向 (主胜/平局/客胜) — 结构化赛前结论
            "predicted_from TEXT",          # 结论来源 prematch_knn/manual/override
            "predicted_excess REAL",       # 该方向 excess(freq-市场)*100, 有符号(跑赢+/跑输-)
            "predicted_roi REAL",          # 该方向历史 ROI%
            "draw_signal INTEGER",         # 平局预警触发 0/1
            "cold_signal INTEGER",         # 冷门标签触发 0/1 (红/冷门类)
            "auto_reviewed_at REAL",       # 自动复核时间戳
            "reviewed_by TEXT",             # 人工复核人 (override 时填)
            "review_note TEXT",             # 复核备注
            "override_result TEXT",         # 人工纠正赛果 home/draw/away
            "override_verdict TEXT",        # 人工纠正方向 主胜/平局/客胜
        ]
        cur = c.execute("PRAGMA table_info(match_analysis_cache)")
        existing = {r[1] for r in cur.fetchall()}
        for col_def in _MAC_ALTER_COLS:
            col_name = col_def.split(" ", 1)[0]
            if col_name not in existing:
                c.execute(f"ALTER TABLE match_analysis_cache ADD COLUMN {col_def}")


def get_cache_row(mid: str) -> Optional[dict]:
    """取单条分析缓存原始行 (含修正字段)。"""
    with conn() as c:
        row = c.execute("SELECT * FROM match_analysis_cache WHERE mid=?", (mid,)).fetchone()
        return dict(row) if row else None


def get_outcome_by_mid(mid: str) -> Optional[dict]:
    """取 match_outcomes 单场赛果 (初盘+终场)。"""
    with conn() as c:
        row = c.execute("SELECT * FROM match_outcomes WHERE mid=?", (mid,)).fetchone()
        return dict(row) if row else None


def save_analysis(mid: str, r_dict: dict) -> bool:
    """赛前分析快照落库 (幂等: 同 mid 已存在则跳过)。

    字段全部来自 r_dict (SSoT), 映射:
      verdict          <- r.direction            (主胜/平局/客胜)
      pred_score_*    <- r.oip.top3_scores[0]  ("X-Y" 拆分)
      edge            <- r.value_layer.best_edge_pct (%)
      stake_suggestion<- r.value_layer.best_direction (H/D/A/PASS)
      stake_amount    <- BET时 r.value_layer.scenario.stake, 否则 1.0
      confidence      <- r.market_conf (0..1)
      odds_type       <- None (赛后修正时从 match_outcomes 回填)
      snapshot_ref    <- r.odds {oh/od/oa}
    返回 True=已写入, False=已存在跳过。失败向上抛 (调用方捕获)。
    """
    ensure_analysis_cache()
    with conn() as c:
        cur = c.execute("SELECT 1 FROM match_analysis_cache WHERE mid=?", (mid,))
        if cur.fetchone():
            return False  # 已缓存, 跳过 (首次去重)

        verdict = r_dict.get("direction")  # 主胜/平局/客胜
        oip = r_dict.get("oip", {}) or {}
        top3 = oip.get("top3_scores", []) or []
        pred_h = pred_a = None
        if top3 and "-" in str(top3[0]):
            try:
                _hh, _aa = str(top3[0]).split("-")[:2]
                pred_h, pred_a = int(round(float(_hh))), int(round(float(_aa)))
            except (ValueError, TypeError):
                pred_h = pred_a = None

        vl = r_dict.get("value_layer", {}) or {}
        edge = vl.get("best_edge_pct")
        stake_suggestion = vl.get("best_direction")  # H/D/A/PASS

        # 注码额: BET 时取 scenario.stake (模型建议¥注码), 否则默认 1 单位
        stake_amount = 1.0
        if vl.get("decision") == "BET":
            _sc = (vl.get("scenario") or {}).get("stake")
            try:
                if _sc and float(_sc) > 0:
                    stake_amount = float(_sc)
            except (ValueError, TypeError):
                pass

        confidence = r_dict.get("market_conf")  # 0..1
        odds = r_dict.get("odds", {}) or {}
        snapshot_ref = f"{odds.get('oh')}/{odds.get('od')}/{odds.get('oa')}"

        c.execute("""INSERT INTO match_analysis_cache
            (mid, captured_at, verdict, pred_score_home, pred_score_away,
             edge, stake_suggestion, stake_amount, confidence, odds_type, snapshot_ref)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, time.time(), verdict, pred_h, pred_a,
             edge, stake_suggestion, stake_amount, confidence, None, snapshot_ref))
        return True


# 方向映射常量 (集中定义, 避免散落硬编码)
_VERDICT_TO_RESULT = {"主胜": "home", "平局": "draw", "客胜": "away"}
_BETDIR_TO_RESULT = {"H": "home", "D": "draw", "A": "away"}


def correct_analysis(mid: str) -> Optional[dict]:
    """赛后修正单场: 读 match_outcomes 赛果, 计算命中/比分差/注码PnL/偏差说明。

    计算规则:
      verdict_hit : r.verdict 方向 vs result(home/draw/away)
                    → 'hit' / 'miss' / 'miss_draw'(verdict平但结果非平)
      score_err    : |pred_home - score_home| + |pred_away - score_away|
      stake_pnl    : 按 stake_suggestion(H/D/A)方向 + 赔率硬结算
                    命中 (odds-1)*stake, 未中 -stake; PASS→0; 无赔率→None
                    赔率取 match_outcomes.op_1x2_*
      deviation_note: 简短中文 (如 "verdict对/比分差2球/注码盈")
    无赛果(未归档或比分缺失)返回 None。幂等: 重复调用覆盖修正字段。
    """
    row = get_cache_row(mid)
    if row is None:
        return None
    outcome = get_outcome_by_mid(mid)
    # 人工覆盖优先于自动赛果
    ovr_result = row.get("override_result")
    if ovr_result in ("home", "draw", "away"):
        result = ovr_result
    else:
        if outcome is None:
            return None
        result = outcome.get("result")  # home/draw/away
    if result not in ("home", "draw", "away"):
        return None
    # 比分: 优先 match_outcomes; 缺失则 score_err=None (仍可做 verdict/roi 复核)
    score_h = (outcome or {}).get("score_home")
    score_a = (outcome or {}).get("score_away")

    # ── verdict_hit ──
    # 人工纠正方向优先
    verdict = row.get("override_verdict") or row.get("verdict")
    v_res = _VERDICT_TO_RESULT.get(verdict)
    if v_res == result:
        verdict_hit = "hit"
    elif verdict == "平局" and result != "draw":
        verdict_hit = "miss_draw"
    else:
        verdict_hit = "miss"

    # ── score_err ──
    ph = row.get("pred_score_home")
    pa = row.get("pred_score_away")
    score_err = None
    if ph is not None and pa is not None:
        score_err = abs(int(ph) - int(score_h)) + abs(int(pa) - int(score_a))

    # ── stake_pnl (硬结算) ──
    direction = row.get("stake_suggestion")  # H/D/A/PASS
    stake_pnl = None
    if direction in ("H", "D", "A"):
        _om = {"H": outcome.get("op_1x2_h"),
                "D": outcome.get("op_1x2_d"),
                "A": outcome.get("op_1x2_a")}
        odds_used = _om.get(direction)
        _bet_res = _BETDIR_TO_RESULT[direction]
        _hit = (_bet_res == result)
        _stake = float(row.get("stake_amount") or 1.0)
        if odds_used is not None:
            stake_pnl = round((float(odds_used) - 1.0) * _stake, 4) if _hit else round(-_stake, 4)
        else:
            stake_pnl = None  # 无赔率, 无法硬结算
    else:
        stake_pnl = 0.0  # PASS, 无下注

    # ── deviation_note (简短中文) ──
    _parts = []
    if verdict_hit == "hit":
        _parts.append("verdict对")
    elif verdict_hit == "miss_draw":
        _parts.append("verdict平错")
    else:
        _parts.append("verdict错")
    if score_err is not None:
        _parts.append(f"比分差{score_err}球")
    if direction in ("H", "D", "A"):
        if stake_pnl is None:
            _parts.append("注码无赔率")
        elif stake_pnl > 0:
            _parts.append("注码盈")
        elif stake_pnl == 0:
            _parts.append("注码平")
        else:
            _parts.append("注码亏")
    deviation_note = "/".join(_parts)

    # ── WS3: 自动复核维度 (最强信号/偏差/ROI/标签) ──
    # 最强信号 = 价值层下注方向; 信号强度 = 最佳 edge %
    strongest_signal = row.get("stake_suggestion")   # H/D/A/PASS
    signal_score = row.get("edge")                  # best_edge_pct

    # 偏差% / ROI / 标签 来自 football_data.db (bet_records + handicap_labels)
    deviation_pct, roi, label = _load_review_signals(mid)

    odds_type = outcome.get("odds_type")
    now = time.time()

    # ── 三色情绪标签 (红绿灯+关键词) ──
    # 仅当 deviation_pct 可用时计算; 缺失则留 NULL (不伪造标签, 铁律: 数据有据可查)。
    # deviation_pct 按有符号处理: +=跑赢庄家预期, -=跑输庄家预期。
    signal_color = signal_tag = signal_meaning = None
    if deviation_pct is not None:
        try:
            from pipeline.signal_label import compute_signal_label
            _t = compute_signal_label(strongest_signal, deviation_pct, roi)
            signal_color = _t.get("color")
            signal_tag = _t.get("tag")
            signal_meaning = _t.get("meaning")
        except Exception:
            pass

    with conn() as c:
        c.execute("""UPDATE match_analysis_cache SET
            result_actual=?, verdict_hit=?, score_err=?, stake_pnl=?,
            deviation_note=?, odds_type=?, corrected_at=?,
            strongest_signal=?, signal_score=?, deviation_pct=?, roi=?, label=?,
            signal_color=?, signal_tag=?, signal_meaning=?
            WHERE mid=?""",
            (result, verdict_hit, score_err, stake_pnl,
             deviation_note, odds_type, now,
             strongest_signal, signal_score, deviation_pct, roi, label,
             signal_color, signal_tag, signal_meaning, mid))
    return get_cache_row(mid)


def _load_review_signals(mid: str):
    """WS3: 从 football_data.db 取复盘复核维度 (偏差%/ROI/标签).

    返回 (deviation_pct, roi, label):
      deviation_pct: bet_records.value_gap*100 (模型vs市场分歧%; 无则 None)
      roi          : bet_records 真实结算 PnL ((odds-1)命中 / -1未中, 占1单位注;
                     未结算/无记录 → None)
      label        : handicap_labels.handicap_bin (盘口标签; 无则 None)
    任一源缺失均优雅回退 None, 不阻断主流程。
    """
    try:
        import os as _os
        _fd = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                            "data", "football_data.db")
        if not _os.path.exists(_fd):
            return None, None, None
        import sqlite3 as _sql
        fc = _sql.connect(_fd); fc.row_factory = _sql.Row
        deviation_pct = None
        roi = None
        label = None
        try:
            _mid_int = int(mid) if str(mid).isdigit() else None
        except (ValueError, TypeError):
            _mid_int = None
        # bet_records: 同 match_id
        if _mid_int is not None:
            br = fc.execute(
                "SELECT value_gap, expected_value, predicted_result, actual_result, "
                "home_odds, draw_odds, away_odds FROM bet_records WHERE match_id=? "
                "ORDER BY bet_id DESC LIMIT 1", (mid,)).fetchone()
            if br:
                vg = br["value_gap"]
                if vg is not None:
                    try:
                        deviation_pct = round(float(vg) * 100.0, 2)
                    except (ValueError, TypeError):
                        deviation_pct = None
                # 真实 ROI: 已结算 (actual_result 非空)
                ar = br["actual_result"]
                pr = br["predicted_result"]
                if ar in ("H", "D", "A") and pr in ("H", "D", "A"):
                    _odds_map = {"H": br["home_odds"], "D": br["draw_odds"], "A": br["away_odds"]}
                    _o = _odds_map.get(pr)
                    if _o is not None:
                        try:
                            _o = float(_o)
                            roi = round((_o - 1.0) if (pr == ar) else -1.0, 4)
                        except (ValueError, TypeError):
                            roi = None
            # handicap_labels: 同 match_id
            hl = fc.execute(
                "SELECT handicap_bin, cover_result FROM handicap_labels WHERE match_id=? "
                "LIMIT 1", (mid,)).fetchone()
            if hl:
                label = hl["handicap_bin"] or hl["cover_result"]
        fc.close()
        return deviation_pct, roi, label
    except Exception:
        return None, None, None


def apply_review(mid: str, override_result: Optional[str] = None,
                 override_verdict: Optional[str] = None,
                 reviewed_by: Optional[str] = None,
                 review_note: Optional[str] = None) -> bool:
    """WS3: 写入人工纠偏字段 (override_result/override_verdict/reviewed_by/review_note)。
    返回是否写入成功。随后由调用方 correct_analysis(mid) 重算全部维度 (覆盖优先)。"""
    ensure_analysis_cache()
    with conn() as c:
        cur = c.execute("SELECT 1 FROM match_analysis_cache WHERE mid=?", (mid,))
        if not cur.fetchone():
            return False
        c.execute("""UPDATE match_analysis_cache SET
            override_result=?, override_verdict=?, reviewed_by=?, review_note=?
            WHERE mid=?""",
            (override_result, override_verdict, reviewed_by, review_note, mid))
    return True


def backfill_all() -> int:
    """批量赛后修正: 遍历已缓存但未修正、且 match_outcomes 已有赛果的 mid。
    返回补算条数。单场异常不影响整体 (跳过继续)。"""
    ensure_analysis_cache()
    with conn() as c:
        rows = c.execute(
            "SELECT c.mid FROM match_analysis_cache c "
            "LEFT JOIN match_outcomes o ON o.mid=c.mid "
            "WHERE c.corrected_at IS NULL "
            "AND o.mid IS NOT NULL "
            "AND o.score_home IS NOT NULL AND o.score_away IS NOT NULL"
        ).fetchall()
    cnt = 0
    for r in rows:
        try:
            if correct_analysis(r["mid"]) is not None:
                cnt += 1
        except Exception:
            # 单场失败不影响批量; 留待下次 backfill
            continue
    return cnt


def query_analysis_cache(date: str = "", league: str = "",
                        result: str = "", verdict_hit: str = "") -> list:
    """查询分析缓存 (LEFT JOIN match_outcomes + matches 取联赛/日期/队名)。

    键空间: 旧行按 match_outcomes.mid; 2026-08-08 自动复核行按 GQ match_key
    (c.match_key 非空时 JOIN matches 取队名/赛果, 否则回退 match_outcomes)。
    支持筛选: date(开赛日前缀) / league(模糊) / result(home/draw/away) / verdict_hit。
    懒修正: 仅对「旧 mid 行且 match_outcomes 有赛果 但 未修正」即时 correct;
            新 match_key 行已自动复核完成, 跳过懒修正 (避免误用 match_outcomes 旧键)。
    返回合并后的 dict 列表 (含 o_league/o_kickoff/o_home/o_away/o_result 字段)。
    """
    ensure_analysis_cache()
    clauses, params = [], []
    # 虚拟/电子盘赛果双源屏蔽 (match_outcomes.is_virtual 与 matches.league 命名规则)
    clauses.append("(COALESCE(o.is_virtual, 0) = 0)")
    clauses.append(f"({virtual_league_sql('m.league')})")
    if league:
        clauses.append("(COALESCE(m.league, o.league) LIKE ?)")
        params.append(f"%{league}%")
    if date:
        clauses.append("(COALESCE(m.kickoff, o.kickoff) LIKE ?)")
        params.append(f"{date}%")
    if result:
        clauses.append("COALESCE(c.result_actual, o.result) = ?"); params.append(result)
    if verdict_hit:
        clauses.append("c.verdict_hit = ?"); params.append(verdict_hit)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = (
        "SELECT c.*, "
        "COALESCE(m.league, o.league) AS o_league, "
        "COALESCE(m.kickoff, o.kickoff) AS o_kickoff, "
        "COALESCE(m.home, o.home) AS o_home, "
        "COALESCE(m.away, o.away) AS o_away, "
        "COALESCE(c.result_actual, o.result) AS o_result "
        "FROM match_analysis_cache c "
        "LEFT JOIN match_outcomes o ON o.mid = c.mid "
        "LEFT JOIN matches m ON m.match_key = c.match_key"
        f"{where} ORDER BY c.captured_at DESC"
    )
    out = []
    with conn() as c:
        rows = c.execute(sql, params).fetchall()
    for r in rows:
        d = dict(r)
        # 懒修正: 仅旧 mid 行 (无 match_key) 且有赛果未修正 → correct_analysis
        if (d.get("match_key") is None
                and d.get("o_result") in ("home", "draw", "away")
                and d.get("corrected_at") is None):
            try:
                corrected = correct_analysis(d["mid"])
                if corrected:
                    for k in ("result_actual", "verdict_hit", "score_err",
                              "stake_pnl", "deviation_note", "odds_type",
                              "corrected_at"):
                        d[k] = corrected.get(k)
            except Exception:
                pass
        out.append(d)
    return out


# ════════════════════════════════════════════════════════════════
# 2026-08-08 自动复核接入: 赛前结论(KNN单结论) vs 赛后实际赛果
#
# 设计要点 (提取自用户方案的最优点):
#  · 结构化赛前结论 = prematch_similarity.query_match() 的确定性单结论,
#    按 GQ match_key 键入 match_analysis_cache.predicted_direction。
#  · 有符号偏差 = query_match 返回的 excess[方向] = (freq-市场)*100,
#    直接喂 compute_signal_label → 三色标签 (不依赖缺失的 bet_records feed)。
#  · 命中规则: 赛前方向(H/D/A) == 实际赛果(H/D/A) → hit; 否则 miss。
#  · 增量: auto_review_all 跳过已 auto_reviewed_at 的行, 适合每日 cron。
#  · 铁律: 无赛前盘口的比赛不伪造预测 (predicted_direction=NULL, 仅存赛果)。
# ════════════════════════════════════════════════════════════════
def ensure_prematch_conclusion():
    """赛前结论固化表: 赛前(展示/冻结)时把"我们告诉用户的确定性结论"存下来,
    供赛后机关(finish trigger)直接比对, 无需赛后重跑 KNN、且严格忠于展示。"""
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS prematch_conclusion (
            match_key    TEXT PRIMARY KEY,
            verdict_code TEXT,            -- H/D/A
            verdict_cn   TEXT,            -- 主胜/平局/客胜
            excess_json  TEXT,            -- {H,D,A} 有符号偏差 JSON
            roi_json     TEXT,            -- {H,D,A} ROI JSON
            draw_signal  INTEGER,
            captured_at  REAL
        )""")


def store_prematch_conclusion(match_key, verdict_code, verdict_cn, excess, roi,
                              draw_signal=0):
    """固化赛前结论 (赛前展示/冻结时调用一次). 赛后机关直接读此行比对."""
    import json
    ensure_prematch_conclusion()
    try:
        excess_j = json.dumps(excess, ensure_ascii=False) if excess else None
        roi_j = json.dumps(roi, ensure_ascii=False) if roi else None
    except Exception:
        excess_j = roi_j = None
    now = time.time()
    with conn() as c:
        c.execute("""INSERT INTO prematch_conclusion
            (match_key, verdict_code, verdict_cn, excess_json, roi_json, draw_signal, captured_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(match_key) DO UPDATE SET
                verdict_code=excluded.verdict_code, verdict_cn=excluded.verdict_cn,
                excess_json=excluded.excess_json, roi_json=excluded.roi_json,
                draw_signal=excluded.draw_signal, captured_at=excluded.captured_at""",
            (match_key, verdict_code, verdict_cn, excess_j, roi_j,
             int(draw_signal or 0), now))
    return True


def auto_review_match(match_key: str, prefer_stored: bool = True) -> Optional[dict]:
    """单场自动复核: 取 GQ 真实赛果 + prematch KNN 赛前结论, 写复盘行。

    返回 dict(含 skipped/predicted_direction/verdict_hit) 或 None(比赛不存在)。
    命中口径见 query_match: verdict==actual → hit。
    """
    with conn() as c:
        c.row_factory = sqlite3.Row
        m = c.execute(
            "SELECT home,away,league,kickoff,status,score_home,score_away "
            "FROM matches WHERE match_key=?", (match_key,)).fetchone()
    if m is None:
        return None
    if (m['status'] or '').lower() != 'finished' or m['score_home'] is None \
            or m['score_away'] is None:
        return {'match_key': match_key, 'skipped': True,
                'reason': 'not_finished_or_no_score'}
    if is_virtual_league(m['league']):
        return {'match_key': match_key, 'skipped': True, 'reason': 'virtual'}

    sh, sa = int(m['score_home']), int(m['score_away'])
    actual = 'H' if sh > sa else ('A' if sa > sh else 'D')
    result_actual = {'H': 'home', 'D': 'draw', 'A': 'away'}[actual]

    ensure_prematch_conclusion()   # 确保固化表存在 (机关读此表)
    # ── 赛前结论: 优先读"赛前固化结论"(机关路径, 严格忠于展示), 否则赛后重跑 KNN 兜底 ──
    predicted = predicted_from = None
    excess_pp = roi_dir = None
    verdict_hit = None
    signal_color = signal_tag = signal_meaning = None
    draw_signal = cold_signal = 0
    verdict = None
    stored = None
    if prefer_stored:
        try:
            with conn() as c:
                c.row_factory = sqlite3.Row
                stored = c.execute(
                    "SELECT verdict_code,verdict_cn,excess_json,roi_json,draw_signal "
                    "FROM prematch_conclusion WHERE match_key=?", (match_key,)
                ).fetchone()
        except Exception:
            stored = None
    if stored and stored['verdict_code']:
        import json
        verdict = stored['verdict_code']                       # H/D/A
        predicted = stored['verdict_cn']                       # 主胜/平局/客胜
        predicted_from = 'prematch_stored'
        try:
            _ex = json.loads(stored['excess_json']) if stored['excess_json'] else {}
            excess_pp = round(float(_ex.get(verdict, 0.0)) * 100.0, 2)   # 有符号
        except Exception:
            excess_pp = None
        try:
            _roi = json.loads(stored['roi_json']) if stored['roi_json'] else {}
            roi_dir = _roi.get(verdict)
        except Exception:
            roi_dir = None
        draw_signal = int(stored['draw_signal'] or 0)
        verdict_hit = 'hit' if verdict == actual else 'miss'
        # 三色标签: 用该方向有符号偏差喂 compute_signal_label
        try:
            from pipeline.signal_label import compute_signal_label
            _t = compute_signal_label(verdict, excess_pp, roi_dir)
            signal_color = _t.get('color'); signal_tag = _t.get('tag'); signal_meaning = _t.get('meaning')
            cold_signal = 1 if signal_color == 'red' else 0
        except Exception:
            pass
    else:
        # 兜底: 赛后重跑 KNN (无固化结论时, 如本机制上线前已完场/从未展示的比赛)
        try:
            from pipeline import prematch_similarity as _pm
            r = _pm.query_match(match_key, k=_pm.DEFAULT_K, draw_upgrade=True)
        except Exception:
            r = {'applicable': False, 'reason': 'engine_error'}
        if r.get('applicable'):
            verdict = r['verdict']                 # H/D/A
            predicted = r['verdict_cn']            # 主胜/平局/客胜
            predicted_from = 'prematch_knn'
            excess_pp = round(r['excess'].get(verdict, 0.0) * 100.0, 2)   # 有符号
            roi_dir = r['roi'].get(verdict)
            verdict_hit = 'hit' if verdict == actual else 'miss'
            draw_signal = 1 if r.get('draw_alert') else 0
            try:
                from pipeline.signal_label import compute_signal_label
                _t = compute_signal_label(verdict, excess_pp, roi_dir)
                signal_color = _t.get('color'); signal_tag = _t.get('tag'); signal_meaning = _t.get('meaning')
                cold_signal = 1 if signal_color == 'red' else 0
            except Exception:
                pass

    now = time.time()
    with conn() as c:
        cur = c.execute("SELECT 1 FROM match_analysis_cache WHERE mid=?",
                        (match_key,)).fetchone()
        if cur is None:
            c.execute("INSERT INTO match_analysis_cache(mid, match_key, captured_at) "
                      "VALUES(?,?,?)", (match_key, match_key, now))
        c.execute("""UPDATE match_analysis_cache SET
            match_key=?, predicted_direction=?, predicted_from=?,
            predicted_excess=?, predicted_roi=?, draw_signal=?, cold_signal=?,
            result_actual=?, verdict_hit=?, signal_color=?, signal_tag=?,
            signal_meaning=?, auto_reviewed_at=?, corrected_at=?
            WHERE mid=?""",
            (match_key, predicted, predicted_from, excess_pp, roi_dir,
             draw_signal, cold_signal, result_actual, verdict_hit,
             signal_color, signal_tag, signal_meaning, now, now, match_key))
    return get_cache_row(match_key)


def auto_review_all(limit: Optional[int] = None, force: bool = False) -> dict:
    """批量自动复核: 遍历 GQ 已完场+有比分比赛, 增量写复盘。

    参数
    ----
    limit : 最多处理多少场 (None=全部)。on-demand 端点建议设上限。
    force : True 时重算已 auto_reviewed 的行; False 跳过 (增量)。
    返回统计 dict: total/reviewed/hit/miss/no_prematch/skipped/errors。
    """
    ensure_analysis_cache()
    with conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT match_key FROM matches "
            "WHERE status='finished' AND score_home IS NOT NULL "
            f"AND score_away IS NOT NULL AND ({virtual_league_sql('league')})"
        ).fetchall()
    stats = dict(total=0, reviewed=0, hit=0, miss=0,
                 no_prematch=0, skipped=0, errors=0)
    for i, r in enumerate(rows):
        if limit is not None and stats['total'] >= int(limit):
            break
        mk = r['match_key']
        if not force:
            ex = get_cache_row(mk)
            if ex and ex.get('auto_reviewed_at') is not None:
                continue
        stats['total'] += 1
        try:
            res = auto_review_match(mk)
            if res is None or res.get('skipped'):
                stats['skipped'] += 1
            elif res.get('predicted_direction') is None:
                stats['no_prematch'] += 1
            else:
                stats['reviewed'] += 1
                if res.get('verdict_hit') == 'hit':
                    stats['hit'] += 1
                else:
                    stats['miss'] += 1
        except Exception:
            stats['errors'] += 1
    return stats


def auto_review_stats() -> dict:
    """聚合自动复核命中率统计 (供前端「历史命中率」卡片 /api/analysis/review-stats).

    仅统计 auto_review 写入的行 (predicted_direction IS NOT NULL 隔离 KNN 子集,
    排除 WS3 correct_analysis 仅写 verdict_hit 但无赛前结论的行)。
    返回: total_cache/reviewed/hit/miss/no_prematch/hit_rate + by_direction[H/D/A] + last_reviewed_at。
    """
    with conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN predicted_direction IS NOT NULL THEN 1 ELSE 0 END) AS reviewed,
                SUM(CASE WHEN predicted_direction IS NOT NULL AND verdict_hit='hit' THEN 1 ELSE 0 END) AS hit,
                SUM(CASE WHEN predicted_direction IS NOT NULL AND verdict_hit IN ('miss','miss_draw') THEN 1 ELSE 0 END) AS miss,
                SUM(CASE WHEN auto_reviewed_at IS NOT NULL AND predicted_direction IS NULL THEN 1 ELSE 0 END) AS no_prematch,
                MAX(auto_reviewed_at) AS last_reviewed_at
            FROM match_analysis_cache
        """).fetchone()
        by_dir = {}
        for cn, code in (("主胜", "H"), ("平局", "D"), ("客胜", "A")):
            rr = c.execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN verdict_hit='hit' THEN 1 ELSE 0 END) AS h "
                "FROM match_analysis_cache WHERE predicted_direction=?", (cn,)).fetchone()
            n = int(rr['n'] or 0)
            h = int(rr['h'] or 0)
            by_dir[code] = {"direction_cn": cn, "n": n, "hit": h,
                            "hit_rate": round(h / n, 4) if n else None}
    reviewed = int(row['reviewed'] or 0)
    hit = int(row['hit'] or 0)
    return {
        "total_cache": int(row['total'] or 0),
        "reviewed": reviewed,
        "hit": hit,
        "miss": int(row['miss'] or 0),
        "no_prematch": int(row['no_prematch'] or 0),
        "hit_rate": round(hit / reviewed, 4) if reviewed else None,
        "by_direction": by_dir,
        "last_reviewed_at": row['last_reviewed_at'],
    }


# ════════════════════════════════════════════════════════════════
# 赛前波胆(CS / 比分)赔率归档 + 赛果验证
# 需求: 采集每场开赛前的比分赔率 → 赛后按赛果验证 → 入库历史可查(类似31K)
# 铁律: 仅采集 status='scheduled' (未开赛) 的比赛; 已开赛(live/finished)不采
# ════════════════════════════════════════════════════════════════

def ensure_cs_tables():
    """建立赛前波胆冻结表 + 赛果验证归档表 (幂等)。"""
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS pre_match_cs (
            match_key TEXT PRIMARY KEY,
            home TEXT, away TEXT, league TEXT,
            kickoff TEXT,
            frozen_at REAL,
            n_scores INT,
            odds_json TEXT,
            created_at REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS cs_verification (
            match_key TEXT PRIMARY KEY,
            home TEXT, away TEXT, league TEXT,
            kickoff TEXT,
            actual_score TEXT,
            actual_home INT, actual_away INT,
            pre_odds_json TEXT,
            actual_odds REAL,
            actual_implied REAL,
            favorite_score TEXT,
            favorite_odds REAL,
            fav_hit INT,
            hit INT,
            margin REAL,
            source TEXT,
            verified_at REAL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_csv_league ON cs_verification(league)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_csv_kickoff ON cs_verification(kickoff)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_csv_actual ON cs_verification(actual_score)")


def _kickoff_ts(kickoff):
    try:
        return datetime.strptime(kickoff[:16], "%Y-%m-%d %H:%M").timestamp()
    except Exception:
        return None


def _pre_market_batch(match_key, kickoff=None, min_scores=5):
    """取赛前(<=kickoff)最完整的 CS 盘口批次。

    返回 (sec, {score: odds})。若无 kickoff(未开赛), 取最近完整批次。
    captured_at 为微秒级浮点, 同一采集批次的分数共享同一整数秒, 故按
    CAST(captured_at AS INT) 分批, 选分数最多(最完整)的批次。
    """
    sql = """SELECT CAST(captured_at AS INT) AS sec, selection, odds
             FROM odds_snapshots WHERE match_key=? AND market='CS'"""
    params = [match_key]
    if kickoff is not None:
        kt = _kickoff_ts(kickoff)
        if kt is not None:
            sql += " AND captured_at <= ?"
            params.append(kt)
    sql += " ORDER BY sec DESC"
    with conn() as c:
        rows = c.execute(sql, params).fetchall()
    batches = {}
    for sec, sel, odds in rows:
        # 卫生过滤: 正确比分(CS)赔率 <=2.0 或 >1000 视为采集噪声(GQ 对部分
        # 野鸡联赛偶发 0.0/1.0/1.14 等异常值, 真实CS最短也在 3.5+)。丢弃以免
        # 污染 favorite/margin/implied 计算。不伪造, 仅剔除明显错误值。
        if odds is None or odds <= 2.0 or odds > 1000:
            continue
        # 比分标签归一: '0-0'/'0.0' → '0:0' (英文冒号格式)
        nsel = normalize_cs_score(sel)
        if nsel is None:
            continue
        batches.setdefault(sec, {})[nsel] = odds
    best_sec = None
    best_mkt = None
    for sec in sorted(batches.keys(), reverse=True):
        mkt = batches[sec]
        if best_mkt is None or len(mkt) > len(best_mkt):
            best_mkt, best_sec = mkt, sec
        if best_mkt is not None and len(best_mkt) >= min_scores:
            break
    if best_mkt is None or len(best_mkt) < min_scores:
        return None, None
    return best_sec, best_mkt


def freeze_pre_match_cs(match_key):
    """冻结未开赛(scheduled)比赛的赛前CS盘口。

    铁律: 只采未开赛比赛。已开赛的绝不调此函数 (采集器只对 scheduled 调用)。
    返回 True 表示已冻结/更新, False 表示跳过(非scheduled或无盘口)。
    """
    with conn() as c:
        row = c.execute(
            "SELECT home,away,league,kickoff,status FROM matches WHERE match_key=?",
            (match_key,)).fetchone()
    if row is None or row[4] != 'scheduled':
        return False
    home, away, league, kickoff = row[0], row[1], row[2], row[3]
    sec, mkt = _pre_market_batch(match_key, kickoff=None)
    if mkt is None:
        return False
    now = time.time()
    with conn() as c:
        c.execute("""INSERT INTO pre_match_cs(match_key,home,away,league,kickoff,frozen_at,n_scores,odds_json,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(match_key) DO UPDATE SET
                       home=excluded.home, away=excluded.away, league=excluded.league,
                       kickoff=excluded.kickoff, frozen_at=excluded.frozen_at,
                       n_scores=excluded.n_scores, odds_json=excluded.odds_json""",
                  (match_key, home, away, league, kickoff, now, len(mkt),
                   json.dumps(mkt, ensure_ascii=False), now))
    return True


def verify_cs(match_key, source='live'):
    """对已完成(finished)且有比分的比赛, 取赛前CS盘口, 按实际比分验证并归档。

    返回 dict(含 hit/fav_hit) 或 None(无赛前盘口/无赛果)。
    """
    with conn() as c:
        row = c.execute(
            "SELECT home,away,league,kickoff,score_home,score_away,status FROM matches WHERE match_key=?",
            (match_key,)).fetchone()
    if row is None:
        return None
    home, away, league, kickoff, sh, sa, status = row
    if status != 'finished' or sh is None or sa is None:
        return None
    sec, mkt = _pre_market_batch(match_key, kickoff=kickoff)
    if mkt is None:
        # 赛前盘口整组无效(全为噪声赔率) → 清除历史可能残留的脏行, 不留伪造数据
        with conn() as c:
            c.execute("DELETE FROM cs_verification WHERE match_key=?", (match_key,))
        return None
    actual = f"{sh}:{sa}"
    actual_odds = mkt.get(actual)
    fav = min(mkt.items(), key=lambda kv: kv[1])          # 最被看好(最低赔)比分
    margin = sum(1.0 / o for o in mkt.values() if o and o > 0)  # 庄家margin(>1抽水)
    hit = 1 if actual_odds is not None else 0
    fav_hit = 1 if fav[0] == actual else 0
    actual_implied = round(1.0 / actual_odds, 4) if actual_odds else None
    now = time.time()
    with conn() as c:
        c.execute("""INSERT INTO cs_verification(
            match_key,home,away,league,kickoff,actual_score,actual_home,actual_away,
            pre_odds_json,actual_odds,actual_implied,favorite_score,favorite_odds,
            fav_hit,hit,margin,source,verified_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_key) DO UPDATE SET
              actual_score=excluded.actual_score, actual_home=excluded.actual_home,
              actual_away=excluded.actual_away, pre_odds_json=excluded.pre_odds_json,
              actual_odds=excluded.actual_odds, actual_implied=excluded.actual_implied,
              favorite_score=excluded.favorite_score, favorite_odds=excluded.favorite_odds,
              fav_hit=excluded.fav_hit, hit=excluded.hit, margin=excluded.margin,
              verified_at=excluded.verified_at""",
            (match_key, home, away, league, kickoff, actual, sh, sa,
             json.dumps(mkt, ensure_ascii=False), actual_odds, actual_implied,
             fav[0], fav[1], fav_hit, hit, round(margin, 4), source, now))
    return dict(match_key=match_key, actual=actual, actual_odds=actual_odds,
                hit=hit, fav_hit=fav_hit, n_scores=len(mkt), margin=round(margin, 4))


def query_pre_match_cs(limit=200):
    """列出未开赛(scheduled)比赛及其已冻结的赛前CS盘口。"""
    with conn() as c:
        rows = c.execute(
            """SELECT p.match_key,p.home,p.away,p.league,p.kickoff,p.frozen_at,p.n_scores,p.odds_json
               FROM pre_match_cs p
               JOIN matches m ON m.match_key=p.match_key
               WHERE m.status='scheduled'
               ORDER BY p.kickoff ASC LIMIT ?""", (limit,)).fetchall()
        return [dict(match_key=r[0], home=r[1], away=r[2], league=r[3], kickoff=r[4],
                     frozen_at=r[5], n_scores=r[6],
                     odds=json.loads(r[7]) if r[7] else {}) for r in rows]


def query_cs_verification(league="", date_from="", date_to="", score="",
                          hit=None, limit=200):
    """历史赛果验证查询 (类似31K): 按联赛/日期/比分/命中筛选。

    hit=1 仅看实际比分在赛前盘口中的比赛; hit=0 看漏掉的(实际比分未被开出)。
    """
    clauses, params = [], []
    if league:
        clauses.append("league LIKE ?"); params.append(f"%{league}%")
    if date_from:
        clauses.append("kickoff >= ?"); params.append(date_from)
    if date_to:
        clauses.append("kickoff <= ?"); params.append(date_to)
    if score:
        clauses.append("actual_score = ?"); params.append(score)
    if hit is not None:
        clauses.append("hit = ?"); params.append(hit)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with conn() as c:
        rows = c.execute(
            f"""SELECT match_key,home,away,league,kickoff,actual_score,actual_odds,
                       actual_implied,favorite_score,favorite_odds,fav_hit,hit,margin,source
                FROM cs_verification{where} ORDER BY kickoff DESC LIMIT ?""",
            params + [limit]).fetchall()
    cols = ["match_key","home","away","league","kickoff","actual_score","actual_odds",
            "actual_implied","favorite_score","favorite_odds","fav_hit","hit","margin","source"]
    return [dict(zip(cols, r)) for r in rows]
