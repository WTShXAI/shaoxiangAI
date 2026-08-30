#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gq/ws_collector.py — 乐鱼 WS 实时盘口采集器 (推送流模式)

与 gq/auto_collector.py (60s HTTP 轮询) 互补: 本采集器接管乐鱼 H5 真实浏览器会话,
拦截其原生 WebSocket 推送帧, 解码 gzip 后实时写入 data/events.db (完整赛事库:
matches 主表 + odds 全市场盘口 + results 总进球 + h2h + content + interface_doc)。
events.db 是融合全部维度的唯一赛事库, events.db 仅留作历史归档。

实测性质 (2026-08-27 抓包验证):
- WS 域名动态轮换 (api-umc.*.com / api.*.com), 不能硬编码; 必须接管 H5 会话让浏览器自动建连。
- WS 帧只带 mid (数字比赛ID), 不带队名/联赛 → 配合 HTTP 比赛列表/结构接口做 mid→match_key 映射
  (match_key = "主 vs 客", 与 auto_collector 一致)。
- 鉴权依赖 H5 会话登录态 (token 过期 → 走 gq-token-rotate 换 gq/.env 的 GQ_H5_URL)。
- C105 帧 cd 字段 = gzip+base64 (以 "H4sI" 开头), 解码后为 {mid, hls2{市场号:[行...]}};
  每行 hv=盘口线(如"2.5"/"-1"), ol=[{ot, ov(字符串整数,/100000=小数赔), ov2(水位), os(1可用)}]。
- 市场分类(基于选项形状, 实测 122 个市场号样本): 1X2 / AH(让球) / OU(大小球) / CORNER_OU(角球) /
  CS(波胆) / BTTS(双进) / OE(奇偶) / DNB(双重机会) / GOALS(进球数) 全部精准捕获; 组合盘及
  未识别市场归 WS_<号>(捕获不丢、不伪造标签)。命名与 auto_collector 一致, 两套数据互通。
- 赛事内容(赛事前瞻/伤病/赛果/情报)走独立 HTTP 端点(见 gq/content_collector.py,
  逆向自 SPA: sonMenuId 须为数字 tab 索引), 发现新比赛时(节流)后台写入 match_meta。
- 缩放恒为 ov/100000 (如 "209000"→2.09)。

用法:
    python gq/ws_collector.py --once            # 单次采集(约60s)后退出, 用于验证
    python gq/ws_collector.py --daemon -d 0     # 守护模式(无限), 日志 ws_daemon.log
    python gq/ws_collector.py --stats           # 打印当前 events.db 快照/变动统计

依赖: playwright (本仓已用隔离 venv 安装) + 系统 Edge (executable_path 指定, 不下载 chromium)。
"""

import argparse
import base64
import gzip
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gq.auto_collector as ac   # 复用: CUID / fetch_match_list / fetch_match_structure / _get_request_id
from core.safe_log import safe_print   # UTF-8 护栏, 中文/emoji 不抛 UnicodeEncodeError

# 完整赛事库 events.db: 采集器唯一写入目标(融合 全市场盘口/主表/总进球/H2H/内容/接口说明)
# 通过 gq.db 写入层落盘 (gq.db.DB_PATH 已指向 events.db, 表结构与 events.db 完全对齐)。
from gq.db import (conn, init_db, upsert_match, record_snapshot, stats as db_stats)

# 赛事内容采集(前瞻/伤病/赛果/情报)走独立 HTTP 端点, 与 WS 盘口流解耦。
# 容错导入: 即便 content_collector 异常也不影响实时盘口采集主链路。
try:
    import gq.content_collector as content_collector
except Exception:
    content_collector = None

# (goals_db 已退役: 总进球维度并入 events.db 的 odds/results, 见 gq/event_db.py)

# ───────────────────────── 配置 ─────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
_LOCK_PATH = os.path.join(HERE, ".ws_collector.lock")
_LOG_PATH = os.path.join(HERE, "ws_daemon.log")
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
ONCE_SEC = 60            # --once 模式运行时长
REGISTRY_SEC = 120       # mid→队名 映射全量刷新间隔
IDLE_RELOAD_SEC = 180     # 超过此秒数无 WS 帧 → 重载页面(重新建连/换 token)
HEARTBEAT_TICK = 1.0      # 主循环 sleep 秒

# 临场早盘补采 (2026-08-28): WS 不推未开赛场盘口, 用详情 HTTP 接口补采临场 2h 内早盘
PREMATCH_SCAN_SEC = 60        # 扫描周期
PREMATCH_WINDOW_SEC = 7200    # kickoff 距今 ≤2h 的场才补采(临场盘口波动最剧烈)
PREMATCH_MAX_PER_ROUND = 25   # 每轮上限(场间 0.4s), 克制防风控
PREMATCH_THROTTLE_SEC = 45    # 单场最小重采间隔(WS 推流场次不受影响, 这里只兜底)

# ── 市场家族分类 (基于选项形状, 逆向自 2026-08-27 抓包 122 个市场号样本) ──
# 原则: 主流玩法精准命名, 未知归 WS_<号>(不丢、不伪造标签)。
# 分类依据选项(ot)集合与盘口线(hv):
#   1X2       : ot == {1,X,2} 且无盘口线(胜平负)
#   AH        : ot == {1,2} 或 {1,2,X}; 带盘口线(hv)或平手盘(让球)
#   OU        : ot 含 Over & Under (大小球)
#   CORNER_OU : ot 为角球范围("0-1"/"2-3"/"7+" 等) (角球大小)
#   CS        : ot 含 ":" (比分, 波胆)
#   BTTS      : ot 含 Yes & No (双进/双方进球)
#   OE        : ot == {Odd,Even} (奇偶)
#   DNB       : ot 含 1X/12/X2 (双重机会)
#   GOALS     : ot 全为数字或数字+ (总进球数)
#   其他      : WS_<号> (组合盘如 1X2+OU / OU+BTTS 等, 捕获不丢, 留待分析)
def _classify_family(ots: list, hv) -> Optional[str]:
    s = set(o for o in ots if o)
    if not s:
        return None
    if s == {"1", "X", "2"}:
        return "1X2"
    if s in ({"1", "2"}, {"1", "2", "X"}, {"1", "2", "None"}):
        # 带盘口线 → 让球AH; 无盘口线且含X → 平手盘仍算AH, 否则1X2(理论无, 兜底)
        if hv not in (None, ""):
            return "AH"
        return "1X2" if "X" in s else "AH"
    # 2026-08-27 修复: obscure 联赛实时帧常只推 1/X/2 中的部分选项(如 {X,2}/{1,X}/{1}/{2}),
    # 此前未命中上方精确集 → 误归 WS_<市号> 丢失, 致滚球 1X2 三选项采集缺口。
    # 全部选项∈{1,X,2} 即视为 1X2(归一化 home/draw/away), 不再丢失。
    if s and s <= {"1", "X", "2"}:
        return "1X2"
    if "Over" in s and "Under" in s:
        return "OU"
    if any(o and (re.match(r"^\d+-\d+$", o) or re.match(r"^\d+\+$", o)) for o in s):
        return "CORNER_OU"
    if any(o and ":" in o for o in s):
        return "CS"
    if {"Yes", "No"} <= s:
        return "BTTS"
    if s == {"Odd", "Even"}:
        return "OE"
    if {"1X", "12", "X2"} & s:
        return "DNB"
    if all(re.match(r"^\d+(\+)?$", o) for o in s):
        return "GOALS"
    return None  # → WS_<号>

def _parse_line(hv):
    """盘口线解析: 四分之一盘 "4.5/5"→4.5, "-0.5/1"→-0.5, "118.5"→118.5, 空→None。"""
    if hv in (None, ""):
        return None
    s = str(hv).split("/")[0].strip()
    try:
        return float(s)
    except Exception:
        return None

def _norm_selection(family: str, ot) -> str:
    """选项归一化: 按家族把 1/X/2、Over/Under、Yes/No 等转成分析库通用 selection。"""
    ot = (ot or "").strip()
    if family == "1X2":
        return {"1": "home", "X": "draw", "2": "away"}.get(ot, ot)
    if family in ("OU", "CORNER_OU"):
        return {"Over": "over", "Under": "under"}.get(ot, ot.lower() if ot else ot)
    if family == "AH":
        return {"1": "home", "2": "away", "X": "draw"}.get(ot)  # None(无平局盘)→跳过
    if family == "BTTS":
        return {"Yes": "yes", "No": "no"}.get(ot, ot.lower())
    if family == "OE":
        return {"Odd": "odd", "Even": "even"}.get(ot, ot.lower())
    if family == "DNB":
        return {"1X": "1x", "12": "12", "X2": "x2"}.get(ot, ot.lower())
    # CS / GOALS / WS_<n>: 保留原始 ot (比分串如 "1:0/2:0/3:0" / 数字 / 组合串)
    return ot

# ───────────────────────── 日志 ─────────────────────────
def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    safe_print(line)
    try:
        with open(_LOG_PATH, "a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(line + "\n")
    except Exception:
        pass

def _mask(url: str) -> str:
    """日志脱敏: 掩盖 token/api/sessionId 真实值。"""
    if not url:
        return ""
    u = re.sub(r"(token=)[^&]+", r"\1XXX", url)
    u = re.sub(r"(api=)[^&]+", r"\1XXX", u)
    u = re.sub(r"(sessionId=)[^&]+", r"\1XXX", u)
    return u

# ───────────────────────── H5 URL 加载 ─────────────────────────
def _load_h5_url() -> str:
    """全量 H5 URL (含 token+api+sessionId), 优先环境变量 GQ_H5_URL, 否则 gq/.env。"""
    env = os.environ.get("GQ_H5_URL")
    if env:
        return env.strip()
    try:
        p = os.path.join(HERE, ".env")
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GQ_H5_URL="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""

def _build_fallback_url() -> str:
    """无 GQ_H5_URL 时, 用 GQ_REQUEST_ID 拼一个最简 H5 URL (缺 api 签名, WS 可能连不上, 仅作兜底)。"""
    tok = ac._get_request_id()
    return (f"https://user-pc-new.ztczzx.com/?token={tok}&gr=b&tm=1&lg=zh&mk=0&stm=blue"
            f"&skinColor=2&sessionId={ac.CUID}0000000000000000")

def _find_edge() -> Optional[str]:
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return None

# ───────────────────────── 解码 / 解析 ─────────────────────────
def _decode_cd(cd) -> Optional[dict]:
    """解码 C105 的 cd 字段 (gzip+base64, 以 H4sI 开头)。明文帧返回 None。"""
    if not isinstance(cd, str) or not cd.startswith("H4sI"):
        return None
    try:
        return json.loads(gzip.decompress(base64.b64decode(cd)).decode("utf-8"))
    except Exception:
        return None

def _build_market_name(family: str, line: Optional[float], mkt_num) -> str:
    """市场名: 核心带盘口线的市场(AH/OU/CORNER_OU)带线号, 其余用家族名; 未映射归 WS_<号>。"""
    if family == "WS":
        return f"WS_{mkt_num}"
    if family in ("OU", "CORNER_OU", "AH") and line is not None:
        return f"{family}_{line:.2f}"
    return family

def parse_c105(decoded: dict):
    """解析解码后的 C105 → (mid, [(market, selection, odds, line), ...])。

    全市场捕获: 每个市场号按选项形状分类(1X2/AH/OU/CORNER_OU/CS/BTTS/OE/DNB/GOALS),
    未识别的归 WS_<号>(捕获不丢)。帧内同名冲突(如同时存在两个无盘口线 1X2 市场)追加市号区分。
    """
    out = []
    mid = str(decoded.get("mid", ""))
    hls2 = decoded.get("hls2") or {}
    seen_names = set()
    for mkt_num, lines in hls2.items():
        if not isinstance(lines, list) or not lines:
            continue
        # 该市场号全部 ot 集合(用于分类) + 首个盘口线(用于 AH/OU 区分)
        all_ots = []
        for le in lines:
            for o in (le.get("ol") or []):
                if o.get("ot") is not None:
                    all_ots.append(str(o.get("ot")))
        hv0 = lines[0].get("hv")
        family = _classify_family(all_ots, hv0) or "WS"
        for line_elem in lines:
            line = _parse_line(line_elem.get("hv"))
            for ol in (line_elem.get("ol") or []):
                os_flag = ol.get("os")
                if os_flag not in (1, None):
                    continue  # 封盘(os=2)/锁(os=3)不写, 非可交易价
                ot = ol.get("ot")
                ov = ol.get("ov")
                if ov is None:
                    continue
                try:
                    odds = float(ov) / 100000.0
                except Exception:
                    continue
                if not (0.01 < odds < 10000):
                    continue
                sel = _norm_selection(family, ot)
                if not sel:   # AH 无平局盘的 "None" 选项等 → 跳过
                    continue
                mkt = _build_market_name(family, line, mkt_num)
                if mkt in seen_names:  # 帧内同名冲突
                    # 无盘口线家族(1X2/BTTS/OE/DNB)常被乐鱼拆分到多个市号 → 合并为同一 market(选项自然并集);
                    # 其余家族(如 AH 不同盘口)追加市号区分, 避免互相覆盖。
                    if line is None and family in ("1X2", "BTTS", "OE", "DNB"):
                        pass
                    else:
                        mkt = f"{mkt}_{mkt_num}"
                seen_names.add(mkt)
                out.append((mkt, sel, odds, line))
    return mid, out

def _kickoff_iso(mgt) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(mgt) / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return None

def _status_from_grp(grp: str) -> str:
    return "live" if grp == "livedata" else "scheduled"

# ───────────────────────── mid → 队名 映射表 ─────────────────────────
class Registry:
    """mid(数字) → match_key(主 vs 客) 映射。周期性全量刷新 + 按需补齐。"""

    def __init__(self, score_sink: Optional[dict] = None):
        self.map = {}                 # mid -> dict(home,away,league,status,match_key)
        self.lock = threading.Lock()
        self.score_sink = score_sink  # WSCollector.score 引用; structure 回写比分时同步

    def resolve(self, mid) -> Optional[dict]:
        with self.lock:
            return self.map.get(str(mid))

    def ensure(self, mid, grp: str = "livedata"):
        """确保某 mid 已登记(按需补齐, 单次 HTTP 结构请求)。"""
        mid = str(mid)
        with self.lock:
            if mid in self.map:
                return
        structs = ac.fetch_match_structure([mid])
        self._ingest(structs, {mid: grp})

    def refresh_all(self):
        """全量刷新: 拉比赛列表 → 拉结构 → 重建映射。"""
        items = ac.fetch_match_list()
        grp_by_mid = {it["mid"]: it.get("grp", "nolivedata") for it in items if it.get("mid")}
        if not grp_by_mid:
            _log(f"[REG] 比赛列表为空 (token 可能失效), 跳过刷新")
            return
        structs = ac.fetch_match_structure(list(grp_by_mid.keys()))
        self._ingest(structs, grp_by_mid)
        _log(f"[REG] 刷新完成: 列表 {len(grp_by_mid)} 场, 成功登记 {len(self.map)} 个 mid")

    def _ingest(self, structs, grp_by_mid: dict):
        if not structs:
            return
        with self.lock:
            for m in structs:
                mid = str(m.get("mid") or "")
                home = (m.get("mhn") or "").strip()
                away = (m.get("man") or "").strip()
                if not mid or not home or not away:
                    continue
                league = m.get("tnjc") or m.get("tn") or ""
                grp = grp_by_mid.get(mid, "livedata")
                status = _status_from_grp(grp)
                mk = f"{home} vs {away}"
                self.map[mid] = dict(home=home, away=away, league=league,
                                     status=status, match_key=mk)
                # 2026-08-27 修复: 乐鱼 WS 对 obscure 场不推 C103 比分帧，
                # 但 structure HTTP 端点的 msc 含 S0|(全场)/S1|(半场) 比分。
                # 作为 fallback: 仅当 DB 当前无比分且非人工锁定时回写，避免覆盖 C103 实时比分。
                sh, sa, ht_sh, ht_sa = ac._score_from_msc(m.get("msc"))
                try:
                    with conn() as c:
                        row = c.execute(
                            "SELECT score_home, score_away, ht_score_home, ht_score_away, is_override "
                            "FROM matches WHERE match_key=?", (mk,)).fetchone()
                        if row is None:
                            upsert_match(mk, home, away, league,
                                         kickoff=_kickoff_iso(m.get("mgt")), status=status,
                                         score_home=sh, score_away=sa,
                                         ht_score_home=ht_sh, ht_score_away=ht_sa)
                        elif not row[4]:
                            sets, vals = [], []
                            if sh is not None and row[0] is None:
                                sets.append("score_home=?"); vals.append(sh)
                            if sa is not None and row[1] is None:
                                sets.append("score_away=?"); vals.append(sa)
                            if ht_sh is not None and row[2] is None:
                                sets.append("ht_score_home=?"); vals.append(ht_sh)
                            if ht_sa is not None and row[3] is None:
                                sets.append("ht_score_away=?"); vals.append(ht_sa)
                            if sets:
                                sets.append("last_seen=?")
                                vals.append(time.time())
                                vals.append(mk)
                                c.execute(f"UPDATE matches SET {','.join(sets)} WHERE match_key=?", vals)
                        c.execute("UPDATE matches SET mid=? WHERE match_key=?", (mid, mk))
                    if sh is not None and self.score_sink is not None:
                        self.score_sink[mid] = f"{sh}-{sa}"
                except Exception as e:
                    _log(f"[REG][WARN] upsert_match {mk}: {e}")

# ───────────────────────── 主采集循环 ─────────────────────────
class WSCollector:
    def __init__(self):
        self.score = {}      # mid -> "sh-sa"
        self.minute = {}     # mid -> int
        self._kickoff_cache = {}   # mid -> kickoff_ts (占位分钟修正用, 2026-08-29)
        self.reg = Registry(score_sink=self.score)
        self.last_frame_ts = time.time()
        self.last_refresh_ts = 0.0
        self.stats = {"snaps": 0, "frames": 0, "unknown_mid": 0}
        self._reloading = False
        self._seen_markets = {}   # mkt_num -> 样本结构(调试期逆向映射用)
        self._content_fetched = {}  # mid -> 上次内容抓取时间戳(节流, 避免每帧重复请求)
        self._content_ttl = 600.0    # 内容(前瞻/伤病)变化慢, 每场每10分钟最多抓一次 (2026-08-28: 3600→600 扩覆盖)

    # ---- WS 帧处理 ----
    def handle_frame(self, text: str):
        try:
            msg = json.loads(text)
        except Exception:
            return
        cmd = msg.get("cmd") or msg.get("id")
        cd = msg.get("cd")
        self.stats["frames"] += 1
        self.stats[f"cmd_{cmd}"] = self.stats.get(f"cmd_{cmd}", 0) + 1

        if cmd == "C105" and isinstance(cd, str) and cd.startswith("H4sI"):
            self._on_c105(cd)
        elif cmd in ("C102", "C1021") and isinstance(cd, dict):
            self._on_event(cd)
        elif cmd == "C103" and isinstance(cd, dict):
            self._on_score(cd)
        # C110=变动通知(真实盘口随后经 C105 推), C3301=菜单, C0/CM0=心跳 → 忽略

    def _on_c105(self, cd: str):
        dec = _decode_cd(cd)
        if not dec:
            _log("[C105][WARN] gzip/base64 解码失败")
            return
        mkt_nums = list((dec.get("hls2") or {}).keys())
        # 调试期: 累积每个市场号的样本结构, 用于逆向真实市场映射
        for mn in mkt_nums:
            if mn not in self._seen_markets:
                line0 = (dec["hls2"][mn] or [{}])[0]
                ol0 = (line0.get("ol") or [{}])[0]
                self._seen_markets[mn] = {
                    "hv": line0.get("hv"),
                    "sample_opts": [
                        {"ot": o.get("ot"), "ov": o.get("ov"), "ov2": o.get("ov2"), "os": o.get("os")}
                        for o in (line0.get("ol") or [])[:4]
                    ],
                }
        mid, rows = parse_c105(dec)
        if not mid:
            _log(f"[C105][WARN] 无 mid (markets={mkt_nums})")
            return
        if not rows:
            # 2026-08-27 降噪: 全封盘(os=2/3, 无任何可交易价)属正常(封盘盘口不入库, IR: 仅 os==1 写入),
            # 不再刷屏警告; 仅当存在 os=1 可交易但被解析拒绝(真异常)时才警告。
            all_closed = True
            for mn in mkt_nums:
                for le in (dec.get("hls2") or {}).get(mn, []) or []:
                    for o in (le.get("ol") or []):
                        if o.get("os") == 1:
                            all_closed = False
                            break
                    if not all_closed:
                        break
                if not all_closed:
                    break
            if not all_closed:
                _log(f"[C105][WARN] mid={mid} 有盘口号 {mkt_nums} 但解析 0 行 (存在 os=1 却未产出)")
            return
        self.reg.ensure(mid)  # 按需补齐队名(已在 map 则无操作)
        info = self.reg.resolve(mid)
        if not info:
            self.stats["unknown_mid"] += 1
            return  # 结构接口暂未返回该 mid 的队名, 跳过本帧(下轮重试)
        mk = info["match_key"]
        score_at = self.score.get(mid, "")
        minute_at = self.minute.get(mid, 0)
        self.maybe_collect_content(mk, mid)   # 后台抓取赛事前瞻/伤病(节流)
        for (mkt, sel, odds, line) in rows:
            try:
                record_snapshot(mk, mkt, sel, odds, line=line,
                                score_at=score_at, minute_at=minute_at)
                self.stats["snaps"] += 1
            except Exception as e:
                _log(f"[SNAP][WARN] {mk} {mkt}/{sel}: {e}")

    def _on_event(self, cd: dict):
        mid = str(cd.get("mid", ""))
        mmp = cd.get("mmp")
        try:
            mmp_i = int(mmp)
        except Exception:
            mmp_i = 0
        # 2026-08-28 脏值防御: 乐鱼 mmp 偶发推非分钟值(实测 29768964 污染 odds_snapshots.
        # minute_at → 滚球查询 minute_at<=minute 全部失真)。合法比赛分钟 0~130(含 45+/90+),
        # 999=完场标记; 其余视为脏 feed, minute 归 0 不入库。
        # 2026-08-29 占位分钟修正 (全库 61.8% minute_at 污染根因):
        #   乐鱼 WS 的 mmp 整个上半场恒推 45、整个下半场恒推 90, 是**占位垃圾**。
        #   原代码直接采信 → odds_snapshots.minute_at 与 matches.minute 全卡在 45/90
        #   (实测 events.db 878 万条里 543 万条 = 61.8%)。
        #   检测 45/90 → 改用 kickoff + 墙钟推算真实比赛分钟 (与 auto_collector.
        #   _status_minute / analysis.live_goal_probe.resolve_true_minute 同口径);
        #   真值(如 38/71/93) 直接采信。
        _mn = mmp_i
        if mmp_i in (45, 90):
            _mn = self._true_minute_from_kickoff(mid, mmp_i)
        self.minute[mid] = _mn if 0 <= _mn <= 130 else 0
        info = self.reg.resolve(mid)
        if not info:
            return
        # 状态判定: 完场标记 999 优先; 占位值(45/90)用真实分钟判(避免墙钟才 60min
        # 就被 mmp=90 误判 finished); 真值维持原判据。
        if mmp == "999":
            st = "finished"
        elif mmp_i in (45, 90):
            st = "finished" if _mn >= 95 else ("live" if _mn > 0 else "scheduled")
        else:
            st = "finished" if mmp_i >= 90 else ("live" if mmp_i > 0 else "scheduled")
        try:
            with conn() as c:
                c.execute("UPDATE matches SET status=?, minute=?, last_seen=? "
                          "WHERE mid=? AND (is_override IS NULL OR is_override=0)",
                          (st, self.minute[mid], time.time(), mid))
        except Exception:
            pass

    def _true_minute_from_kickoff(self, mid, fallback):
        """mmp 占位值(45/90) → 用 kickoff + 墙钟推算真实比赛分钟。

        与 gq.auto_collector._elapsed_to_minute 同口径: 上半场=est, 中场=45,
        下半场=est-HT_BREAK_MIN。kickoff 不可得时返回 fallback(零回归)。
        """
        ts = self._kickoff_cache.get(mid)
        if ts is None:
            try:
                with conn() as c:
                    row = c.execute(
                        "SELECT kickoff FROM matches WHERE mid=? LIMIT 1", (mid,)).fetchone()
                if row and row[0]:
                    ts = ac._parse_kickoff(row[0]) or 0
                else:
                    ts = 0
            except Exception:
                ts = 0
            self._kickoff_cache[mid] = ts
        if not ts:
            return fallback
        est = (time.time() - ts) / 60.0
        if est <= 45:
            return int(est)
        if est <= 45 + ac.HT_BREAK_MIN:
            return 45
        return min(125, int(est - ac.HT_BREAK_MIN))

    def _on_score(self, cd: dict):
        mid = str(cd.get("mid", ""))
        msc = cd.get("msc") or []
        sh = sa = None
        ht_h = ht_a = None
        for entry in msc:
            if isinstance(entry, str):
                if entry.startswith("S0|"):   # 全场比分
                    try:
                        a, b = entry[3:].split(":")
                        sh, sa = int(a), int(b)
                    except Exception:
                        pass
                elif entry.startswith("S1|"):  # 半场比分
                    try:
                        a, b = entry[3:].split(":")
                        ht_h, ht_a = int(a), int(b)
                    except Exception:
                        pass
        # 2026-08-27 修复: 原 `if sh is None: return` 导致 C103 帧无 S0|(如只有半场比分/格式微变)时
        # 整帧丢弃 — score/last_seen 都不更新 → 前端"比分落后"。
        # 现在: ① 半场比分 ht_h/ht_a 解析后落库(原代码解析但未使用); ② 无条件刷新 last_seen(WS 活跃证明),
        # 仅当连全场+半场都无时才跳过.
        if sh is None and ht_h is None:
            return
        if sh is not None:
            self.score[mid] = f"{sh}-{sa}"
        info = self.reg.resolve(mid)
        if not info:
            return
        try:
            if sh is not None and ht_h is not None:
                with conn() as c:
                    c.execute("UPDATE matches SET score_home=?, score_away=?, ht_score_home=?, ht_score_away=?, last_seen=? "
                              "WHERE mid=? AND (is_override IS NULL OR is_override=0)",
                              (sh, sa, ht_h, ht_a, time.time(), mid))
            elif sh is not None:
                with conn() as c:
                    c.execute("UPDATE matches SET score_home=?, score_away=?, last_seen=? "
                              "WHERE mid=? AND (is_override IS NULL OR is_override=0)",
                              (sh, sa, time.time(), mid))
            elif ht_h is not None:
                with conn() as c:
                    c.execute("UPDATE matches SET ht_score_home=?, ht_score_away=?, last_seen=? "
                              "WHERE mid=? AND (is_override IS NULL OR is_override=0)",
                              (ht_h, ht_a, time.time(), mid))
        except Exception:
            pass

    # ---- 赛事内容(前瞻/伤病)后台抓取 ----
    def _content_worker(self, match_key: str, mid: str):
        """守护线程体: 抓取并写入 match_meta, 异常被吞(不影响实时盘口)。"""
        if content_collector is None:
            return
        try:
            ok = content_collector.collect_and_store(match_key, mid)
            if ok:
                _log(f"[CONTENT] 写入 match_meta: {match_key}")
        except Exception as e:
            _log(f"[CONTENT][WARN] {match_key}: {e}")

    def maybe_collect_content(self, match_key: str, mid: str):
        """节流触发内容抓取: 每场每 _content_ttl 秒最多一次, 后台线程执行不阻塞 WS 帧循环。"""
        if content_collector is None:
            return
        now = time.time()
        last = self._content_fetched.get(mid)
        if last and now - last < self._content_ttl:
            return
        self._content_fetched[mid] = now
        try:
            t = threading.Thread(target=self._content_worker,
                                args=(match_key, mid), daemon=True)
            t.start()
        except Exception as e:
            _log(f"[CONTENT][WARN] 线程启动失败 {match_key}: {e}")

    # ---- 临场早盘补采 (2026-08-28) ----
    # 主采集器切换为 WS 推送后, 乐鱼对未开赛场不推 C105 盘口帧, 而旧 HTTP 全量轮询
    # (auto_collector 主循环)已退役 → 临开赛 2h 内的场早盘(1X2/OU/AH/波胆)全缺,
    # 前端显示"盘口缺失"(实测 俄U19 5605230 有完整乐鱼盘口但库内 0 行)。
    # 此线程用详情 HTTP 接口补采: status=scheduled + kickoff≤2h + 单场 45s 节流 +
    # 每轮上限 25 场 + 场间 0.4s, 克制防风控。record_match_odds 内部 st 判定(毫秒 mgt)
    # 在未开赛时为 scheduled → "全场波胆仅 scheduled 采集"分支正常触发, 波胆随之入库。
    def _prematch_backfill_tick(self) -> int:
        now = time.time()
        # 2026-08-28 赛程表维护: live_flip 随 auto_collector 退役后无人翻转赛程状态,
        # 僵尸 live(开赛>4h)/超期 scheduled 堆积(实测 321+3 场)污染补采队列与前端列表。
        # 判据与 auto_collector._status_minute 一致: 开赛>3.5h 强制 finished; scheduled
        # 超期 2 天视作已完/取消。无比分的完场由 match_outcomes 归档链路独立处理。
        try:
            with conn() as c:
                c.execute("UPDATE matches SET status='finished' "
                          "WHERE status='live' AND kickoff IS NOT NULL AND kickoff != '' "
                          "AND kickoff < datetime('now', '-4 hours')")
                c.execute("UPDATE matches SET status='finished' "
                          "WHERE status='scheduled' AND kickoff IS NOT NULL AND kickoff != '' "
                          "AND kickoff < datetime('now', '-2 days')")
                c.execute("UPDATE odds_snapshots SET minute_at=0 WHERE minute_at > 130")
        except Exception as e:
            _log(f"[PRE][WARN] 赛程维护失败: {e}")
        # 2026-08-28 扩展: live 场也兜底。实测乐鱼 WS 对滚球场会静默断供(莫斯科斯巴达U19
        with conn() as c:
            rows = c.execute(
                "SELECT match_key, mid, kickoff, status, COALESCE(minute,0) FROM matches "
                "WHERE status IN ('scheduled','live') AND mid IS NOT NULL AND mid != '' "
                "AND (is_override IS NULL OR is_override=0)").fetchall()
        picked = []
        focus_keys = set()
        try:   # 前端焦点场 (registerFocus 覆盖写): 最高优先级, 绕过名额; 120s 内新鲜才认
            fp = os.path.join(HERE, "focus_matches.json")
            if time.time() - os.path.getmtime(fp) < 120:
                with open(fp, encoding="utf-8") as f:
                    focus_keys = set(json.load(f).get("match_keys") or [])
        except Exception:
            pass
        for mk, mid, ko_iso, st, minute in rows:
            try:
                ko = datetime.fromisoformat(ko_iso).timestamp()
            except Exception:
                ko = 0.0
            is_focus = mk in focus_keys
            if st == 'scheduled':
                dt = ko - now
                if -300.0 <= dt <= PREMATCH_WINDOW_SEC or is_focus:   # 焦点场不看窗口
                    picked.append((mk, str(mid), ko, -1 if is_focus else 1, dt))
            elif st == 'live':
                # live 真活过滤: minute 0~125 且 kickoff 距今 <3.5h (踢掉完场僵尸,
                # 否则 300 僵尸 live 稀释 25 名额 → 真断供场饿死, 实测 5605230)。
                # 焦点场豁免 — 用户正盯着它, 状态异常也补。
                alive = 0 < minute < 125 and ko > 0 and now - ko < 3.5 * 3600
                if alive or is_focus:
                    picked.append((mk, str(mid), ko, -1 if is_focus else 0, 0.0))
        if not picked:
            return 0
        mks = [p[0] for p in picked]
        qm = ",".join("?" * len(mks))
        with conn() as c:
            last = dict(c.execute(
                f"SELECT match_key, MAX(captured_at) FROM odds_snapshots "
                f"WHERE match_key IN ({qm}) GROUP BY match_key", mks).fetchall())
        # 最久未更新优先 (2026-08-28): live 断供场 last_ts 最老 → 排最前, 防止名额
        # 每轮反复覆盖同批场(SQL 返回序固定)而真正断供的场饿死。
        # 焦点场节流 30s(用户盯着), 普通 45s。
        picked = [(mk, mid, ko, prio, now - (last.get(mk) or 0.0))
                  for mk, mid, ko, prio, _ in picked
                  if now - (last.get(mk) or 0.0) >= (30.0 if prio == -1 else PREMATCH_THROTTLE_SEC)]
        picked.sort(key=lambda p: (p[3], -p[4]))   # 焦点(-1)恒最先; 其余断供最久先, live 先于 scheduled
        focus_pick = [p for p in picked if p[3] == -1][:12]
        rest = [p for p in picked if p[3] != -1][:PREMATCH_MAX_PER_ROUND]
        n = 0
        for mk, mid, ko, _p, _age in focus_pick + rest:
            try:
                dec = ac.fetch_match_odds(mid)
                if dec:
                    ac.record_match_odds(dec, {"mid": mid, "mgt": ko * 1000.0})
                    n += 1
            except Exception as e:
                _log(f"[PRE][WARN] {mk} 补采失败: {e}")
            time.sleep(0.4)
        return n

    def _prematch_backfill_worker(self):
        _log(f"[PRE] 盘口兜底补采线程启动 (live断供 + scheduled临场≤{PREMATCH_WINDOW_SEC//3600}h, "
             f"{PREMATCH_SCAN_SEC}s/轮, 上限{PREMATCH_MAX_PER_ROUND}/轮)")
        while True:
            try:
                n = self._prematch_backfill_tick()
                if n:
                    _log(f"[PRE] 早盘补采 {n} 场")
            except Exception as e:
                _log(f"[PRE][WARN] 补采轮失败: {e}")
            time.sleep(PREMATCH_SCAN_SEC)

    # ---- 浏览器驱动 ----
    def run(self, once: bool = False, duration_min: int = 0):
        from playwright.sync_api import sync_playwright
        edge = _find_edge()
        if not edge:
            _log("[FATAL] 未找到 Edge 可执行文件, 无法启动 WS 采集器")
            return
        h5 = _load_h5_url() or _build_fallback_url()
        _log(f"[INIT] H5={_mask(h5)}")
        _log(f"[INIT] Edge={edge}")

        end_ts = (time.time() + duration_min * 60.0) if duration_min > 0 else 0.0
        started_at = time.time()

        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=edge, headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(ignore_https_errors=True, locale="zh-CN")
            page = ctx.new_page()

            def on_ws(ws):
                _log(f"[WS] open {ws.url[:70]}")
                # Playwright Python: framereceived 回调直接收到 payload(str/bytes), 非 event 对象
                ws.on("framereceived", lambda data: self._safe_frame(data))
                ws.on("close", lambda: _log("[WS] closed"))

            page.on("websocket", on_ws)
            page.on("crash", lambda: _log("[WS][WARN] page crash"))

            # 首轮: 先建立 mid→队名 映射, 再加载 H5
            try:
                self.reg.refresh_all()
            except Exception as e:
                _log(f"[REG][WARN] 首轮刷新失败: {e}")

            # 临场早盘补采线程 (WS 不推未开赛场盘口的兜底, 见方法注释)
            try:
                threading.Thread(target=self._prematch_backfill_worker,
                                 daemon=True, name="prematch-backfill").start()
            except Exception as e:
                _log(f"[PRE][WARN] 补采线程启动失败: {e}")

            self._navigate(page, h5)

            # 主循环
            while True:
                now = time.time()
                # 周期性全量刷新映射
                if now - self.last_refresh_ts >= REGISTRY_SEC:
                    try:
                        self.reg.refresh_all()
                    except Exception as e:
                        _log(f"[REG][WARN] 刷新失败: {e}")
                    self.last_refresh_ts = now
                # 长时间无帧 → 重载页面(重新建连 / 换 token)
                if now - self.last_frame_ts >= IDLE_RELOAD_SEC:
                    _log(f"[WS][WARN] {IDLE_RELOAD_SEC}s 无推送, 重载页面重建连接")
                    self._navigate(page, h5)
                # 退出条件
                if once and (now - started_at >= ONCE_SEC):
                    break
                if end_ts and now >= end_ts:
                    break
                page.wait_for_timeout(int(HEARTBEAT_TICK * 1000))

            browser.close()
        _log(f"[DONE] 退出. 累计帧={self.stats['frames']} 快照={self.stats['snaps']} 未知mid={self.stats['unknown_mid']}")
        _log(f"[DONE] cmd分布={ {k:v for k,v in self.stats.items() if k.startswith('cmd_')} }")
        try:
            import json as _json
            with open(os.path.join(HERE, "_ws_markets.json"), "w", encoding="utf-8") as f:
                _json.dump(self._seen_markets, f, ensure_ascii=False, indent=2)
            _log(f"[DONE] 市场样本已存 _ws_markets.json ({len(self._seen_markets)} 个市场号)")
        except Exception as e:
            _log(f"[DONE][WARN] 市场样本写出失败: {e}")

    def _navigate(self, page, h5: str):
        # 2026-08-29: 每次导航**重新读一次** .env 的 GQ_H5_URL。
        #   原实现复用 run() 启动时读取的 h5 变量 → 换号(新 token/sessionId)后
        #   运行中的采集器永远用旧 URL, 只能靠重启才生效。现改为热读:
        #   gq/.env 一改, 下一次页面重载(或 IDLE_RELOAD_SEC 无帧触发)即自动 pickup。
        #   h5 参数退化为"读不到时的兜底值", 保证零回归。
        h5_live = _load_h5_url() or h5
        if h5_live != h5:
            _log(f"[NAV] 检测到 .env 中 GQ_H5_URL 已更新, 本次导航改用新 URL")
        self._reloading = True
        try:
            page.goto(h5_live, wait_until="domcontentloaded", timeout=30000)
            self.last_frame_ts = time.time()
            _log("[NAV] H5 已加载, 等待 WS 推送...")
        except Exception as e:
            _log(f"[NAV][WARN] 加载失败: {e}")
        self._reloading = False

    def _safe_frame(self, data):
        """WS 帧回调入口: 兜底异常, 单帧出错绝不杀监听循环。"""
        try:
            self.last_frame_ts = time.time()
            text = data if isinstance(data, str) else (data.decode("utf-8", "ignore") if isinstance(data, (bytes, bytearray)) else "")
            if text:
                self.handle_frame(text)
        except Exception as e:
            _log(f"[FRAME][WARN] 跳过异常帧: {e}")

# ───────────────────────── 单实例锁 (PID 文件, 跨平台可靠) ─────────────────────────
# Windows 下 msvcrt.locking 配合 open("w") 截断会破坏已持有进程的锁字节, 导致双实例写库.
# 改用 PID 文件: 原子 O_EXCL 创建抢锁; 若已存在则读其中 PID 校验存活, 存活则放弃, 已死则清残留后重试.
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False

def _acquire_singleton() -> bool:
    try:
        try:
            fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return True
        except FileExistsError:
            # 锁已存在 → 读 PID 校验
            try:
                with open(_LOCK_PATH, "r", encoding="utf-8") as f:
                    old = f.read().strip()
                old_pid = int(old) if old.isdigit() else -1
                if old_pid > 0 and _pid_alive(old_pid):
                    safe_print(f"[LOCK] 已有 ws_collector 实例运行 (PID={old_pid}), 退出")
                    return False
                # 残留锁(PID 已死) → 删除后重试一次
                try:
                    os.remove(_LOCK_PATH)
                except Exception:
                    pass
                fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(fd, "w") as f:
                    f.write(str(os.getpid()))
                return True
            except FileExistsError:
                # 极窄竞态: 删除与重建之间被第三者抢建 → 再校验一次
                try:
                    with open(_LOCK_PATH, "r", encoding="utf-8") as f:
                        old = f.read().strip()
                    old_pid = int(old) if old.isdigit() else -1
                    if old_pid > 0 and _pid_alive(old_pid):
                        return False
                except Exception:
                    pass
                return False
    except Exception as e:
        safe_print(f"[LOCK][ERR] {e}")
        return False

def _release_singleton():
    try:
        if os.path.exists(_LOCK_PATH):
            try:
                with open(_LOCK_PATH, "r", encoding="utf-8") as f:
                    cur = f.read().strip()
                if cur.isdigit() and int(cur) == os.getpid():
                    os.remove(_LOCK_PATH)
            except Exception:
                try:
                    os.remove(_LOCK_PATH)
                except Exception:
                    pass
    except Exception:
        pass

# ───────────────────────── main ─────────────────────────
def main():
    ap = argparse.ArgumentParser(description="乐鱼 WS 实时盘口采集器 (推送流)")
    ap.add_argument("--once", action="store_true", help="单次采集(约60s)后退出, 用于验证")
    ap.add_argument("-d", "--duration", type=int, default=0,
                    help="守护运行时长(分钟), 0=无限 (默认0)")
    ap.add_argument("--stats", action="store_true", help="打印 events.db 统计后退出")
    args = ap.parse_args()

    if args.stats:
        try:
            init_db()
            s = db_stats()
            _log(f"[STATS] {json.dumps(s, ensure_ascii=False)}")
        except Exception as e:
            _log(f"[STATS][ERR] {e}")
        return

    init_db()
    collector = WSCollector()

    if args.once:
        _log("[MODE] --once (约 %ds)" % ONCE_SEC)
        collector.run(once=True)
        _log(f"[STATS] 帧={collector.stats['frames']} 快照={collector.stats['snaps']} "
              f"未知mid={collector.stats['unknown_mid']}")
        return

    # 守护模式: 单实例锁
    if not _acquire_singleton():
        safe_print(f"[LOCK] 已有 ws_collector 实例运行 (.ws_collector.lock), 退出")
        return
    try:
        _log(f"[MODE] --daemon duration={args.duration}min (0=无限) | log={_LOG_PATH}")
        collector.run(once=False, duration_min=args.duration)
    finally:
        _release_singleton()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        safe_print("[EXIT] 用户中断")
    except Exception:
        safe_print("[FATAL] " + traceback.format_exc())
