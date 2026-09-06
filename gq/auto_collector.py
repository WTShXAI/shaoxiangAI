"""
GQ 全市场自动赔率采集器 — 纯 HTTP 版 v3.0 (现为「HTTP 辅助库 / 旧采集器」)
=========================================================================
【状态 2026-08-27】主采集器已切换为 gq/ws_collector.py (乐鱼 WS 实时推送流, 全市场+内容)。
本文件保留为：
  (1) HTTP 辅助库 —— ws_collector / content_collector 复用其 CUID / _build_headers /
      _api_post / fetch_match_list / fetch_match_structure 等能力；
  (2) 旧轮询采集器 —— 已由看门狗(start_collector.py + watchdog_collector.py)改指向
      ws_collector，不再以此文件作为活跃守护进程。
⚠️ 勿再以此文件单独起 --daemon 双写 odds_snapshots (会与 ws_collector 重复落库)。

1X2 + 让球(AH) + 大小球(OU) + 波胆(CS) (HTTP 轮询版, 仅作兜底/对照)

⚠️ 本版本完全基于逆向成功的 REST API，不依赖任何浏览器 / Playwright / Chromium。
   仅使用 Python 标准库 (urllib / gzip / base64 / json)，系统 Python3.12 可直接运行。

已逆向的 API 契约(直接调用，未改动):
  - Host: https://api.wnbtmel.com
  - 鉴权: 每个请求带自定义 header `checkid` / `requestid`，外加 UA + Accept，不使用 cookie。
  - 比赛列表: POST /yewu11/v2/w/structureTournamentMatchesPB
      响应 json["data"] 为 gzip+base64 字符串，解码后是含 livedata/nolivedata 的 dict。
  - 单场赔率: POST /yewu11/v1/w/getMatchBaseInfoByOddsPB
      响应 json["data"] 同样是 gzip+base64 字符串，解码后含 data[0] 比赛元信息 + playData 玩法市场。

数据库复用 gq/db.py (init_db / upsert_match / record_snapshot / stats / DB_PATH)，未做任何改动。

用法(仅作库/兜底, 主采集请用 ws_collector):
  python gq/auto_collector.py --once       # 单次采集
  python gq/auto_collector.py --daemon     # 守护模式(已不推荐, 看门狗改跑 ws_collector)
  python gq/auto_collector.py --stats      # 查看统计
  python gq/auto_collector.py -i 30 -d 60 # 守护 30s 一轮，运行 60 分钟

环境变量:
  GQ_REQUEST_ID  可覆盖默认会话 token (requestid)。日志只打印前 8 位。
"""
from __future__ import annotations

import argparse, base64, gzip, json, os, random, re, sys, threading, time, traceback, uuid, atexit
import contextlib
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from gq.db import (init_db, conn, upsert_match, record_snapshot, record_match_outcome,
                   stats as db_stats, DB_PATH, ensure_cs_tables,
                   freeze_pre_match_cs, verify_cs, auto_review_match,
                   normalize_cs_score)

# ── 事故④ 根治: 入口装 UTF-8 护栏 + 用 SafeLog/safe_print 替代裸 print,
#    含中文/emoji 的日志一律 backslashreplace, 绝不抛 UnicodeEncodeError ──
from core.safe_log import install_utf8, safe_print, SafeLog
from core.collector_step import CollectorRound, FunctionStep, CollectorContext, summarize

# 已见过的半场hpn标签集合 (首次遇到时记日志, 用于确认乐鱼半场市场的精确标签名)
_HALFTIME_HPN_SEEN: set = set()


def _call_with_timeout(fn, args=(), kwargs=None, timeout_sec: float = 5.0):
    """在独立守护线程中调用 fn, 超时返回 (None, True), 正常返回 (result, False).

    用于 fast 轮对可能因网络/DB锁而长时间阻塞的调用做硬超时保护,
    避免单场比赛拖死整个 3s 焦点轮.
    """
    if kwargs is None:
        kwargs = {}
    result = [None]
    exc = [None]

    def _target():
        try:
            result[0] = fn(*args, **kwargs)
        except Exception as e:
            exc[0] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        return None, True
    if exc[0] is not None:
        raise exc[0]
    return result[0], False

# ── 单实例锁: 确保全局只有一个采集器在写库 (防止监督进程/重复启动导致双写/双API) ──
# 用操作系统级文件锁 (Windows msvcrt.locking / Unix fcntl.flock), 原子且随进程退出自动释放,
# 完全不依赖 PID 存活探测 (Windows 上 os.kill(pid,0) 与 ctypes.OpenProcess 均不可靠).
_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".collector.lock")
_lock_fd = None

def _acquire_singleton() -> bool:
    """获取文件锁. 返回 True=获得锁可继续; False=已被其他实例持有, 应退出."""
    global _lock_fd
    try:
        _lock_fd = open(_LOCK_PATH, "w")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True
    except (OSError, IOError, ImportError):
        if _lock_fd is not None:
            try:
                _lock_fd.close()
            except Exception:
                pass
        _lock_fd = None
        return False

def _release_singleton():
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            try:
                _lock_fd.seek(0)
                msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        _lock_fd.close()
    except Exception:
        pass
    _lock_fd = None

atexit.register(_release_singleton)


# ── API 鉴权与 Host 常量 ──
HOST = "https://api.wnbtmel.com"
CUID = "526002076777845380"

# ── 令牌加载: 优先环境变量 GQ_REQUEST_ID, 否则从 gq/.env 读取 (gitignore 不入库) ──
# 这样无论用哪个 Python / 哪种启动方式, 都能自动拿到最新令牌, 避免副本丢失令牌.
def _load_request_id() -> str:
    env_tok = os.environ.get("GQ_REQUEST_ID")
    if env_tok:
        return env_tok
    # 回退: 读同目录 .env (格式 GQ_REQUEST_ID=xxx)
    try:
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gq", ".env")
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GQ_REQUEST_ID="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    # 最后兜底: 硬编码旧 token (可能已过期, 仅作占位)
    return "22f9755cba6b14eac450b4d2e537072607fac7a3"

# ── 令牌运行时热重载 ──
# 旧实现: REQUEST_ID = _load_request_id() 在导入时一次性求值 → "换号必须重启" 铁律。
# 现改为带缓存的动态读取: 每 _RID_TTL 秒重新读 .env/环境变量; 一旦 _flag_token_suspect
# 判定 token 失效会立即强制重载(绕过缓存). 用户更新 gq/.env 的 GQ_REQUEST_ID 后无需重启进程.
_RID_CACHE = {"val": None, "ts": 0.0}
_RID_TTL = 30.0
def _get_request_id() -> str:
    """返回当前有效 requestid; 30s 缓存, 到期重新从 .env/环境变量读取 (支持热更新)."""
    global _RID_CACHE
    now = time.time()
    if _RID_CACHE["val"] is None or now - _RID_CACHE["ts"] >= _RID_TTL:
        _RID_CACHE["val"] = _load_request_id()
        _RID_CACHE["ts"] = now
    return _RID_CACHE["val"]

# 三个接口路径
LIST_PATH = "/yewu11/v2/w/structureTournamentMatchesPB"
ODDS_PATH = "/yewu11/v1/w/getMatchBaseInfoByOddsPB"
# 早盘/结构端点: 传 mids 批量返回比赛基础信息(含队名/开赛时间/联赛),
# 对"未开赛"(尚无赔率)的比赛也返回完整队名 —— 而 ODDS_PATH 对未开赛返回 0401038。
# 用于滚球神器等需要展示未开赛比赛(带真实队名)的场景。
STRUCT_PATH = "/yewu11/v1/w/structureMatchBaseInfoByMidsPB"

# 公共请求头 (不要 cookie，不要打开浏览器)
_COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# ── 辅助函数 ──
def _build_headers() -> dict:
    """构造鉴权请求头。checkid 每次请求都重新生成 (含 uuid + 毫秒时间戳)。"""
    checkid = f"pc-{uuid.uuid4().hex}-{CUID}-{int(time.time() * 1000)}"
    h = dict(_COMMON_HEADERS)
    h["checkid"] = checkid
    h["requestid"] = _get_request_id()
    return h


def _api_post(path: str, body: dict, timeout: int = 20) -> Optional[dict]:
    """POST JSON 到 API，返回解析后的顶层 json dict；网络/HTTP 层失败返回 None。"""
    global _FENG_KONG_UNTIL
    url = HOST + path
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_build_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                safe_print(f"[WARN] {path} HTTP {resp.status}")
                return None
            raw = resp.read().decode("utf-8", errors="ignore")
        # ── 阿里云天级流控滑块检测 ──
        if any(k in raw for k in ("天级流控", "aliyun", "captcha", "滑动验证")):
            _FENG_KONG_UNTIL = time.time() + 1800  # 等30分钟
            safe_print(f"[风控] 阿里云流控触发 → 暂停30分钟 (until {_FENG_KONG_UNTIL:.0f})")
            return None
        return json.loads(raw)
    except Exception as e:
        safe_print(f"[WARN] {path} 请求失败: {e}")
        return None


_FENG_KONG_UNTIL = 0.0  # 全局风控截止时间


def _decode(raw) -> Optional[dict]:
    """解码 gzip+base64 的 data 字段 → dict；失败返回 None。"""
    # 防御: 若接口已经直接返回 dict/list，直接返回
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(gzip.decompress(base64.b64decode(raw)).decode("utf-8"))
    except Exception as e:
        safe_print(f"[WARN] 解码失败: {e}")
        return None


# ── token 失效检测 / 自愈 ──
# 乐鱼 requestid 是会过期的 web session (失效码 0401013=鉴权失败). 旧实现过期后
# fetch_match_list 静默返回空 → 采集器零产出却无任何提示, 只能人工发现并 kill+重启.
# 现: 检测到鉴权失败码 / 连续空列表 → 立即强制重载 .env(绕过缓存, pickup 用户写入的新 token)
# + 限频(5min)打醒目 ALERT, 不再静默断采; 用户改 .env 后无需重启进程即可恢复.
_TOKEN_ALERT_TS = 0.0
_EMPTY_STREAK = 0
_EMPTY_ALERT_TS = 0.0
# 0401038=未开赛/尚无赔率 — 非 token 失效, 绝不可当鉴权失败(会掩盖真故障+无谓重载 .env).
# 乐鱼明确鉴权失败码为 0401013(requestid 过期); 其余非 0000000 码一律按疑似鉴权失败处理.
_PREKICKOFF_CODES = ("0401038",)   # 未开赛/该场尚未开赔, 非 token 死

def _is_token_auth_fail(code) -> bool:
    """判断返回 code 是否代表 token 真正失效(需告警+重载 .env).

    0401038(未开赛/无赔率)等无害状态码返回 False, 不再误报/无谓重载 .env;
    仅真实鉴权失败(如 0401013)或其他非成功码才返回 True。
    """
    c = str(code or "")
    if c in _PREKICKOFF_CODES:
        return False
    if c == "0000000":
        return False
    return True

def _flag_token_suspect(code: str):
    """token 疑似失效/鉴权失败: 立即强制重载 .env (绕过缓存), 并限频告警."""
    global _TOKEN_ALERT_TS, _RID_CACHE
    _RID_CACHE = {"val": _load_request_id(), "ts": time.time()}  # 立即 pickup .env 新 token
    now = time.time()
    if now - _TOKEN_ALERT_TS >= 300:
        _TOKEN_ALERT_TS = now
        safe_print("=" * 64)
        safe_print(f"[ALERT] 乐鱼 token(requestid) 疑似失效/鉴权失败 code={code}")
        safe_print("        请更新 gq/.env 的 GQ_REQUEST_ID 为最新登录 URL 的 token= 值;")
        safe_print("        已自动重载 .env, 更新后无需重启采集器即可恢复.")
        safe_print("=" * 64)

def _note_empty_list():
    """code 正常但 data 为空: 可能真无比赛(深夜), 也可能 token 失效被静默返回空. 连续过多则告警."""
    global _EMPTY_STREAK, _EMPTY_ALERT_TS
    _EMPTY_STREAK += 1
    if _EMPTY_STREAK >= 8:
        now = time.time()
        if now - _EMPTY_ALERT_TS >= 300:
            _EMPTY_ALERT_TS = now
            safe_print("=" * 64)
            safe_print(f"[ALERT] 连续 {_EMPTY_STREAK} 轮比赛列表为空 (code 正常但 data 空).")
            safe_print("        如非深夜休赛, 疑似乐鱼 token 失效, 请更新 gq/.env 的 GQ_REQUEST_ID.")
            safe_print("=" * 64)


def _is_simulated_league(tn: str) -> bool:
    """屏蔽模拟/虚拟盘 (电子盘), 从根源拦截不入采集队列。

    委托 gq.db.is_virtual_league: VS- 电竞 + 8分钟虚拟杯 + 已知虚拟杯名
    (瓦尔哈拉/瓦尔基里/梦幻对垒)。真实联赛名绝不含 "分钟" 或这些虚拟杯名, 误杀安全。
    不使用 PANDA/EAFC 子串, 避免误杀被 PANDA 误标为真赛事的盘 (如真 WC 淘汰赛)。
    """
    from gq import db as _db
    return _db.is_virtual_league(tn)


def parse_ah_line(line: str) -> Optional[float]:
    """解析让球线: -0/0.5 → -0.25, +0/0.5 → +0.25, -1 → -1.0, 0.5 → 0.5 (负号=主让)。

    2026-08-05 修 (第四污染源 / 铁律1 "未知一律 --, 绝不填 0 假装已知"):
      旧实现在 line 为空时 **返回 0.0**, 于是 GQ 一旦不在 hon 里给让球数,
      整条让球盘就被贴上 "AH_0.00 平手盘" 的假标签落库。
      实测后果: 2026-07-18 之后全库 10852 条 AH 全是 AH_0.00, 让球梯队 100% 丢失;
      特征库 xah_line 变成恒 0 的死特征; label_ah 退化成 "1X2 去掉平局",
      直接伪造出 "AH AUC 0.7015 三任务最强" 的假结论 (已由
      scripts/ah_task_ablation.py 证伪: |A-C|=0.0003, xah_* 零贡献)。
      现在解析失败一律返回 None, 由调用方决定落 AH_UNK + line=NULL。
    """
    if line is None:
        return None
    s = str(line).strip().replace(" ", "")
    if not s:
        return None
    sign = -1 if s.startswith("-") else 1
    clean = s.lstrip("+-")
    try:
        if "/" in clean:
            a, b = clean.split("/")
            return sign * (float(a) + float(b)) / 2.0
        return sign * float(clean)
    except (TypeError, ValueError):
        return None


def parse_ou_line(line: str) -> Optional[float]:
    """解析大小球线: 2.5 → 2.5, 2/2.5 → 2.25; 解析不出返回 None。

    2026-08-05 修: 旧实现空值返回 2.5 (凭空捏造一条 2.5 大小球线), 同样违反铁律1。
    """
    if line is None:
        return None
    s = str(line).strip().replace(" ", "")
    if not s:
        return None
    try:
        if "/" in s:
            a, b = s.split("/")
            return (float(a) + float(b)) / 2.0
        return float(s)
    except (TypeError, ValueError):
        return None


def resolve_ah_line(line_elem: dict) -> Optional[float]:
    """从一个 hl 元素里把让球数挖出来; 挖不到返回 None。

    GQ 的字段位置在不同玩法/不同时期并不一致 —— OU 的线就不在 hon 而在 ol[].on
    (见 record_match_odds 中 '全场大小' 分支的历史注释)。让球盘同样可能漂移,
    因此这里按优先级依次尝试, 而不是只认 hon。
    """
    cands = [line_elem.get("hon")]
    for opt in (line_elem.get("ol") or [])[:2]:
        if isinstance(opt, dict):
            cands.append(opt.get("on"))
            cands.append(opt.get("ot"))
    for c in cands:
        lv = parse_ah_line(c)
        if lv is not None and abs(lv) <= 5:      # 边界保护: 正常AH线在±5以内
            return lv
    return None


# 让球线解析失败时的一次性原始结构留证 (等 token 刷新后自动抓到地面真相)。
_AH_DIAG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ah_raw_diag.json")
_ah_diag_done = False


def _dump_ah_diag(match_key: str, pd_raw: dict):
    """让球线挖不出来时, 把该玩法的原始 JSON 留一份证据, 每进程只写一次。"""
    global _ah_diag_done
    if _ah_diag_done:
        return
    _ah_diag_done = True
    try:
        with open(_AH_DIAG_PATH, "w", encoding="utf-8") as f:
            json.dump({"match_key": match_key, "ts": time.time(), "playData": pd_raw},
                      f, ensure_ascii=False, indent=2)
        safe_print(f"[AH-DIAG] 让球线字段缺失, 原始结构已留证 -> {_AH_DIAG_PATH}")
    except Exception:
        pass


# ── 时间轴关联辅助 (纯 stdlib) ──
_TZ8 = timezone(timedelta(hours=8))


def _kickoff_iso(mgt):
    """列表 mgt(毫秒) → GMT+8 字符串 'YYYY-MM-DD HH:MM'; 解析失败返回 ''."""
    try:
        return datetime.fromtimestamp(float(mgt) / 1000, _TZ8).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _score_from_msc(msc):
    """msc 统计标记集合(list) -> (sh, sa, ht_sh, ht_sa) 整数元组; 解析失败返回 (None,)*4.

    修BUG(2026-07-30): msc 不是时间线而是统计标记集合 (S0=当前全场比分,
    S8=进攻, S104/S105=控球等, S5xxxx=其他统计). 旧逻辑取 msc[-1] 会拿到
    'S50016|0:0' 这类统计项, 把 3-1 的比赛记成 0-0. 必须优先取 'S0|' 项.
    2026-08-03: 新增解析 'S1|' 项 = 半场(HT)比分 (用户要求采集完整数据).
    """
    if not msc:
        return None, None, None, None

    def _split(it):
        if not isinstance(it, str) or "|" not in it:
            return None, None
        s = it.split("|")[-1]
        try:
            h, a = s.split(":")
            return int(h), int(a)
        except Exception:
            return None, None

    full = half = None
    for item in msc:
        if isinstance(item, str):
            if item.startswith("S0|") and full is None:
                full = item
            elif item.startswith("S1|") and half is None:
                half = item
    if full is None and msc:
        full = msc[-1]  # 兜底: 老格式无 S0 标记时保持旧行为
    return _split(full) + _split(half)



# 中场休息时长(分钟) — 与 analysis.live_goal_probe.HALFTIME_BREAK_MIN 保持一致。
# 墙钟 elapsed 与"真实比赛分钟"的换算基准: 比赛分钟 = elapsed - HT_BREAK_MIN (下半场)。
HT_BREAK_MIN = 15
# 终场判定的墙钟阈值(分钟): 45(上半场)+15(中场)+45(下半场)+5(补时余量) = 110
FT_ELAPSED_MIN = 110


def _parse_kickoff(s):
    """把 matches.kickoff (naive GMT+8) 解析为 Unix 时间戳。

    与 analysis.live_goal_probe._parse_kickoff 同实现, 供 ws_collector 复用
    (避免 gq 层反向依赖 analysis 层)。
    """
    if not s:
        return None
    from datetime import datetime, timezone, timedelta
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.timestamp()
    except Exception:
        return None


def _elapsed_to_minute(elapsed):
    """墙钟已进行秒数 → 真实比赛分钟 (扣中场休息 15 分钟)。

    分段: 上半场 est<=45 → est ; 中场 45<est<=60 → 45 ; 下半场 est>60 → est-15。
    与 analysis.live_goal_probe.resolve_true_minute 的中场口径保持一致。
    """
    est = max(0, int(elapsed / 60))
    if est <= 45:
        return est
    if est <= 45 + HT_BREAK_MIN:
        return 45
    return min(125, est - HT_BREAK_MIN)


def _status_minute(mlet, kickoff_ts, now_ts):
    """从 mlet 推断状态(scheduled/live/finished) + 分钟数.

    kickoff_ts 单位毫秒; now_ts 单位秒. 兜底: 开赛超 2.5h 仍非 live → finished.
    新增(WS2): 开赛时间在未来 → scheduled (纠正 GQ 把未开赛场误标 live).
    修正(2026-08-15): 无 mlet 但已开赛时, 保留 kickoff 估算的分钟, 不再被后面的 minute=0 重置。
    """
    elapsed = (now_ts - kickoff_ts / 1000) if kickoff_ts else None

    # 1) 从 mlet 推断状态 + 分钟
    if not mlet:
        # 无分钟字符串: 依据 kickoff 判断。已开赛 → live(刚开赛 GQ 尚未推分钟属正常,
        # 用 kickoff 估算当前分钟); 尚未开赛 → scheduled。
        if elapsed is not None and elapsed >= 0:
            st = "live"
            minute = _elapsed_to_minute(elapsed)   # 扣中场休息, 与规则4同口径(2026-08-29)
        else:
            st = "scheduled"
            minute = 0
    else:
        st = "live"
        mm = re.match(r"(\d+)(?:\+(\d+))?", mlet)
        if mm:
            minute = int(mm.group(1)) + (int(mm.group(2)) if mm.group(2) else 0)
        else:
            minute = 0
        m = re.match(r"(\d+):\d+", mlet)
        if (m and int(m.group(1)) >= 90) or "FT" in mlet or "完" in mlet:
            st = "finished"

    # 2) 未来开赛 → scheduled (纠正 GQ 把未开赛场误标 live)
    if kickoff_ts and now_ts < kickoff_ts / 1000:
        st = "scheduled"
        minute = 0

    # 3) 开赛超3.5h → 强制已结束(覆盖 mlet 卡死/空 mlet 等所有情况; 延迟/加时/点球真活比赛需要更宽容窗口)
    if elapsed is not None and elapsed > 3.5 * 3600:
        st = "finished"

    # 4) 2026-08-29 重写 (全库 61.8% minute_at 污染的根因就在这里):
    #      乐鱼 feed 整个上半场恒报 mlet="45"、整个下半场恒报 "90", 是**占位垃圾**,
    #      实测 events.db 878 万条 minute_at>0 快照里 543 万条 (61.8%) 卡死在 45/90。
    #      原逻辑把它当"地面真相"进 pass 分支直接采信 → 写进库的就是 45/90。
    #    新口径 (与 analysis.live_goal_probe.resolve_true_minute 保持一致):
    #      a) 脏值检测 — ① mlet 恰为 "45"/"90" 且无补时后缀("45+2"/"90+3" 才是真值);
    #                    ② minute>=45 但 elapsed<45 (开赛不足45min 却报 45+, 必然脏)
    #      b) 命中脏值 **或** feed 分钟与墙钟背离>10min(feed 停滞) → 用 elapsed 推算
    #         真实比赛分钟 (扣中场休息 15min):
    #           上半场 est<=45 → est ; 中场 45<est<=60 → 45 ; 下半场 est>60 → est-15
    #      c) 都不命中(如 "38" 且墙钟吻合) → 信任 feed 分钟, 支持延迟比赛
    _fixed_by_elapsed = False
    if elapsed is not None:
        est = max(0, int(elapsed / 60))
        _mlet_s = str(mlet).strip() if mlet else ""
        _dirty = (bool(re.fullmatch(r"(45|90)", _mlet_s))
                  or (minute is not None and minute >= 45 and elapsed < 45 * 60))
        _diverged = (minute is not None and abs(est - minute) > 10)
        if (_dirty or _diverged) and elapsed < 3.5 * 3600:
            minute = _elapsed_to_minute(elapsed)
            _fixed_by_elapsed = True

    # 5) 兜底: mlet 卡 45' 但已开赛超 60min (与规则4互补, 防 est 边界抖动)。
    #    2026-08-29 修正: 原 `max(90, ...)` 把卡 45' 的比赛直接推到 90+, 跳过整个
    #    下半场 —— 实测 elapsed=61min 时给出 90(真实应为 46)。改扣中场休息推算。
    if minute == 45 and elapsed is not None and elapsed > 60 * 60 and not _fixed_by_elapsed:
        minute = max(46, min(125, int(elapsed / 60) - HT_BREAK_MIN))

    # 4b) 终场判定 (2026-08-29 口径修正): 原用**墙钟** `elapsed>=90` 判 finished ——
    #     墙钟 90 分钟时比赛分钟才 75(要扣中场 15), 会过早翻 finished, 把还在打的
    #     比赛提前归档。改双判据, 任一满足即 finished:
    #       ① 墙钟 >= FT_ELAPSED_MIN(110)  → 比赛分钟 >= 95 (含补时余量 5 分钟)
    #       ② 比赛分钟 >= 95 (真值 feed 报 90+5 及以后)
    #     注: 完全不依赖 minute_at, 规则3(3.5h) 仍作最终兜底。
    if st != "finished" and elapsed is not None:
        if elapsed >= FT_ELAPSED_MIN * 60 or (minute is not None and minute >= 95):
            st = "finished"

    return st, minute


def _upsert_match_full(match_key, home, away, league, mid, kickoff, status,
                       score_home, score_away, minute):
    """同 gq.db.upsert_match 但带 mid 列 (用于 decoded 为 None 的占位写入).

    单独开一个 db 连接, 在一条语句里写入 mid + 其余字段, 避免 upsert_match
    不接收 mid 而需二次 UPDATE.
    """
    # 纵深防御: 队名缺失拒绝写入 (正常路径已不再调用本函数)
    if not home or not str(home).strip() or not away or not str(away).strip() \
       or str(home).strip() == "vs" or str(away).strip() == "vs":
        return
    now = time.time()
    from gq import db as _db
    with _db.conn() as _c:
        cur = _c.execute("SELECT 1 FROM matches WHERE match_key=?", (match_key,))
        if cur.fetchone() is None:
            _c.execute("""INSERT INTO matches
                (match_key, home, away, league, kickoff, status,
                 score_home, score_away, minute, mid, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (match_key, home, away, league, kickoff, status,
                 score_home, score_away, minute, mid, now, now))
        else:
            # P2: 人工纠偏锁 — 已锁定的比赛, 采集器绝不覆盖其赛果/状态
            _ov = _c.execute("SELECT is_override FROM matches WHERE match_key=?", (match_key,)).fetchone()
            if _ov and _ov[0]:
                return
            _c.execute("""UPDATE matches SET
                home=?, away=?, league=?, kickoff=?, status=?,
                score_home=?, score_away=?, minute=?, mid=?, last_seen=?
                WHERE match_key=?""",
                (home, away, league, kickoff, status,
                 score_home, score_away, minute, mid, now, match_key))


# ── 接口调用 ──
def fetch_match_list() -> list[dict]:
    """拉比赛列表，返回展开后的比赛项列表 (每项含 mid/tn/mgt/tid/csid)。

    mids 可能是逗号分隔的多个比赛 ID，这里统一 split(',') 展开。
    """
    body = {"cuid": CUID, "sort": 1, "tid": "", "apiType": 1, "orpt": 0, "euid": "3020101"}
    js = _api_post(LIST_PATH, body)
    if not js:
        return []
    code = js.get("code")
    if code != "0000000":
        if _is_token_auth_fail(code):
            safe_print(f"[WARN] 比赛列表 code={code}")
            _flag_token_suspect(str(code))
        return []
    data = _decode(js.get("data", ""))
    if not data:
        _note_empty_list()
        return []
    global _EMPTY_STREAK
    _EMPTY_STREAK = 0  # 成功拿到数据, 重置连续空计数

    items = []
    for grp in ("livedata", "nolivedata"):
        for it in data.get(grp, []):
            tn = it.get("tn", "")
            if _is_simulated_league(tn):
                continue  # 屏蔽 VS- 模拟电竞比赛, 不进入采集队列
            mids = str(it.get("mids", "")).split(",")
            for mid in mids:
                mid = mid.strip()
                if not mid:
                    continue
                items.append({
                    "mid": mid,
                    "tn": it.get("tn", ""),
                    "mgt": it.get("mgt", 0),
                    "tid": it.get("tid", ""),
                    "csid": it.get("csid", ""),
                    "grp": grp,  # 'livedata'=进行中, 'nolivedata'=非进行中(未开赛/已完); 供上层按需筛选
                })
    return items


def fetch_match_odds(mid: str) -> Optional[dict]:
    """拉单场赔率，返回解码后的 dict (含 data / playData)；失败返回 None。"""
    body = {"cuid": CUID, "cos": 0, "orpt": 0, "euid": "3020101",
            "mid": str(mid), "mcid": 0, "newUser": 0}
    js = _api_post(ODDS_PATH, body)
    if not js:
        return None
    if js.get("code") != "0000000":
        c = js.get("code")
        # 0401038=未开赛/尚无赔率: 无害, 静默忽略(不告警/不重载 .env)
        if c not in _PREKICKOFF_CODES and _is_token_auth_fail(c):
            safe_print(f"[WARN] 赔率 code={c} mid={mid}")
            _flag_token_suspect(str(c))
        return None
    return _decode(js.get("data", ""))


def fetch_match_structure(mids: list) -> list:
    """批量拉比赛基础信息(早盘/未开赛也返回队名+开赛时间)。

    走 STRUCT_PATH = structureMatchBaseInfoByMidsPB: 传 mids 逗号串, 返回 data.data 列表,
    每项含 mhn/man(队名)/mgt(开赛ms)/tnjc(联赛)/mststi(状态)/msc(比分) 等完整字段。
    与 ODDS_PATH 不同, 该端点对"尚未开赔"的未开赛比赛仍返回真实队名 —— 滚球神器
    展示未开赛比赛时, 必须从本端点取队名(ODDS_PATH 对未开赛返回 0401038 无队名)。

    入参 mids 可为任意可迭代(内部转 str)。返回解码后的比赛 dict 列表; 失败/空返回 []。
    自动分批(每批 <=50)以避免超大 payload。
    """
    if not mids:
        return []
    out = []
    batch_size = 25  # 乐鱼结构端点单批最多返回 25 条, 超出的 mid 会被截断丢弃 → 必须 <=25/批
    try:
        mid_list = [str(x).strip() for x in mids if str(x).strip()]
    except Exception:
        return []
    for i in range(0, len(mid_list), batch_size):
        batch = mid_list[i:i + batch_size]
        body = {"mids": ",".join(batch), "cuid": CUID,
                "cos": 0, "orpt": 0, "euid": "3020101"}
        js = _api_post(STRUCT_PATH, body)
        if not js or js.get("code") != "0000000":
            continue
        data = _decode(js.get("data", ""))
        if isinstance(data, dict):
            arr = data.get("data") or []
        elif isinstance(data, list):
            arr = data
        else:
            arr = []
        for m in arr:
            if isinstance(m, dict):
                out.append(m)
        time.sleep(0.05)  # 轻微限速, 避免连续批量请求触发流控
    return out


# ── 解析 playData 并写入 DB ──
def record_match_odds(decoded: dict, it: dict = None) -> Optional[str]:
    """解析单场解码后的 dict，写 match 元信息 + 各市场赔率快照。返回 match_key 或 None。

    it 为列表项 dict (含 mid/mgt/tn)，由 collect_round 传入，用于把 mid/kickoff
    写入 matches 表 (时间轴按 mid 关联列表接口)。it 为 None 时退化为"纯赔率"模式。
    """
    mid = (it or {}).get("mid")
    mgt = (it or {}).get("mgt")
    tn = (it or {}).get("tn", "")

    # 防御: 即使在列表层漏过, 写入前再次拦截模拟电竞比赛
    if _is_simulated_league(tn):
        return None

    # —— 详情接口返回 null (decoded 为 None): 刚开赛/网络抖动/限流 导致暂无可解析数据 ——
    # 列表项 it 仅含 mid/mgt, 不含主客队名(队名在详情里), 无法建新行; 但可凭 mid 找到
    # 已存在的行并按 kickoff 估算续更 status/minute, 防止比赛冻结在旧状态(如卡在 45').
    # 不写赔率快照、不动比分(那需详情); 若库内尚无该行, 等详情可用时再建(UPDATE 影响 0 行无副作用).
    # 注意: 早期版本用 it.get("mhn") 守卫, 但列表项永不含队名 → 该分支恒 return None 永不更新,
    # 导致"详情短暂缺失"的比赛冻结在旧分钟(实测 诺士郡 卡 45'/K联赛 卡 31'). 现改为按 mid 续更.
    if decoded is None:
        if mid and mgt:
            st, minute = _status_minute("", float(mgt), time.time())
            from gq import db as _db
            with _db.conn() as _c:
                _c.execute(
                    "UPDATE matches SET status=?, minute=?, last_seen=? "
                    "WHERE mid=? AND (is_override IS NULL OR is_override=0)",
                    (st, minute, time.time(), mid))
        return None

    m_list = decoded.get("data", [])
    if not m_list:
        return None
    m = m_list[0]
    mhn = (m.get("mhn") or "").strip()      # 主队名
    man = (m.get("man") or "").strip()      # 客队名
    mlet = m.get("mlet") or ""              # 已进行时间; 空字符串 "" = 未开赛
    if not mhn or not man:
        return None

    match_key = f"{mhn} vs {man}"
    # 比分 + 状态 + 分钟 (来自 msc / mlet)
    sh, sa, ht_sh, ht_sa = _score_from_msc(m.get("msc"))
    st, minute = _status_minute(mlet, float(mgt) if mgt else 0, time.time())
    # 修BUG(2026-08-20 审计): 完场后 re-sweep 时乐鱼源会把 msc 的 S1(半场)项
    # 污染成终场比分(实证: 特罗姆瑟U19场 ht_score 被覆盖成 1-3, 真实半场 0-1)。
    # db.py 的防 clobber 只防"新值为空", 不防"新值为错误值"。半场比分是既成
    # 历史事实, 完场后不再更新 → 强制 None 走"保留旧值"分支。
    if st == 'finished':
        ht_sh = ht_sa = None
    # 写比赛元信息 + kickoff/score/status/minute (+半场比分 ht)
    upsert_match(match_key, mhn, man, tn, kickoff=_kickoff_iso(mgt), status=st,
                 score_home=sh, score_away=sa,
                 ht_score_home=ht_sh, ht_score_away=ht_sa, minute=minute)

    # 2026-08-20 分钟级数据流修复: 把已解析的滚球分钟/比分带入每笔赔率快照,
    # 使 odds_snapshots.minute_at/score_at 记录真实滚球状态(此前全为0/空, 导致
    # 80万条"滚球"快照无分钟级信息, 破蛋神器无法做逐分钟分析).
    score_at_str = f"{sh}-{sa}" if (sh is not None and sa is not None) else ""
    def snap(market, selection, odds, line=None):
        return record_snapshot(match_key, market, selection, odds, line=line,
                               score_at=score_at_str, minute_at=minute)

    # 写 mid (upsert_match 不接收 mid, 单独 UPDATE)
    if mid:
        from gq import db as _db
        with _db.conn() as _c:
            _c.execute("UPDATE matches SET mid=? WHERE match_key=?", (mid, match_key))

    # ── 初盘→赛果归档: 检测到完场+有比分 → 写入 match_outcomes ──
    if st == "finished" and sh is not None and sa is not None and mid:
        try:
            outcome = record_match_outcome(mid, mhn, man, tn,
                                           kickoff=_kickoff_iso(mgt),
                                           score_home=sh, score_away=sa,
                                           ht_score_home=ht_sh, ht_score_away=ht_sa,
                                           match_key_override=match_key)
            if outcome:
                _log_msg = f"初盘归档: {mhn} vs {man} {sh}-{sa} [{outcome['result']}] type={outcome['odds_type']}"
                safe_print(f"[{datetime.now().strftime('%H:%M:%S')}] {_log_msg}")
        except Exception:
            pass  # 归档失败不影响赔率写入主流程

    # 遍历所有玩法市场
    for pd in decoded.get("playData", []):
        hpn = pd.get("hpn", "")

        # —— 1X2 独赢 (GQ 不同联赛用不同标签: "独赢" / "全场独赢") ——
        if hpn in ("独赢", "全场独赢"):
            hl = pd.get("hl", [])
            if not hl:
                continue
            ol = hl[0].get("ol", [])
            if len(ol) >= 3:
                snap("1X2", "home", ol[0].get("ov", 0) / 100000)
                snap("1X2", "draw", ol[1].get("ov", 0) / 100000)
                snap("1X2", "away", ol[2].get("ov", 0) / 100000)

        # —— 全场让球 AH ——
        elif hpn == "全场让球":
            for line_elem in pd.get("hl", []):
                # 2026-08-05: 不再只认 hon (GQ 会把线放到 ol[].on, OU 已有前科)。
                lv = resolve_ah_line(line_elem)
                ol = line_elem.get("ol", [])
                if len(ol) < 2:
                    continue
                if lv is None:
                    # 铁律1: 未知就是未知, 绝不写成 AH_0.00 冒充平手盘。
                    _dump_ah_diag(match_key, pd)
                    snap("AH_UNK", "home",
                                    ol[0].get("ov", 0) / 100000, line=None)
                    snap("AH_UNK", "away",
                                    ol[1].get("ov", 0) / 100000, line=None)
                    continue
                snap(f"AH_{lv:.2f}", "home",
                                ol[0].get("ov", 0) / 100000, line=lv)
                snap(f"AH_{lv:.2f}", "away",
                                ol[1].get("ov", 0) / 100000, line=lv)

        # —— 全场大小 OU ——
        # 注: GQ 的 OU 结构与 AH 不同 —— hon 字段是赔率整数(如 '32069'), 真实大小球线在 ol[].on (如 "1.5/2")。
        #     不能像 AH 那样用 hon 解析线, 否则 parse_ou_line 会把赔率当线 -> lv>10 全跳过 (历史 OU 覆盖仅 0.5% 的根因)。
        elif hpn == "全场大小":
            for line_elem in pd.get("hl", []):
                ol = line_elem.get("ol", [])
                if len(ol) < 2:
                    continue
                on = ol[0].get("on", "") or line_elem.get("hon", "")
                lv = parse_ou_line(on)
                if lv is None or lv <= 0 or lv > 10:   # P0a: 边界保护, 正常大小球在0.5~10
                    continue
                snap(f"OU_{lv:.2f}", "over",
                                ol[0].get("ov", 0) / 100000, line=lv)
                snap(f"OU_{lv:.2f}", "under",
                                ol[1].get("ov", 0) / 100000, line=lv)

        # —— 全场波胆 CS (最关键市场) —— 仅赛前(scheduled)采集。
        #   2026-08-17 铁律: 开赛后(live/finished)CS 赔率无价值, 不再采集(含 obscure live 场)。
        #   写时卫生: 比分非法 / 赔率<=0 或 >1000 直接丢弃, 不写入 odds_snapshots (防 0.0 脏数据)。
        elif hpn == "全场波胆" and st == "scheduled":
            for line_elem in pd.get("hl", []):
                for opt in line_elem.get("ol", []):
                    on = opt.get("on", "")       # 比分, 如 "1-0" / "2-1" / "其他"
                    ov = opt.get("ov", 0)        # 赔率原始整数
                    on = normalize_cs_score(on)  # '1-0'/'1.0' → '1:0'
                    if on is None:               # 空选项/非法比分跳过("其他"等保留项原样归一后非空)
                        continue
                    ovv = ov / 100000
                    if ovv <= 0 or ovv > 1000:   # 0.0 等脏赔率丢弃, 源头拦截
                        continue
                    snap("CS", on, ovv)

        # —— 半场标准盘 (上半场/下半场独赢/让球/大小, 2026-08-02 对齐模板) ——
        # 仅匹配 GQ 标准半场盘名, 严禁"半场"通配(会误抓"上/下半场全胜"等球队 prop 垃圾).
        # 下半场 -> _2H 后缀(对齐模板"下半场"列); 上半场 -> _1H 后缀.
        # 注: GQ 无标准半场波胆市场, 故半场块不含 CS.
        elif hpn in ("下半场独赢", "下半场让球", "下半场大小",
                     "上半场独赢", "上半场让球", "上半场大小"):
            suffix = "_2H" if hpn.startswith("下半场") else "_1H"
            if "独赢" in hpn:
                hl = pd.get("hl", [])
                if hl:
                    ol = hl[0].get("ol", [])
                    if len(ol) >= 3:
                        snap(f"1X2{suffix}", "home", ol[0].get("ov", 0) / 100000)
                        snap(f"1X2{suffix}", "draw", ol[1].get("ov", 0) / 100000)
                        snap(f"1X2{suffix}", "away", ol[2].get("ov", 0) / 100000)
            elif "让球" in hpn:
                for line_elem in pd.get("hl", []):
                    lv = resolve_ah_line(line_elem)
                    ol = line_elem.get("ol", [])
                    if len(ol) < 2: continue
                    if lv is None:            # 铁律1: 未知不写 0.00
                        _dump_ah_diag(match_key, pd)
                        snap(f"AH{suffix}_UNK", "home", ol[0].get("ov", 0) / 100000, line=None)
                        snap(f"AH{suffix}_UNK", "away", ol[1].get("ov", 0) / 100000, line=None)
                        continue
                    snap(f"AH{suffix}_{lv:.2f}", "home", ol[0].get("ov", 0) / 100000, line=lv)
                    snap(f"AH{suffix}_{lv:.2f}", "away", ol[1].get("ov", 0) / 100000, line=lv)
            elif "大小" in hpn:
                for line_elem in pd.get("hl", []):
                    ol = line_elem.get("ol", [])
                    if len(ol) < 2: continue
                    on = ol[0].get("on", "") or line_elem.get("hon", "")
                    lv = parse_ou_line(on)
                    if lv is None or lv <= 0 or lv > 10: continue
                    snap(f"OU{suffix}_{lv:.2f}", "over", ol[0].get("ov", 0) / 100000, line=lv)
                    snap(f"OU{suffix}_{lv:.2f}", "under", ol[1].get("ov", 0) / 100000, line=lv)

    return match_key


# ── 单写者写连接上下文管理器 (事故②尾巴闭环) ──
# `_sweep_finished` 等关键写路径原直接裸 `sqlite3.connect(DB_PATH)` 直写, 抢写锁 → 坏页根因。
# 现统一委托 `gq.db.conn()` (core.db_manager 单写者) 串行化写, 杜绝并发写抢锁。
@contextlib.contextmanager
def _gq_single_writer():
    """委托单写者: 返回 gq.db.conn() 写连接上下文管理器 (嵌套可重入, 仅最外层提交)。"""
    with conn() as _writer:
        yield _writer


# ── 采集器 (纯 HTTP, 同步) ──
class GQCollector:
    """纯 HTTP 全市场赔率采集器 (v3.0, 无浏览器)。"""

    def __init__(self, interval_sec: int = 45, max_per_round: int = 200,
                 sleep_min: float = 0.05, sleep_max: float = 0.15,
                 full_every: int = 4,
                 fast_interval_sec: int = 5,
                 focus_mids: Optional[list] = None,
                 focus_match_keys: Optional[list] = None,
                 focus_file: str = "",
                 auto_focus: bool = True):
        self.interval_sec = interval_sec
        # 每 full_every 轮做一次"全量轮"(含远期未开赛/疑完场)。
        # 其余轮次只采 进行中+即将开赛, 把带宽让给实时性最关键的场次。
        self.full_every = max(1, full_every)
        # 每轮最多处理的比赛数。历史默认 60 会把 115+ 场的列表硬截断掉 ~48%,
        # 导致排在 60 名之后的比赛(常见于中场休息时被挤后)彻底失联 ——
        # minute 永久冻结在 45、status 永久冻结在 live。现放宽到能容纳全量,
        # 并配合 _priority() 排序: 即使触顶, 被牺牲的也只会是已完场/远期未开赛。
        self.max_per_round = max_per_round
        self.sleep_min = sleep_min
        self.sleep_max = sleep_max
        self._iter = 0
        # 秒级焦点轮询配置
        self.fast_interval_sec = max(1, fast_interval_sec)
        self._kickoff_probed = set()  # 开赛+1min已精确拉取的场(防重, 仅成功写入才标记)
        self.focus_mids = set(str(m).strip() for m in (focus_mids or []) if str(m).strip())
        self.focus_match_keys = set(str(k).strip() for k in (focus_match_keys or []) if str(k).strip())
        # 初始 CLI 焦点 (--focus-match-keys) 作为种子, 与 focus-file 内容合并, 不被文件覆盖清空
        self._seed_focus_match_keys = set(self.focus_match_keys)
        # --focus-file: 采集器每 30s 读取该 JSON 的 match_keys 合并为秒级焦点.
        # 防御: 相对路径相对采集器脚本目录解析 (启动 cwd 可能非 gq/), 避免找不到文件.
        self.focus_file = ""
        if focus_file and str(focus_file).strip():
            _ff = str(focus_file).strip()
            if not os.path.isabs(_ff):
                _ff = os.path.join(os.path.dirname(os.path.abspath(__file__)), _ff)
            self.focus_file = os.path.normpath(_ff)
        self._focus_items: dict = {}   # mid -> it dict 缓存
        self._focus_lock = threading.Lock()
        self._last_focus_refresh = 0.0
        self._fast_running = False
        self._focus_file_mtime = 0.0     # focus-file 最近修改时间, 用于 ttl 过期判断
        self._focus_ttl_seconds = 60     # focus-file 有效期, 过期回落到 CLI 种子
        # 自动聚焦活跃比赛(默认开): 守护循环每 60s 把进行中比赛塞进 focus 列表, 复用 5s 焦点轮压缩比分滞后
        self.auto_focus = auto_focus
        # 事故④ 根治: 统一安全日志出口 (UTF-8 + backslashreplace, 绝不抛 UnicodeEncodeError)
        self._slog = SafeLog("gq.auto_collector", echo=True)
        # 暴露最近一轮逐步结果, 供运维/测试断言 (如 persist 失败→live_flip 仍执行)
        self._last_round_results: list = []

    def log(self, msg: str):
        t = datetime.now().strftime("%H:%M:%S")
        line = f"[{t}] {msg}"
        # 事故④ 根治: 经 SafeLog.safe_print, 非 ASCII/emoji 一律 backslashreplace,
        # 绝不抛 UnicodeEncodeError (曾经的 ⚠ 崩点已消除, 不再提前 return 跳过 live 翻页)
        self._slog.print(line)

    @staticmethod
    def _priority(it: dict, now_s: float) -> tuple:
        """轮询优先级 (数字越小越先采, 同级按时间距离近者优先)。

        0 = 进行中 (已开赛 0~2.5h) —— 必须每轮采到, 否则比分/分钟数会冻结
        1 = 即将开赛 (2h 内)       —— 盘口波动最剧烈
        2 = 远期未开赛 / 无开赛时间
        3 = 已过 2.5h (大概率完场) —— 赔率接口多半返回 null, 最先可牺牲

        修正(2026-08-15): 进行中内部把接近完场(>=75min)的排最前, 其余按已开赛时间
        降序(老的在前), 避免 90min 比赛被 truncation 切掉导致比分/分钟 stale。
        """
        try:
            ko = float(it.get("mgt") or 0) / 1000.0
        except (TypeError, ValueError):
            ko = 0.0
        if ko <= 0:
            return (2, 0.0)
        delta = now_s - ko            # >0 已开赛, <0 未开赛
        if 0 <= delta <= 2.5 * 3600:
            # 已开赛: 接近完场的(>=75min)优先级最高; 其余老的在前(避免僵尸)
            finishing = 0 if delta >= 75 * 60 else 1
            return (0, finishing, -delta)
        if -2 * 3600 <= delta < 0:
            return (1, -delta)
        if delta < 0:
            return (2, -delta)
        return (3, delta)

    def collect_round(self, limit: Optional[int] = None) -> int:
        """一轮采集: 拉列表 → 遍历 mid 拉赔率写入 → 库存兜底 → CS冻结 → 赛前固化。
        返回成功写入的比赛数。

        事故④ 根治: 用 ``core.collector_step.CollectorRound`` 把每轮关键步骤隔离,
        单步失败只记 ``StepResult`` 并**继续后续步骤**, 绝不提前 return 跳过
        ``live_flip``(``_sweep_finished`` 的 live→finished 翻页) → 消除"刚开赛不显示"。
        """
        from core.collector_step import CollectorRound, CollectorContext, FunctionStep

        ctx = CollectorContext(round_no=self._iter)
        now_s = time.time()

        def step_fetch_and_prepare(c: CollectorContext) -> int:
            """fetch list + 优先级排序/分级降频/焦点缓存/截断 (原 collect_round 前半段)。"""
            items = fetch_match_list()
            if not items:
                self.log("比赛列表为空")
                # 不提前 return: 后续 live_flip/freeze/capture 仍应执行(它们读 DB, 不依赖列表)
                c.set("items", [])
                c.set("ok", 0)
                return 0
            items.sort(key=lambda x: self._priority(x, now_s))
            total = len(items)

            deferred = 0
            is_full = (self._iter <= 1) or (self._iter % self.full_every == 0)
            if not is_full:
                kept = [x for x in items if self._priority(x, now_s)[0] <= 1]
                deferred = len(items) - len(kept)
                items = kept

            # 焦点比赛缓存: 记录 mid/mgt/tn 用于秒级 fast_round
            if self.focus_mids or self.focus_match_keys:
                with self._focus_lock:
                    for it in items:
                        mid = str(it.get("mid", ""))
                        mk = f"{it.get('mhn','')} vs {it.get('man','')}".strip()
                        if mid in self.focus_mids or mk in self.focus_match_keys:
                            self._focus_items[mid] = it

            # 限制每轮比赛数: 进行中比赛绝不被截断, 只牺牲未开赛/疑完场.
            if limit:
                items = items[:limit]
            if self.max_per_round and len(items) > self.max_per_round:
                live_items = [x for x in items if self._priority(x, now_s)[0] == 0]
                if len(live_items) >= self.max_per_round:
                    items = live_items
                    self.log(f"[!] 进行中 {len(live_items)} 场 ≥ 上限 {self.max_per_round}, 全部保留")
                else:
                    non_live = [x for x in items if self._priority(x, now_s)[0] != 0]
                    items = live_items + non_live[:self.max_per_round - len(live_items)]
                    self.log(f"[!] 列表 {total} 场 超出上限 {self.max_per_round}, "
                             f"优先保留 {len(live_items)} 场进行中")
            live_n = sum(1 for x in items if self._priority(x, now_s)[0] == 0)
            _tag = "全量轮" if is_full else f"临场轮, 缓采{deferred}场"
            self.log(f"比赛列表: {len(items)}/{total} 场 (进行中 {live_n}) [{_tag}]")

            c.set("items", items)
            c.set("ok", 0)
            return len(items)

        def step_parse(c: CollectorContext) -> int:
            """逐场拉赔率并解码 (parse); 单场拉取失败仅记日志并跳过该场, 不中断步骤。"""
            items = c.get("items", []) or []
            parsed = []
            for it in items:
                try:
                    decoded = fetch_match_odds(it["mid"])
                except Exception as e:  # fetch 失败: 跳过该场, 继续其余
                    self.log(f"单场解析 {it.get('mid')} 失败: {e}")
                    decoded = None
                parsed.append((it, decoded))
            c.set("parsed", parsed)
            return len(parsed)

        def step_persist(c: CollectorContext) -> int:
            """逐场写入 DB (persist)。

            ⚠ 刻意不在单场级吞异常: 单场写入失败会令本步骤记为失败, 由 ``CollectorRound``
            在**步骤级**隔离 —— 后续 ``live_flip`` 等步骤照常执行, 绝不会因此跳过
            live→finished 翻页。下一轮会重试失败场次。
            """
            parsed = c.get("parsed", []) or []
            ok = 0
            for it, decoded in parsed:
                key = record_match_odds(decoded, it)  # 失败则步骤级抛, 由运行器隔离
                if key:
                    ok += 1
                # 场间随机 sleep, 降低被封风险
                time.sleep(random.uniform(self.sleep_min, self.sleep_max))
            c.set("ok", ok)
            return ok

        def step_live_flip(c: CollectorContext) -> str:
            """live→finished 翻页兜底 (库存兜底)。关键步骤: 绝不因前序失败而跳过。"""
            self._sweep_finished()
            return "live_flip_ok"

        def step_freeze_cs(c: CollectorContext) -> None:
            self._freeze_scheduled_cs()

        def step_capture(c: CollectorContext) -> None:
            self._capture_prematch_conclusions()

        steps = [
            FunctionStep("fetch_list", step_fetch_and_prepare, critical=True),
            FunctionStep("parse", step_parse),
            FunctionStep("persist", step_persist),
            FunctionStep("live_flip", step_live_flip, critical=True),
            FunctionStep("freeze_scheduled_cs", step_freeze_cs),
            FunctionStep("capture_prematch_conclusions", step_capture),
        ]
        results = CollectorRound(steps, logger=self._slog).run_all(ctx)
        # 暴露本轮逐步结果, 供运维/测试断言 (如 "persist 失败 → live_flip 仍执行")
        self._last_round_results = results
        return int(ctx.get("ok", 0) or 0)

    def _refresh_focus_from_file(self):
        """读取 --focus-file 指定的 JSON, 把 match_keys 合并进 self.focus_match_keys。

        文件格式: {"match_keys": ["主队 vs 客队", ...], "ttl_seconds": 60}
        合并语义: focus_match_keys = 初始CLI种子 | 文件当前内容 (覆盖式合并, 不累积历史
        已滚出列表的 match_key, 因为 bridge 每次 POST 都是覆盖写当前可见集合).
        文件不存在 / JSON 损坏 / 字段缺失 → 静默沿用当前 focus, 不抛异常(不影响采集).
        防护: 文件大小上限 256KB, match_keys 上限 50, 单 key 上限 200 字符.
        优化: 文件 mtime 未变化时直接跳过, 避免每秒级 fast 轮都走 IO/解析.
        """
        if not self.focus_file:
            return
        try:
            # 防异常大文件导致内存/解析耗尽
            sz = os.path.getsize(self.focus_file)
            if sz > 256 * 1024:
                self.log(f"[focus-file] too large ({sz} bytes), skip")
                return
            mtime = os.path.getmtime(self.focus_file)
            # mtime 未变 && 之前成功解析过 → 无需重复读取
            if mtime == self._focus_file_mtime and self._focus_file_mtime > 0:
                return
            with open(self.focus_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                self.log("[focus-file] invalid JSON root, skip")
                return
            keys = data.get("match_keys") or []
            if not isinstance(keys, list):
                self.log("[focus-file] match_keys not list, skip")
                return
            ttl = data.get("ttl_seconds", 300)
            try:
                ttl = int(ttl)
            except Exception:
                ttl = 300
            ttl = max(300, min(86400, ttl))
            new_keys = set()
            for k in keys:
                s = str(k).strip()
                if s and len(s) <= 200:
                    new_keys.add(s)
                if len(new_keys) >= 50:
                    break
            with self._focus_lock:
                self.focus_match_keys = self._seed_focus_match_keys | new_keys
                self._focus_file_mtime = mtime
                self._focus_ttl_seconds = ttl
            self.log(f"[focus-file] merged {len(new_keys)} keys from {self.focus_file}")
        except Exception as e:
            # 静默: 文件缺失/损坏不应中断采集流程, 但记录以便排查
            self.log(f"[focus-file] read failed {self.focus_file}: {e}")

    def _focus_file_expired(self) -> bool:
        """focus-file 是否已过期; 无 focus-file 时不过期."""
        if not self.focus_file or self._focus_file_mtime <= 0:
            return False
        return time.time() - self._focus_file_mtime > self._focus_ttl_seconds

    def _refresh_focus_mids_from_db(self):
        """把 focus_match_keys 解析成 mid, 并构造 it 缓存供 fast_round 使用。

        采用**重建语义**: 每次刷新先清空文件派生的 focus_mids/_focus_items,
        再按当前 focus_match_keys 重新查 DB 构建, 避免滚出列表的 mid 永久累积.
        优化:
        - focus_mids 非空且 30s 内已刷新过 → 直接复用, 避免被全量轮 DB 写锁阻塞.
        - DB 连接 timeout 仅 2s, 拿不到锁立刻放弃本轮, 下轮再试.
        - 单条 IN 查询替换逐条查询, 减少锁竞争.
        """
        # 先读 focus-file (可能填充/更新 focus_match_keys), 再解析 mid
        self._refresh_focus_from_file()
        # 文件过期 → 焦点回落到 CLI 种子集合
        if self._focus_file_expired():
            with self._focus_lock:
                self.focus_mids.clear()
                self._focus_items.clear()
                self.focus_match_keys = set(self._seed_focus_match_keys)
        now = time.time()
        # 热点路径: 有焦点 mid 且 30s 内已刷新, 不复查 DB
        if self.focus_mids and (now - self._last_focus_refresh) < 30:
            return
        if not self.focus_match_keys:
            with self._focus_lock:
                self.focus_mids.clear()
                self._focus_items.clear()
            self._last_focus_refresh = now
            return
        import sqlite3
        try:
            # 统一 DB 连接: timeout=30 + busy_timeout 排队, 不再因 2s 超时丢单场
            con = sqlite3.connect(DB_PATH, timeout=30)
            con.execute("PRAGMA busy_timeout=30000")
            con.row_factory = sqlite3.Row
            try:
                keys = list(self.focus_match_keys)
                placeholders = ','.join('?' * len(keys))
                sql = f"SELECT mid, kickoff, league, match_key FROM matches WHERE match_key IN ({placeholders}) LIMIT 50"
                rows_raw = con.execute(sql, tuple(keys)).fetchall()
                rows = []
                for r in rows_raw:
                    mid = str(r["mid"] or "")
                    if not mid:
                        continue
                    mgt_ms = 0
                    try:
                        ko = r["kickoff"]
                        if ko:
                            dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
                            mgt_ms = int(dt.timestamp() * 1000)
                    except Exception:
                        mgt_ms = 0
                    rows.append((mid, mgt_ms, r["league"] or ""))
            finally:
                con.close()
            with self._focus_lock:
                self.focus_mids.clear()
                self._focus_items.clear()
                for mid, mgt_ms, league in rows:
                    self.focus_mids.add(mid)
                    self._focus_items[mid] = {
                        "mid": mid,
                        "mgt": mgt_ms,
                        "tn": league,
                    }
            self._last_focus_refresh = time.time()
        except Exception as e:
            # DB 被锁 / 超时 / 其他异常 → 记录并下轮再试; 若已有 focus_mids 则继续复用
            self.log(f"[focus] DB刷新失败(可能全量轮锁DB): {e}")

    def _auto_focus_live(self):
        """自动把活跃进行中的比赛加入焦点列表, 复用已有 {fast_interval_sec}s 焦点轮, 压缩比分滞后窗口.
        仅当 self.auto_focus=True 时生效(默认开, 可用 --no-auto-focus 关闭).
        封顶: 自动 live 最多 25 场, 合并种子后总上限 40, 避免焦点轮过载触发乐鱼风控."""
        if not self.auto_focus:
            return
        try:
            import sqlite3
            con = sqlite3.connect(DB_PATH, timeout=30)
            con.execute("PRAGMA busy_timeout=30000")
            con.row_factory = sqlite3.Row
            try:
                # 进行中: minute 在 (1,90), 按最近活跃(last_seen)排序取前 25
                rows = con.execute(
                    "SELECT match_key FROM matches WHERE minute>=1 AND minute<90 "
                    "ORDER BY last_seen DESC LIMIT 25"
                ).fetchall()
                live_keys = [r["match_key"] for r in rows if r["match_key"]]
            finally:
                con.close()
            with self._focus_lock:
                merged = set(self._seed_focus_match_keys) | set(live_keys)
                if len(merged) > 40:
                    merged = set(list(merged)[:40])
                self.focus_match_keys = merged
            if live_keys:
                self.log(f"[auto_focus] 自动聚焦 {len(live_keys)} 场活跃比赛 (焦点轮 {self.fast_interval_sec}s)")
        except Exception as e:
            self.log(f"[auto_focus] 失败: {e}")

    def fast_round(self) -> int:
        """秒级焦点轮询: 只采集 focus_mids 的单场赔率, 不写 sweep/cs/conclusion。

        与 collect_round 解耦, 避免全量轮的沉重附加操作拖慢 3-5 秒节奏.
        硬保护: 单轮耗时不超过 max(2*fast_interval, 6)s, 超时立即中断, 防止被慢请求/DB锁拖死.
        """
        # 若配置了 focus_file, 即使初始 focus_match_keys 为空, 也应先读文件解析.
        has_focus_source = bool(self.focus_mids or self.focus_match_keys or self.focus_file)
        if not has_focus_source:
            return 0
        # 30s 刷新一次 或 尚无焦点 mid 但配置了文件 → 尝试刷新
        if (time.time() - self._last_focus_refresh >= 30) or (not self.focus_mids and self.focus_file):
            self._refresh_focus_mids_from_db()
        if not self.focus_mids:
            return 0
        ok = 0
        with self._focus_lock:
            focus_mids = sorted(self.focus_mids)
            focus_items = dict(self._focus_items)
        self.log(f"[fast] 轮询 {len(focus_mids)} 个焦点 mid")
        round_start = time.time()
        max_round_sec = max(self.fast_interval_sec * 2, 6)
        for idx, mid in enumerate(focus_mids):
            if time.time() - round_start > max_round_sec:
                self.log(f"[fast] 本轮已耗时 {max_round_sec}s, 中断剩余 {len(focus_mids) - idx} 场")
                break
            try:
                # fetch 硬保护 3s: 防止网络 hang 拖死整轮
                decoded, fetch_timeout = _call_with_timeout(fetch_match_odds, (mid,), timeout_sec=3.0)
                if fetch_timeout:
                    self.log(f"fast mid={mid} fetch timeout, skip")
                    continue
                if decoded is None:
                    continue
                it = focus_items.get(mid)
                # record 硬保护 8s: db.conn() 已设 busy_timeout=30s, 写会排队等锁;
                # 旧值 2s 会在全量轮写锁竞争中误杀单场快照 → 抬高到 8s 让写完成(不再丢单场)
                key, record_timeout = _call_with_timeout(record_match_odds, (decoded, it), timeout_sec=8.0)
                if record_timeout:
                    self.log(f"fast mid={mid} record timeout(DB lock?), skip")
                    continue
                if key:
                    ok += 1
            except Exception as e:
                self.log(f"fast mid={mid} 失败: {e}")
                continue
            # 剩余时间不够完成下一场时, 提前退出而不是 sleep 后超时
            if time.time() - round_start + max(self.sleep_min, 0.05) > max_round_sec:
                break
            time.sleep(random.uniform(self.sleep_min, self.sleep_max))
        return ok

    def _fast_loop(self):
        """守护线程: 持续秒级采集焦点比赛, 与全量轮并行。"""
        self.log("[fast] 秒级焦点线程已启动")
        while self._fast_running:
            try:
                if _FENG_KONG_UNTIL > time.time():
                    time.sleep(5)
                    continue
                n = self.fast_round()
                if n:
                    self.log(f"fast {n}场")
            except Exception as e:
                self.log(f"fast循环异常: {e}")
                traceback.print_exc()
            time.sleep(self.fast_interval_sec)

    def _kickoff_probe_loop(self):
        """守护线程: 每场 scheduled 比赛在开赛(kickoff)+60s 精确拉取一次开场数据。

        业务动机(2026-08-18 用户): 滚球模型需要"开场盘口基线"(t≈1min 锚点, 漂移特征
        的对照原点)。常规全量轮 45s 周期 + 每轮最多 200 场串行拉取, 轮到某场时可能
        已过数分钟, 错过开场瞬态盘口。此线程保证 kickoff+1min 有一个精确数据点。

        机制: 每 20s 扫 matches 表 status='scheduled' 的场;
        kickoff+60s <= now < kickoff+600s 且未拉过 → fetch_match_odds+record_match_odds。
        只有成功写入才标记已拉取(_kickoff_probed), 失败下轮(20s 后)重试, 窗口
        (kickoff+10min) 过期后放弃。风控冷却期间暂停。日志一律 ASCII。
        """
        self.log("[kickoff-probe] 开赛+1min精确拉取线程已启动")
        while self._fast_running:
            try:
                if _FENG_KONG_UNTIL > time.time():
                    time.sleep(10)
                    continue
                now = time.time()
                import sqlite3 as _sq
                conn_db = _sq.connect(DB_PATH, timeout=30)
                conn_db.execute("PRAGMA busy_timeout=30000")
                rows = conn_db.execute(
                    "SELECT match_key, mid, home, away, league, kickoff "
                    "FROM matches WHERE status='scheduled' AND mid IS NOT NULL AND mid != ''"
                ).fetchall()
                conn_db.close()
                for mk, mid, home, away, league, kickoff in rows:
                    if mk in self._kickoff_probed:
                        continue
                    try:
                        ko_ts = datetime.strptime(str(kickoff)[:16], "%Y-%m-%d %H:%M").replace(
                            tzinfo=_TZ8).timestamp()
                    except Exception:
                        continue
                    # 窗口: 开赛后 60s ~ 600s
                    if not (ko_ts + 60 <= now < ko_ts + 600):
                        continue
                    try:
                        decoded = fetch_match_odds(mid)
                        if not decoded:
                            continue
                        it = {"mid": mid, "mgt": ko_ts * 1000.0, "tn": league or "",
                              "mhn": home or "", "man": away or ""}
                        key = record_match_odds(decoded, it)
                        if key:
                            self._kickoff_probed.add(mk)
                            self.log(f"[kickoff-probe] {mk} 开赛+{int(now-ko_ts)}s 开场数据已拉取")
                    except Exception as e:
                        self.log(f"[kickoff-probe] {mk} 拉取失败(下轮重试): {e}")
                    time.sleep(random.uniform(self.sleep_min, self.sleep_max))
            except Exception as e:
                self.log(f"[kickoff-probe] 循环异常: {e}")
            time.sleep(20)

    def _freeze_scheduled_cs(self):
        """冻结所有未开赛(scheduled)比赛的赛前波胆(CS)盘口。

        铁律: 只采未开赛比赛, 已开赛(live/finished)不调 freeze_pre_match_cs
        (该函数内部也二次校验 status='scheduled')。每轮刷新, 临开赛前拿到的
        即最接近开盘的赛前盘口。
        """
        try:
            import sqlite3
            conn_db = sqlite3.connect(DB_PATH, timeout=30)
            conn_db.execute("PRAGMA busy_timeout=30000")
            sched = conn_db.execute(
                "SELECT match_key FROM matches WHERE status='scheduled'").fetchall()
            conn_db.close()
            n = 0
            for (mk,) in sched:
                try:
                    if freeze_pre_match_cs(mk):
                        n += 1
                except Exception:
                    continue
            if n:
                self.log(f"[CS冻结] 已冻结/刷新 {n} 场未开赛赛前波胆盘口")
        except Exception as e:
            self.log(f"[CS冻结] 扫描异常: {e}")

    def _capture_prematch_conclusions(self):
        """赛前冻结捕获: 对所有"有赛前盘口但未固化"的未开赛比赛, 跑一次 KNN 赛前结论并
        固化进 prematch_conclusion —— 让赛后机关对"全部有赛前盘口的比赛"零遗漏, 无需回退
        KNN 兜底 (与 _freeze_scheduled_cs 同构, 都是"赛前只采一次")。

        仅 status='scheduled' + 非虚拟联赛; 已固化(match_key∈prematch_conclusion)的跳过,
        故每场只算一次 KNN; 无赛前快照的比赛 query_match 返回 not applicable → 不下写、下轮再试。
        """
        import sqlite3
        try:
            from gq.db import (store_prematch_conclusion, is_virtual_league, conn)
            from pipeline.prematch_similarity import query_match, DEFAULT_K
            with conn() as c:
                c.row_factory = sqlite3.Row
                sched = c.execute(
                    "SELECT match_key, league FROM matches WHERE status='scheduled'"
                ).fetchall()
                captured = {r["match_key"] for r in c.execute(
                    "SELECT match_key FROM prematch_conclusion").fetchall()}
            n = 0
            for row in sched:
                mk = row["match_key"]
                if mk in captured:          # 已固化 → 跳过 (每场只算一次)
                    continue
                if is_virtual_league(row["league"]):   # 虚拟联赛跳过
                    continue
                try:
                    r = query_match(mk, k=DEFAULT_K, draw_upgrade=True)
                except Exception:
                    continue
                if not r.get('applicable'):
                    continue
                try:
                    store_prematch_conclusion(
                        mk, r['verdict'], r['verdict_cn'], r.get('excess'),
                        r.get('roi'), int(r.get('draw_alert') or 0))
                    n += 1
                except Exception:
                    continue
            if n:
                self.log(f"[赛前固化] 已固化 {n} 场未开赛赛前结论 (机关零遗漏)")
        except Exception as e:
            self.log(f"[赛前固化] 扫描异常: {e}")

    def _try_recover_score(self, mid):
        """GQ 保留窗口内重拉单场终比分 (治本: 修复 finished 但 score 丢失).

        主路径在 decoded=None(乐鱼源 purge/缺失)时只更 status/minute 不捕比分,
        导致比赛被标 finished 却 score=NULL 且永不归档. 本函数在库存兜底扫描中
        对已 finished 丢比分的比赛尝试重拉(仅近期、GQ 未 purge 时有效).
        返回 (sh, sa, ht_sh, ht_sa) 或 None(失败/超时/无 msc/已 purge).
        """
        try:
            items = fetch_match_structure([mid])
            for m in items:
                if not isinstance(m, dict):
                    continue
                sh, sa, ht_sh, ht_sa = _score_from_msc(m.get("msc"))
                if sh is not None and sa is not None:
                    return sh, sa, ht_sh, ht_sa
        except Exception as e:
            self.log(f"[库存兜底-恢复] 重拉异常 mid={mid}: {e}")
        return None

    def _sweep_finished(self):
        """扫描 events.db 中 status='live' 但已实质性结束/未开赛的比赛 → 修正状态 → 归档初盘。

        WS2 修正:
          - 取下 `score_home IS NOT NULL` 限制: 僵尸可能无比分, 仍需清状态
          - 未来开赛却标 live → 纠正为 scheduled (覆盖 180 场假 live)
          - 开赛超 3.5h → 强制 finished (兼容延迟/加时/点球真活比赛), 替代失效的
            `sc==0 and minute<90` 守卫(该守卫因 minute 恒=45 而永真, 僵尸永不归档)
                - 仅 sc>0 (真实进球) 才归档赛果, 避免把卡 45:00 的 0-0 僵尸写成假终场;
                  0-0/无比分僵尸仅清状态, 赛果留人工/复核
        """
        import sqlite3
        # 事故②尾巴闭环: 写路径委托单写者 gq.db.conn() (core.db_manager 单写者),
        # 杜绝裸 sqlite3.connect(DB_PATH) 抢写锁 → 坏页根因. live 翻页写仍真实执行.
        _sweep_entered = False
        _sweep_ctx = _gq_single_writer()
        try:
            conn_db = _sweep_ctx.__enter__()
            _sweep_entered = True
            conn_db.row_factory = sqlite3.Row
            now_s = time.time()
            rows = conn_db.execute("""
                SELECT match_key, home, away, league, kickoff, score_home, score_away, mid, minute, last_seen
                FROM matches
                WHERE status = 'live'
                   OR (status = 'finished' AND score_home IS NULL AND mid IS NOT NULL)
            """).fetchall()

            count = 0
            sched_count = 0
            for row in rows:
                # 年龄判定: 优先用 kickoff(开赛时长), 解析失败则回退 last_seen(数据停滞时长).
                age = None
                if row["kickoff"]:
                    try:
                        kt = datetime.strptime(row["kickoff"][:16], "%Y-%m-%d %H:%M")
                        kt_utc = kt.replace(tzinfo=timezone(timedelta(hours=8)))
                        age = now_s - kt_utc.timestamp()
                    except Exception:
                        age = None
                if age is None and row["last_seen"]:
                    age = now_s - row["last_seen"]
                if age is None:
                    continue

                # 未来开赛却标 live → 纠正为 scheduled
                if age < 0:
                    upsert_match(row["match_key"], row["home"], row["away"],
                                 row["league"], kickoff=row["kickoff"],
                                 status="scheduled",
                                 score_home=row["score_home"],
                                 score_away=row["score_away"], minute=0)
                    sched_count += 1
                    continue

                # 未满3.5h 的 live 仍可能进行中(含延迟/加时/点球) → 跳过;
                # 但"已 finished 却丢比分"的僵尸必须尝试恢复, 不走此跳过.
                # 2026-08-29 修正 (IR-06 僵尸 live 根治):
                #   原判据是 `age >= 3.5h` 单条件 —— 刚结束的比赛要干等 3.5 小时才归档。
                #   实测 3 场僵尸 age=3.35h 差 9 分钟卡着不动, 而 minute=110/116/121
                #   比分早已定(3-1/4-3/1-1), 前端一直当"进行中"展示。
                #   新增早判: **age >= 2.5h 且 minute >= 90** → finished。
                #     · minute>=90 是可信终场信号: resolve_true_minute 明确"feed 报 >90
                #       是真实递增值(补时), 直接采信"; 且 _status_minute 现已写入真实分钟。
                #     · 2.5h 覆盖加时+点球(最长约 135min), 不会误杀真活比赛。
                #   两条件**同时**满足才翻; 任一不满足仍走 3.5h 兜底 —— 宁慢不误判。
                is_finished_null = (row["status"] == "finished"
                                    and row["score_home"] is None
                                    and row["score_away"] is None
                                    and row["mid"])
                if not is_finished_null and age < 3.5 * 3600:
                    _mn = row["minute"]
                    _early_done = (age >= 2.5 * 3600 and _mn is not None and int(_mn) >= 90)
                    if not _early_done:
                        continue

                # ── 治本: 已 finished 但 score 丢失(主路径 decoded=None 时只更状态未捕比分) ──
                # 在 GQ 保留窗口内重拉终比分, 避免永久丢失(状态-比分耦合 bug 修复).
                sh = row["score_home"]
                sa = row["score_away"]
                mid = row["mid"]
                if (sh is None or sa is None) and mid and age is not None and age < 2 * 86400:
                    rec = self._try_recover_score(mid)
                    if rec:
                        sh, sa, _ht_sh, _ht_sa = rec
                        self.log(f"[库存兜底-恢复] {row['home']} vs {row['away']} 重拉到比分 {sh}-{sa}")
                    else:
                        # GQ 已 purge 或超窗口 → 不可恢复, 留人工/接受丢失, 跳过归档
                        continue
                elif (sh is None or sa is None):
                    # 无比分且不在窗口内(或无可恢复) → 跳过, 不伪造赛果
                    continue

                # 开赛超 3.5h(或数据停滞超 3.5h) → 强制已结束
                upsert_match(row["match_key"], row["home"], row["away"],
                             row["league"], kickoff=row["kickoff"],
                             status="finished",
                             score_home=sh, score_away=sa, minute=90)

                # 归档初盘→赛果: 有终比分即写(含 0-0, 属真实赛果); score 仍为空才跳过(真丢失)
                if mid and sh is not None and sa is not None:
                    try:
                        outcome = record_match_outcome(
                            mid, row["home"], row["away"], row["league"],
                            kickoff=row["kickoff"],
                            score_home=sh,
                            score_away=sa,
                            match_key_override=row["match_key"])
                        if outcome:
                            count += 1
                            self.log(
                                f"初盘归档: {row['home']} vs {row['away']} "
                                f"{sh}-{sa} "
                                f"[{outcome['result']}] type={outcome['odds_type']}")
                            # ── 2026-08-30: 分析快照赛果回填 (用户指令: 记录分析→结合赛果回训) ──
                            #   归档赛果时同步 resolve analysis_snapshot, 标注方向/比分命中。
                            try:
                                from pipeline.analysis_snapshot import resolve_snapshot as _res_snap
                                _n_res = _res_snap(conn_db, row["match_key"])
                                if _n_res:
                                    self.log(f"[分析快照回填] {row['home']} vs {row['away']} "
                                             f"解析 {_n_res} 条分析")
                            except Exception as _rse:
                                self.log(f"[分析快照回填] 失败(不影响归档): {_rse}")
                            # ── 赛后机关: 比赛落入复盘即触发自动复核 (事件驱动, 代替每日批量) ──
                            # 优先读赛前固化结论(严格忠于展示); 无则赛后重跑 KNN 兜底。
                            try:
                                rev = auto_review_match(row["match_key"])
                                if rev and rev.get('verdict_hit'):
                                    self.log(
                                        f"[自动复核] {row['home']} vs {row['away']} "
                                        f"预测={rev.get('predicted_direction')} "
                                        f"实际={outcome['result']} {rev.get('verdict_hit')} "
                                        f"({rev.get('predicted_from')})")
                            except Exception:
                                pass
                            # 赛前波胆(CS)验证: 取赛前盘口按实际比分归档
                            try:
                                vr = verify_cs(row["match_key"], source='live')
                                if vr:
                                    self.log(f"[CS验证] {row['home']} vs {row['away']} "
                                             f"实际{vr['actual']} 赛前赔{vr['actual_odds']} "
                                             f"命中={vr['hit']} 热门命中={vr['fav_hit']}")
                            except Exception:
                                pass
                    except Exception:
                        pass
            if count or sched_count:
                self.log(f"库存兜底: 归档 {count} 场, 纠正未开赛 {sched_count} 场")

            # 安全网(scheduled→live/finished)抽到 _sweep_scheduled(), 在 finally 中调用,
            # 确保无论主循环是否异常, 状态纠正都必定执行(防御性, 修复"开赛不显示").
        except Exception as e:
            self.log(f"库存兜底异常: {e}")
        finally:
            # 状态纠正安全网: 必定执行(主循环异常也不跳过), 独立连接不影响上方事务
            try:
                self._sweep_scheduled()
            except Exception as e:
                self.log(f"[库存兜底-安全网] 异常: {e}")
            # 单写者上下文退出: 仅最外层提交并归还写连接(嵌套可重入, RLock 保护)
            if _sweep_entered:
                try:
                    _sweep_ctx.__exit__(None, None, None)
                except Exception:
                    pass

    def _sweep_scheduled(self):
        """库存兜底安全网: 扫描 status='scheduled' 且 kickoff 已过的比赛 → 翻 live / finished.

        独立于主循环(在 _sweep_finished 的 finally 中调用), 即使主循环抛异常也必定执行.
        - 开赛 2min~3.5h: 翻 live (进行中, 详情路径偶发漏掉的兜底; 兼容延迟/加时)
        - 开赛超 3.5h 仍 scheduled: 真实早已结束, 翻 finished (不伪造比分, 沿用既有 score)
        - age<=2min(含未来): 不动, 留给详情路径/_status_minute
        upsert_match 内部尊重 is_override, 人工纠偏锁的比赛不会被覆盖.
        """
        import sqlite3
        try:
            conn_db = sqlite3.connect(DB_PATH, timeout=30)
            conn_db.execute("PRAGMA busy_timeout=30000")
            conn_db.row_factory = sqlite3.Row
            now_s = time.time()
            rows = conn_db.execute("""
                SELECT match_key, home, away, league, kickoff, score_home, score_away, minute
                FROM matches
                WHERE status = 'scheduled' AND kickoff IS NOT NULL AND kickoff != ''
            """).fetchall()
            live_flipped = 0
            finished_flipped = 0
            for row in rows:
                try:
                    kt = datetime.strptime(row["kickoff"][:16], "%Y-%m-%d %H:%M")
                    kt_utc = kt.replace(tzinfo=timezone(timedelta(hours=8)))
                    age = now_s - kt_utc.timestamp()
                except Exception:
                    continue
                if age <= 2 * 60:
                    # 刚开赛(<2min)或未来开赛(age<=0): 留给详情路径/_status_minute, 保持 scheduled
                    continue
                if age < 3.5 * 3600:
                    # 开赛 2min~3.5h: 翻 live (进行中, 详情路径偶发漏掉的兜底; 兼容延迟/加时)
                    upsert_match(row["match_key"], row["home"], row["away"], row["league"],
                                 kickoff=row["kickoff"], status="live",
                                 score_home=row["score_home"], score_away=row["score_away"],
                                 minute=min(125, max(0, int(age / 60))))
                    live_flipped += 1
                else:
                    # 开赛超 3.5h 仍 scheduled: 真实早已结束, 翻 finished.
                    # 根因: 旧 _sweep_finished 只扫 status='live', scheduled 老比赛永不进归档 → 孤儿堆积
                    # (实测 456 场 week-old 比赛卡在 scheduled 不显示). 不伪造比分, 沿用既有 score.
                    upsert_match(row["match_key"], row["home"], row["away"], row["league"],
                                 kickoff=row["kickoff"], status="finished",
                                 score_home=row["score_home"], score_away=row["score_away"],
                                 minute=90)
                    finished_flipped += 1
            if live_flipped or finished_flipped:
                self.log(f"库存兜底: 翻 live {live_flipped} 场, 翻 finished {finished_flipped} 场 "
                         f"(scheduled→已开赛/已结束)")
        except Exception as e:
            self.log(f"[库存兜底-安全网] 异常: {e}")
        finally:
            try:
                conn_db.close()
            except Exception:
                pass

    def run_once(self):
        init_db()
        ensure_cs_tables()
        # 事故④ 根治: 入口装 UTF-8 护栏 (幂等, 绝不抛); 后续所有日志含中文/emoji 不再崩
        install_utf8()
        self.log("=== 单次采集 (纯HTTP) ===")
        self.log(f"REQUEST_ID={_get_request_id()[:8]}... env覆盖={'GQ_REQUEST_ID' in os.environ}")
        n = self.collect_round()
        s = db_stats()
        self.log(f"本轮回写 {n} 场 | DB: {s['matches']}M / {s['snapshots']}S / {s['changes']}C")

    def run_daemon(self, dur_min: int = 0):
        init_db()
        ensure_cs_tables()
        # 事故④ 根治: 入口装 UTF-8 护栏 (幂等, 绝不抛); 后续所有日志含中文/emoji 不再崩
        install_utf8()
        self.log(f"=== 守护 (纯HTTP, 全量{self.interval_sec}s / 焦点{self.fast_interval_sec}s) ===")
        t0 = time.time()
        last_full = 0.0
        last_auto_focus = 0.0

        # 启动秒级焦点采集线程(与全量轮并行, 避免全量轮阻塞焦点)
        self._fast_running = True
        # 自动聚焦: 先把活跃比赛塞进 focus 列表(若有), 再启动焦点线程
        if self.auto_focus:
            self._auto_focus_live()
        if self.focus_mids or self.focus_match_keys or self.focus_file:
            threading.Thread(target=self._fast_loop, daemon=True).start()

        # 开赛+1min 精确拉取线程(无条件启动): 每场 scheduled 在 kickoff+60s 拉开场盘口
        threading.Thread(target=self._kickoff_probe_loop, daemon=True).start()

        try:
            while True:
                # ── 风控冷却检查 ──
                if _FENG_KONG_UNTIL > time.time():
                    left = int(_FENG_KONG_UNTIL - time.time())
                    if self._iter % 6 == 0:
                        self.log(f"[风控冷却] 还需 {left}s")
                    time.sleep(min(60, max(5, left)))
                    continue

                now = time.time()

                # ── 全量轮询 ──
                if now - last_full >= self.interval_sec:
                    last_full = now
                    self._iter += 1
                    self.log(f"--- # {self._iter} ---")
                    try:
                        n = self.collect_round()
                        s = db_stats()
                        self.log(f"{n}场 | DB: {s['snapshots']}S / {s['changes']}C")
                    except Exception as e:
                        self.log(f"异常: {e}")
                        traceback.print_exc()

                time.sleep(1.0)

                # ── 自动聚焦活跃比赛(每 60s) ──
                if self.auto_focus and now - last_auto_focus >= 60:
                    last_auto_focus = now
                    self._auto_focus_live()

                if dur_min > 0 and time.time() - t0 >= dur_min * 60:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self._fast_running = False
            s = db_stats()
            self.log(f"结束: {s['matches']}M / {s['snapshots']}S / {s['changes']}C")


# ── CLI ──
def main():
    # 单实例锁: 已有采集器在跑则直接退出 (防止双写/双API负载)
    if not _acquire_singleton():
        safe_print("[singleton] 已有采集器实例在运行, 本进程退出 (避免重复采集)")
        sys.exit(0)

    # 事故④ 根治: 入口装 UTF-8 护栏 (幂等, 绝不抛); 裸 print 含中文/emoji 不再崩
    install_utf8()

    ap = argparse.ArgumentParser(description="GQ 全市场采集器 (纯HTTP v3.0)")
    ap.add_argument("--once", action="store_true", help="单次采集")
    ap.add_argument("--daemon", action="store_true", help="守护模式")
    ap.add_argument("-i", "--interval", type=int, default=45, help="轮询间隔秒 (默认45)")
    ap.add_argument("--max-per-round", type=int, default=200,
                    help="每轮最多采集场数 (默认200, 足以容纳全量; 设0=不限)")
    ap.add_argument("-d", "--duration", type=int, default=0, help="守护运行时长(分钟), 0=无限")
    ap.add_argument("--fast-interval", type=int, default=5, help="焦点比赛轮询间隔秒 (默认5)")
    ap.add_argument("--focus-mids", type=str, default="",
                    help="焦点 mid 列表, 逗号分隔; 这些比赛会单独秒级轮询")
    ap.add_argument("--focus-match-keys", type=str, default="",
                    help="焦点 match_key 列表, 逗号分隔; 采集器会从 DB 解析 mid 并秒级轮询")
    ap.add_argument("--focus-file", type=str, default="",
                    help="焦点 match_key JSON 文件(含 match_keys 列表); 采集器每30s读取并合并为秒级焦点")
    ap.add_argument("--no-auto-focus", action="store_true",
                    help="关闭自动聚焦活跃比赛(默认开启: 每60s把进行中比赛塞入5s焦点轮, 压缩比分滞后)")
    ap.add_argument("--visible", action="store_true", help="兼容旧CLI, 纯HTTP下无效")
    ap.add_argument("--stats", action="store_true", help="查看全局统计")
    ap.add_argument("--outcomes", action="store_true", help="查看初盘→赛果对照统计")
    ap.add_argument("--no-detail", action="store_true", help="兼容旧CLI, 纯HTTP下无效")
    args = ap.parse_args()

    if args.stats:
        init_db()
        safe_print(json.dumps(db_stats(), indent=2, ensure_ascii=False))
        return

    if args.outcomes:
        init_db()
        from gq.db import outcomes_stats
        safe_print(json.dumps(outcomes_stats(), indent=2, ensure_ascii=False))
        return

    focus_mids = [m.strip() for m in args.focus_mids.split(",") if m.strip()]
    focus_match_keys = [k.strip() for k in args.focus_match_keys.split(",") if k.strip()]
    c = GQCollector(interval_sec=args.interval, max_per_round=args.max_per_round,
                    fast_interval_sec=args.fast_interval,
                    focus_mids=focus_mids,
                    focus_match_keys=focus_match_keys,
                    focus_file=args.focus_file,
                    auto_focus=not args.no_auto_focus)
    if args.once:
        c.run_once()
    elif args.daemon:
        c.run_daemon(args.duration)
    else:
        ap.print_help()


if __name__ == "__main__":
    # 注意: 本机 venv 的 pythonw 是「shim 启动器」, 实际 worker 会 re-dispatch 到
    # 系统 Python312 (即「.venv shim + Python312 真身」结构, 见收尾_GQ恢复_*.md,
    # 属正常单实例, 非双写)。故**不能**在此按 sys.executable 拦截系统 Python,
    # 否则看门狗重启后 worker 被判死刑 -> 0 采集器。真正的双实例由下方
    # _acquire_singleton() 文件锁保证。
    main()
