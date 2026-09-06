# -*- coding: utf-8 -*-
"""
GQ 今日比赛时间轴客户端 (乐鱼体育 GQ 纯 HTTP API)
=================================================

独立客户端模块, 供 bridge_service 调用. 不依赖 gq/auto_collector.py(采集器),
也不依赖 requests/playwright 等第三方库, 纯 stdlib (urllib/gzip/base64/json/sqlite3).

职责:
  1. 以 urllib 调 GQ 两个端点 (比赛列表 / 单场详情), 解码 gzip+base64 响应.
  2. 聚合"今日"(GMT+8) 全部比赛, 按开赛时间升序, 附比分/状态/分钟/实时赔率.
  3. 实时赔率直接从 data/events.db (odds_snapshots 表) 读取, 不 import gq.db 包
     (避免 gq 包路径问题), 自己实现 get_latest_odds.

⚠️ 本机 DNS 坑: 本机 DNS 优先返回 IPv6 但路由不通, Python urllib 默认走 IPv6
会卡死 ~21s. 故在模块顶部强制 IPv4 (import 时执行一次).
"""

# ── 0) 强制 IPv4 (必须在任何 urllib 调用之前, 模块 import 时执行一次) ──
import socket
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """猴子补丁: 把默认地址族强制为 AF_INET(IPv4), 绕开本机 IPv6 路由不通的坑."""
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

# ── 1) 标准库 ──
import os
import sys
import re
import json
import time
import gzip
import base64
import sqlite3
import logging
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("gq_timeline")

# ── 2) 常量与鉴权 ──
CUID = "526002076777845380"                       # 固定设备/用户标识
HOST = "https://api.wnbtmel.com"                  # GQ API 域名
LIST_PATH = "/yewu11/v2/w/structureTournamentMatchesPB"    # 比赛列表端点
DETAIL_PATH = "/yewu11/v1/w/getMatchBaseInfoByOddsPB"      # 单场详情端点
EU_ID = "3020101"                                 # 固定 euid
REQUEST_TIMEOUT = 15                              # 单请求超时(秒), 避免无限卡死
SLEEP_BETWEEN = 0.08                              # 场间限流(秒)

# requestid (会话 token): 优先读环境变量, 否则用 fallback; 日志只打前 8 位
_FALLBACK_REQUEST_ID = "22f9755cba6b14eac450b4d2e537072607fac7a3"
REQUEST_ID = os.getenv("GQ_REQUEST_ID", _FALLBACK_REQUEST_ID)


def _build_headers() -> dict:
    """构造两个鉴权 header + 常规 header.

    - checkid: pc-{uuid}-{CUID}-{毫秒时间戳}
    - requestid: 会话 token (来自环境变量或 fallback)
    """
    checkid = f"pc-{uuid.uuid4().hex}-{CUID}-{int(time.time() * 1000)}"
    return {
        "checkid": checkid,
        "requestid": REQUEST_ID,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ════════════════════════════════════════════════════════════════════════
# GQTimelineClient —— 封装 HTTP 客户端 + 解码
# ════════════════════════════════════════════════════════════════════════
class GQTimelineClient:
    """乐鱼体育 GQ 客户端: 强制 IPv4 + 鉴权 header, 两个端点."""

    def __init__(self, cuid: str = CUID, request_id: str = REQUEST_ID):
        self.cuid = cuid
        self.request_id = request_id
        # 仅打印 token 前 8 位, 绝不打印完整 token
        logger.debug("GQTimelineClient 初始化, requestid=%s...", self.request_id[:8])

    # ---- 底层 POST ----
    def _post(self, path: str, body: dict) -> dict:
        """POST JSON, 返回解析后的外层 json (含 data 字段)."""
        import urllib.request
        url = HOST + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in _build_headers().items():
            req.add_header(k, v)
        # timeout 防止 IPv4 也异常时无限挂起
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))

    # ---- 解码 gzip+base64 ----
    @staticmethod
    def _decode(payload) -> dict:
        """GQ 响应 data 字段是 gzip+base64 字符串, 解成 dict.

        防御: token 过期/无效时 data=null → 返空底座而非崩.
        """
        if not payload:
            return {"livedata": [], "nolivedata": []}
        return json.loads(gzip.decompress(base64.b64decode(payload)).decode("utf-8"))

    # ---- 端点 1: 比赛列表 ----
    def list_raw(self) -> dict:
        """调列表端点, 返回解码后的 dict (含 livedata / nolivedata 两个 list).

        可能抛异常 (网络/解码), 由调用方(路由) 捕获后转成 gq_api_unreachable.
        """
        body = {
            "cuid": self.cuid,
            "sort": 1,
            "tid": "",
            "apiType": 1,
            "orpt": 0,
            "euid": EU_ID,
        }
        j = self._post(LIST_PATH, body)
        return self._decode(j["data"])

    # ---- 端点 2: 单场详情 ----
    def match_detail(self, mid) -> dict:
        """调详情端点, 返回解码后 dict 里的首条比赛数据.

        返回 dict 含: mhn(主队) / man(客队) / tn(联赛) / mid / mgt(开赛ms)
                      / msc(list 比分时间线) / mlet(str 进行中标记).
        若结构异常或取不到, 返回 None.
        """
        body = {
            "cuid": self.cuid,
            "cos": 0,
            "orpt": 0,
            "euid": EU_ID,
            "mid": str(mid),
            "mcid": 0,
            "newUser": 0,
        }
        j = self._post(DETAIL_PATH, body)
        # 部分比赛(尤其未开赛/已结束)该端点返回 data=null -> 直接视为无详情
        data_field = j.get("data") if isinstance(j, dict) else None
        if not data_field:
            return None
        try:
            decoded = self._decode(data_field)
        except Exception as e:
            logger.debug("GQ 详情解码失败 mid=%s: %s", mid, e)
            return None
        # 解码后是 dict, 其 "data" 键为比赛列表, 取第一项
        if isinstance(decoded, dict):
            dl = decoded.get("data")
        elif isinstance(decoded, list):
            dl = decoded
        else:
            dl = None
        if isinstance(dl, list) and dl:
            return dl[0] if isinstance(dl[0], dict) else None
        return None


# ════════════════════════════════════════════════════════════════════════
# 解析工具 (模块级, 便于单测/复用)
# ════════════════════════════════════════════════════════════════════════
def parse_score(msc_list) -> "tuple | None":
    """从 msc(比分时间线 list) 取末项解析当前比分.

    msc 形如 ['S0|0:0','S1|0:0','S5|0:1','S6|2:1']; 末项即当前比分.
    返回 (home, away) 整数元组; 解析失败返回 None.
    """
    if not msc_list:
        return None
    try:
        last = msc_list[-1]
        score_part = str(last).split("|")[1]
        home_s, away_s = score_part.split(":")
        return (int(home_s), int(away_s))
    except Exception:
        return None


def parse_status(mlet, kickoff_ts: float, now_ts: float) -> str:
    """判断比赛状态: scheduled(待开赛) / live(进行中) / finished(已结束).

    规则:
      - mlet 为空字符串           -> scheduled
      - mlet 匹配 r"(\\d+):\\d+" 且分钟>=90, 或含 FT/完 -> finished
      - 否则                      -> live
    兜底: 若 now_ts-kickoff_ts > 2.5h 且状态非 live(mlet空或异常) -> finished
          (防止 obscure 联赛 mlet 不更新导致误标为 scheduled)
    """
    if mlet is None:
        mlet = ""
    mlet = str(mlet).strip()

    if mlet == "":
        status = "scheduled"
    else:
        m = re.match(r"(\d+):\d+", mlet)
        if (m and int(m.group(1)) >= 90) or ("FT" in mlet) or ("完" in mlet):
            status = "finished"
        else:
            status = "live"

    # 兜底: 开赛超 2.5h 但状态非 live -> 判已结束
    if status != "live" and (now_ts - kickoff_ts) > 2.5 * 3600:
        status = "finished"
    return status


def parse_minute(mlet) -> int:
    """从 mlet(如 '45:00') 解析整数分钟, 解析失败返回 0."""
    if not mlet:
        return 0
    m = re.match(r"(\d+):\d+", str(mlet))
    return int(m.group(1)) if m else 0


# ════════════════════════════════════════════════════════════════════════
# 实时赔率: 直接读 data/events.db (自实现, 不 import gq.db)
# ════════════════════════════════════════════════════════════════════════
def _gq_db_path() -> str:
    """定位 data/events.db: pipeline/gq_timeline.py -> d:/Architecture/data/events.db."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "events.db"
    )


def get_latest_odds(match_key: str) -> dict:
    """取某场比赛所有市场最新赔率 (与 gq.db.get_latest_odds 完全一致的返回结构).

    返回: {f"{market}/{selection}": {market, selection, odds, line,
                                       captured_at, score_at, minute_at}, ...}
    match_key 格式: "{home} vs {away}" (与 auto_collector 写入格式一致).
    库不存在或查询异常时返回 {}.
    """
    db = _gq_db_path()
    if not os.path.exists(db):
        return {}
    try:
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """SELECT market, selection, odds, line, captured_at, score_at, minute_at
               FROM odds_snapshots s1
               WHERE match_key=? AND captured_at = (
                   SELECT MAX(captured_at) FROM odds_snapshots s2
                   WHERE s2.match_key=s1.match_key
                     AND s2.market=s1.market
                     AND s2.selection=s1.selection
               )""",
            (match_key,),
        ).fetchall()
        c.close()
        return {f"{r['market']}/{r['selection']}": dict(r) for r in rows}
    except Exception as e:
        logger.warning("get_latest_odds 失败 match_key=%s: %s", match_key, e)
        return {}


# ════════════════════════════════════════════════════════════════════════
# 聚合: 今日比赛时间轴
# ════════════════════════════════════════════════════════════════════════
def _today_bounds_gmt8():
    """返回 (date_str, start_ts秒, end_ts秒) 基于 GMT+8 的"今日"边界."""
    tz8 = timezone(timedelta(hours=8))
    now8 = datetime.now(tz8)
    start = now8.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.strftime("%Y-%m-%d"), start.timestamp(), end.timestamp()


# ── 时间轴结果缓存 (模块级, 避免前端轮询重复打列表接口) ──
_CACHE = {"ts": 0.0, "data": None}
_CACHE_TTL = 120.0


def _build_db_index() -> dict:
    """读 data/events.db matches 表 (WHERE mid IS NOT NULL) → {mid: {...}} 内存索引.

    时间轴底座来自列表接口 (0.3s), 已采到的队名/比分/状态/赔率来自 events.db,
    按 mid 关联. ORDER BY 让同 mid 的"有队名"行排在"MID占位"行之后,
    建 dict 时后者被前者覆盖, 避免未开赛占位覆盖已开赛真实记录.
    """
    db = _gq_db_path()
    if not os.path.exists(db):
        return {}
    try:
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        # 防御: 确保 mid 列存在 (采集器 init_db 会建, 时间轴独立运行也兜底)
        try:
            c.execute("ALTER TABLE matches ADD COLUMN mid TEXT")
        except Exception:
            pass
        rows = c.execute(
            """SELECT mid, home, away, league, status, score_home, score_away,
                      minute, kickoff
               FROM matches
               WHERE mid IS NOT NULL
               ORDER BY CASE WHEN home IS NULL OR home = '' THEN 0 ELSE 1 END,
                        last_seen DESC"""
        ).fetchall()
        c.close()
        idx = {}
        for r in rows:
            idx[r["mid"]] = {
                "home": r["home"] or "",
                "away": r["away"] or "",
                "league": r["league"] or "",
                "status": r["status"] or "scheduled",
                "score_home": r["score_home"],
                "score_away": r["score_away"],
                "minute": r["minute"] or 0,
            }
        return idx
    except Exception as e:
        logger.warning("_build_db_index 失败: %s", e)
        return {}


def _timeline_from_gq_db(date_str: str, start_ts: float, end_ts: float, tz8, now_ts: float) -> list:
    """live API token 失效时的时间轴 DB 回退 — 直接查 events.db matches 表.

    场景: bridge 进程 GQ_REQUEST_ID 未设/过期, GQ live list_raw 返回 data=null.
    此前 `_decode(None)` 崩 → 整个时间轴 0 场, 前端"共 0 场".
    修复后返 0 条 → 本回退接管, 查 events.db 今日比赛凑齐时间轴.

    返回与 live 路径同构的 out dict 列表; mid 缺则用 match_key 兜底.
    """
    db = _gq_db_path()
    if not os.path.exists(db):
        return []
    try:
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row
        rows = c.execute(
            """SELECT match_key, home, away, league, kickoff, status,
                      score_home, score_away, minute, mid
               FROM matches
               WHERE kickoff LIKE ? AND home != '' AND away != ''
               ORDER BY kickoff ASC""",
            (f"{date_str}%",),
        ).fetchall()
        c.close()
    except Exception as e:
        logger.warning("_timeline_from_gq_db 查询失败: %s", e)
        return []

    out = []
    for r in rows:
        kickoff_str = r["kickoff"] or ""
        kickoff_ts = 0.0
        try:
            kickoff_ts = (datetime.strptime(kickoff_str, "%Y-%m-%d %H:%M")
                          .replace(tzinfo=tz8).timestamp())
        except Exception:
            continue  # 解析失败丢弃
        if not (start_ts <= kickoff_ts < end_ts):
            continue
        mid = r["mid"] or r["match_key"]
        home = r["home"]; away = r["away"]
        score = ([r["score_home"], r["score_away"]]
                 if r["score_home"] is not None else None)
        odds = get_latest_odds(f"{home} vs {away}")
        out.append({
            "mid": mid,
            "home": home,
            "away": away,
            "league": r["league"] or "",
            "kickoff_ts": kickoff_ts,
            "kickoff_str": datetime.fromtimestamp(kickoff_ts, tz8).strftime("%H:%M"),
            "status": r["status"] or "scheduled",
            "score": score,
            "minute": r["minute"] or 0,
            "odds": odds,
            "_source": "db_fallback",
        })
    return out


def get_today_timeline(limit: int = None) -> dict:
    """聚合今日(GMT+8)全部比赛时间轴 — 架构A: 读 events.db + 快列表底座.

    步骤:
      1. list_raw() 拿全量底座 (0.3s, 不限流): mid/联赛/kickoff
      2. 展开 mids, 用 mgt 过滤今日(GMT+8 00:00~次日00:00), 按 mgt 升序
      3. 直接 sqlite3 连 events.db, 按 mid 取已采到的队名/比分/状态/赔率
      4. 无 db 记录的场: 状态按 kickoff 推 (未到→scheduled, 已过→live-采集中)
      5. 赔率仅对有队名的场调用 get_latest_odds (MID占位场 odds={})
      6. 模块级缓存 TTL=120s, 前端轮询不重复打列表接口

    若 list_raw() 整体失败(网络/解码) 会抛异常 —— 由路由层捕获转成
    {"error":"gq_api_unreachable"} 而非 500.
    """
    # 缓存命中: 直接返回上一次快照 (瞬时, 零 API 压力)
    if _CACHE["data"] is not None and (time.time() - _CACHE["ts"]) < _CACHE_TTL:
        return _CACHE["data"]

    client = GQTimelineClient()
    date_str, start_ts, end_ts = _today_bounds_gmt8()

    # 1) 列表底座 (0.3s, 不限流)
    decoded = client.list_raw()

    # 2) 展开 mids + 收集基础信息
    raw_items = []
    for key in ("livedata", "nolivedata"):
        raw_items.extend(decoded.get(key) or [])
    base = []
    for it in raw_items:
        mgt = it.get("mgt")
        if mgt is None:
            continue
        try:
            mgt = int(mgt)
        except Exception:
            continue
        mids = str(it.get("mids", ""))
        for mid in mids.split(","):
            mid = mid.strip()
            if not mid:
                continue
            base.append({"mid": mid, "tn": it.get("tn", ""), "mgt": mgt})

    # 2b) 过滤"今日" (mgt 为毫秒) + 按开赛时间升序
    today = [m for m in base if start_ts * 1000 <= m["mgt"] < end_ts * 1000]
    today.sort(key=lambda x: x["mgt"])

    # 3) 读 events.db 已采信息 (按 mid 关联)
    db_idx = _build_db_index()
    tz8 = timezone(timedelta(hours=8))
    now_ts = time.time()
    now8 = datetime.now(tz8)
    out = []
    for m in today:
        mid = m["mid"]
        kickoff_ts = m["mgt"] / 1000.0
        kickoff_str = datetime.fromtimestamp(kickoff_ts, tz8).strftime("%H:%M")
        rec = db_idx.get(mid)
        if rec and rec["home"]:
            # 已采到: 队名/比分/状态/分钟来自库
            home = rec["home"]
            away = rec["away"]
            league = rec["league"] or m["tn"]
            status = rec["status"]
            minute = rec["minute"]
            score = [rec["score_home"], rec["score_away"]] if rec["score_home"] is not None else None
            odds = get_latest_odds(f"{home} vs {away}")
        else:
            # 未采到 (MID占位或无记录): 状态按 kickoff 推
            home = ""
            away = ""
            league = m["tn"]
            status = "scheduled" if kickoff_ts > now_ts else "live"
            minute = 0
            score = None
            odds = {}

        out.append({
            "mid": mid,
            "home": home,
            "away": away,
            "league": league,
            "kickoff_ts": kickoff_ts,
            "kickoff_str": kickoff_str,
            "status": status,
            "score": score,
            "minute": minute,
            "odds": odds,
        })

    # 1c) live API 出 0 条 → events.db 回退 (token 过期/无效时保证时间轴有真比赛)
    if not out:
        db_out = _timeline_from_gq_db(date_str, start_ts, end_ts, tz8, now_ts)
        if db_out:
            out = db_out
            logger.info("时间轴 live API 空, events.db 回退 %d 场", len(out))

    # 4) 可选截断
    if limit is not None and limit > 0:
        out = out[:limit]

    result = {
        "date": date_str,
        "tz": "GMT+8",
        "count": len(out),
        "cached_at": now8.strftime("%Y-%m-%d %H:%M:%S"),
        "matches": out,
    }
    # 写缓存
    _CACHE["data"] = result
    _CACHE["ts"] = time.time()
    return result


def get_match_detail_api(mid) -> dict:
    """单场完整信息 (供按需端点 /api/timeline/match/{mid}).

    架构A: 先查 events.db by mid (瞬时), 命中直接返回;
    未命中(或库无该 mid)再按需调 match_detail (单场, 慢但按需, 不进时间轴批量).
    """
    mid = str(mid)

    # 1) 查 events.db (快)
    db = _gq_db_path()
    rec = None
    if os.path.exists(db):
        try:
            c = sqlite3.connect(db)
            c.row_factory = sqlite3.Row
            try:
                c.execute("ALTER TABLE matches ADD COLUMN mid TEXT")
            except Exception:
                pass
            r = c.execute(
                """SELECT mid, home, away, league, status, score_home,
                          score_away, minute, kickoff
                   FROM matches WHERE mid=? LIMIT 1""",
                (mid,),
            ).fetchone()
            c.close()
            if r and (r["home"] or r["mid"]):
                rec = r
        except Exception as e:
            logger.warning("get_match_detail_api 查库失败 mid=%s: %s", mid, e)

    if rec:
        tz8 = timezone(timedelta(hours=8))
        kickoff_ts = 0.0
        if rec["kickoff"]:
            try:
                kickoff_ts = (datetime.strptime(rec["kickoff"], "%Y-%m-%d %H:%M")
                              .replace(tzinfo=tz8).timestamp())
            except Exception:
                kickoff_ts = 0.0
        home = rec["home"] or ""
        away = rec["away"] or ""
        return {
            "mid": mid,
            "found": True,
            "source": "db",
            "home": home,
            "away": away,
            "league": rec["league"] or "",
            "kickoff_ts": kickoff_ts,
            "kickoff_str": rec["kickoff"] or "",
            "status": rec["status"] or "scheduled",
            "score": ([rec["score_home"], rec["score_away"]]
                      if rec["score_home"] is not None else None),
            "minute": rec["minute"] or 0,
            "odds": get_latest_odds(f"{home} vs {away}") if home else {},
        }

    # 2) 兜底: 单场详情 (慢, 按需)
    client = GQTimelineClient()
    try:
        d = client.match_detail(mid)
    except Exception as e:
        logger.warning("GQ 单场详情失败 mid=%s: %s", mid, e)
        d = None

    if not d:
        return {"mid": mid, "found": False, "error": "not_found"}

    home = d.get("mhn", "")
    away = d.get("man", "")
    league = d.get("tn", "")
    mgt = d.get("mgt")
    try:
        mgt = int(mgt) if mgt is not None else 0
    except Exception:
        mgt = 0
    kickoff_ts = mgt / 1000.0 if mgt else 0.0
    msc = d.get("msc") or []
    mlet = d.get("mlet", "")
    tz8 = timezone(timedelta(hours=8))
    kickoff_str = (datetime.fromtimestamp(kickoff_ts, tz8).strftime("%H:%M")
                   if kickoff_ts else "")

    return {
        "mid": mid,
        "found": True,
        "source": "api",
        "home": home,
        "away": away,
        "league": league,
        "kickoff_ts": kickoff_ts,
        "kickoff_str": kickoff_str,
        "status": parse_status(mlet, kickoff_ts, time.time()),
        "score": parse_score(msc),
        "minute": parse_minute(mlet),
        "odds": get_latest_odds(f"{home} vs {away}"),
    }
