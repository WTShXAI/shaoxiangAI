"""leisu_live.py — 微瑞/乐鱼类体育平台实时赛程 + 赔率采集器 (哨响AI v7.4 赛程数据源)

数据源: https://api.u92tiil.com/yewu11/v1/w/getMatchBaseInfoByOddsPB 
        + structureMatchBaseInfoByMidsPB (权威赔率补充)

鉴权: 不走 cookie, 走自定义 HTTP 头 (checkid + requestid).
      由于服务器做 TLS 指纹检测(JA3), 不能直接用 Python urllib,
      必须通过 Playwright 浏览器上下文发 fetch 请求.

离线模式(训练/调试): 直接调用 decode_payload 解码已有样本.
"""
import os, time, json, base64, gzip, zlib, socket, threading
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

ODDS_DIVISOR = 100000.0  # ov 整数赔率 = 十进制赔率 * 100000

# ═══ 鉴权配置 (从 deep-link URL 和 Playwright 拦截提取) ═══
TOKEN = "7d9c9d8b996f1d1d849672d0c1c512135f8d3119"
DEVICE_UUID = "c6cf3aabe2a84dd3a870d669b8ba5094"
SESSION_PREFIX = "526002076777845380"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
API_DOMAIN = "https://api.u92tiil.com"

# 乐鱼深链 api= 参数默认值(随账号/session 刷新而变, 优先从 config/leisu_session.json 读取)
API_DEFAULT = "TMZtsDWYt3GneclbQg/EJhSnImbsu44GPt3ENLuInvA="

# 本地会话文件(手动维护, 可选)
_SESSION_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "leisu_session.json")


def decode_payload(data_b64: str):
    """base64 → gzip → zlib raw deflate → zlib。成功返回 dict, 失败 None。"""
    if not data_b64:
        return None
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return None
    for fn in (lambda: gzip.decompress(raw),
               lambda: zlib.decompress(raw, -zlib.MAX_WBITS),
               lambda: zlib.decompress(raw)):
        try:
            return json.loads(fn().decode("utf-8"))
        except Exception:
            continue
    return None


# ═══ 原始结构抽取(离线/解码用) ═══

# hpid 映射: 1=全场独赢, 2=全场大小, 4=全场让球, 17=半场独赢, 18=半场大小, 19=半场让球
HPID_1X2_FULL = "1"
HPID_OU_FULL = "2"
HPID_AH_FULL = "4"
HPID_1X2_HALF = "17"
HPID_OU_HALF = "18"
HPID_AH_HALF = "19"
ALL_MARKET_HPIDS = {HPID_1X2_FULL, HPID_OU_FULL, HPID_AH_FULL,
                    HPID_1X2_HALF, HPID_OU_HALF, HPID_AH_HALF}


def _parse_ov(ov):
    if ov is None:
        return None
    try:
        return float(ov) / ODDS_DIVISOR
    except Exception:
        return None


def _extract_1x2(match: dict):
    """全场 1X2 (hpid=1)。ot 1=主, X=平, 2=客。"""
    hps_data = match.get("hpsData") or []
    oh = od = oa = None
    for block in (hps_data if isinstance(hps_data, list) else []):
        if not isinstance(block, dict):
            continue
        for h in block.get("hps", []):
            if str(h.get("hpid")) != HPID_1X2_FULL:
                continue
            for opt in h.get("hl", {}).get("ol", []):
                val = _parse_ov(opt.get("ov"))
                if val is None:
                    continue
                ot = str(opt.get("ot"))
                if ot == "1":
                    oh = val
                elif ot == "X":
                    od = val
                elif ot == "2":
                    oa = val
    return oh, od, oa


def _extract_ou(match: dict, hpid: str):
    """大小 (hpid=2 全场 或 18 半场)。返回 (line, over, under) 字符串 line e.g. '3' 或 '2.5/3'。"""
    hps_data = match.get("hpsData") or []
    for block in (hps_data if isinstance(hps_data, list) else []):
        if not isinstance(block, dict):
            continue
        for h in block.get("hps", []):
            if str(h.get("hpid")) != hpid:
                continue
            line = ""
            over = under = None
            for opt in h.get("hl", {}).get("ol", []):
                on = opt.get("on", "") or ""
                # 'on' 格式: "大 3" / "小 3/3.5" / "Over 2.5" — 提取行号
                parts = on.split(" ", 1)
                cn = parts[0] if parts else ""
                line = parts[1] if len(parts) > 1 else ""
                val = _parse_ov(opt.get("ov"))
                if val is None:
                    continue
                if cn in ("大", "Over", "O"):
                    over = val
                elif cn in ("小", "Under", "U"):
                    under = val
            if over is not None or under is not None:
                return line or "—", over, under
    return None, None, None


def _extract_ah(match: dict, hpid: str):
    """让球 (hpid=4 全场 或 19 半场)。返回 (line, home_odds, away_odds)。

    line 含义 = 主队视角盘口字符串: 正数=主队受让(主弱), 负数=主队让球(主强)。
    约定与 bridge_service._annotate_scores 一致: ah>0 主队受让, ah<0 主队让球。

    ⚠️ 关键准确度修复 (实证 12 场中 5 场 1X2 与 AH 强弱矛盾):
       Leisu 的 AH 市场 ot 编号与主客队映射不可靠 (多场 1X2 主队大热, 但 AH ot='1' 赔率反而更高),
       不能相信 ot→主客 映射, 也不能用 AH 自身赔率方向推导符号。
       → 「谁是被看好方(让球方)」以**同场 1X2 赔率为真相源** (同一庄同场比赛, 两市场必指向同一强弱)。
       → ah_home / ah_away 始终对应「主队覆盖盘口赔率」/「客队覆盖盘口赔率」:
           主队被看好 ⇒ 主队覆盖赔率更低 ⇒ ah_home=较低值; 否则 ah_home=较高值。
           这样 _live_predict 的 AH 价值层 (fav_is_home = h_odds<a_odds) 才会正确。
       → 盘口大小取两选项共有绝对盘口值 (互为镜像); 符号由 1X2 方向定。
    """
    hps_data = match.get("hpsData") or []
    # 1X2 方向 (ground truth for favorite)
    try:
        xh, _xd, xa = _extract_1x2(match)
    except Exception:
        xh = _xd = xa = None
    home_favored = None
    if xh is not None and xa is not None and xh > 0 and xa > 0:
        if abs(xh - xa) < 1e-6:
            home_favored = None
        else:
            home_favored = xh < xa
    for block in (hps_data if isinstance(hps_data, list) else []):
        if not isinstance(block, dict):
            continue
        for h in block.get("hps", []):
            if str(h.get("hpid")) != hpid:
                continue
            opts = []
            for opt in h.get("hl", {}).get("ol", []):
                on = opt.get("on", "") or ""
                val = _parse_ov(opt.get("ov"))
                if val is None:
                    continue
                opts.append((on, val))
            if not opts:
                continue
            # 盘口大小: 取两选项共有绝对盘口值 (互为镜像, 大小一致)
            base = None
            for on, _ in opts:
                for tok in str(on).replace("/", " ").split():
                    try:
                        base = abs(float(tok.lstrip("+").lstrip("-")))
                        break
                    except ValueError:
                        continue
                if base is not None:
                    break
            if base is None:
                base = 0.0
            # 两选项赔率 (不依赖 ot 映射)
            o_low = min(v for _, v in opts)
            o_high = max(v for _, v in opts)
            # 主队覆盖赔率: 主队被看好→更低; 否则更高
            if home_favored is True:
                ah_home, ah_away = o_low, o_high
            elif home_favored is False:
                ah_home, ah_away = o_high, o_low
            else:
                # 无法定方向 (无 1X2): 维持 ot 顺序兜底
                ah_home = opts[0][1]
                ah_away = opts[1][1] if len(opts) > 1 else None
            # line 字符串: 保留 split 格式, 符号由 1X2 方向定
            raw_on = opts[0][0] if opts else "0"
            bstr = str(raw_on).lstrip("+").lstrip("-")
            if home_favored is True:
                line = "-" + bstr       # 主队让球 (主强)
            elif home_favored is False:
                line = "+" + bstr       # 主队受让 (主弱)
            else:
                line = bstr             # 平手/无法定方向
            return line, ah_home, ah_away
    return None, None, None


def _extract_half_1x2(match: dict):
    """半场 1X2 (hpid=17)。"""
    hps_data = match.get("hpsData") or []
    oh = od = oa = None
    for block in (hps_data if isinstance(hps_data, list) else []):
        if not isinstance(block, dict):
            continue
        for h in block.get("hps", []):
            if str(h.get("hpid")) != HPID_1X2_HALF:
                continue
            for opt in h.get("hl", {}).get("ol", []):
                val = _parse_ov(opt.get("ov"))
                if val is None:
                    continue
                ot = str(opt.get("ot"))
                if ot == "1":
                    oh = val
                elif ot == "X":
                    od = val
                elif ot == "2":
                    oa = val
    return oh, od, oa


def _iso_from_ms(mgt):
    try:
        return __import__("datetime").datetime.fromtimestamp(
            int(mgt) / 1000.0, __import__("datetime").timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _score_from_msc_leisu(msc):
    """Leisu structure/match 的 msc 比分时间线 → (home, away) 整数元组。

    msc 是多条 'S{code}|H:A' 字符串, 其中 S0 表示当前全场比分.
    解析失败返回 (None, None). 为兼容 GQ 风格的 msc, 如果 S0 不存在,
    回退到末项解析.
    """
    if not msc or not isinstance(msc, list):
        return None, None
    try:
        for item in msc:
            if not isinstance(item, str):
                continue
            if item.startswith("S0|"):
                s = item.split("|", 1)[1]
                h, a = s.split(":")
                return int(h), int(a)
        # 兜底: 末项
        last = str(msc[-1])
        if "|" in last:
            s = last.split("|", 1)[1]
        else:
            s = last
        h, a = s.split(":")
        return int(h), int(a)
    except Exception:
        return None, None


def _match_minute_from_msc(msc):
    """Leisu msc 中 S8 表示当前比赛时间, e.g. 'S8|74:26' → '74:26'。
    解析失败返回 None.
    """
    if not msc or not isinstance(msc, list):
        return None
    for item in msc:
        if isinstance(item, str) and item.startswith("S8|"):
            return item.split("|", 1)[1]
    return None


def _match_status_from_ms(ms, mststi=None, mprmc=None, elapsed_min=None, match_minute=None):
    """把 Leisu 原始状态映射到标准化的 match_state.

    标准化: 0=未开赛, 1=上半场, 2=中场, 3=下半场, 4=加时, 5=点球, -1=已结束,
            >=6=异常(中断/延期等).
    观察:
        ms=1   → 进行中
        ms=110 → 未开赛
        mststi 在 structure 里经常不可靠(0/4/6 同时出现), 不再直接映射.
        优先用 S8 比赛时间判断上下半/加时/中场.
    """
    try:
        ms = int(ms) if ms is not None else 0
    except Exception:
        ms = 0
    if ms == 1:
        # 终场兜底(修BUG#1, 2026-07-29): 乐鱼 ms 终场后不回落,
        # 比赛含中场+补时+加时通常≤130min, 超过则强制判已结束, 防止永远LIVE.
        # (加时赛到120+点球, 130min阈值已含常规+加时; 点球阶段 match_minute 会>120先命中加时分支)
        if elapsed_min is not None and elapsed_min > 130:
            return -1  # 已结束
        # 从 S8 分钟判断阶段
        if match_minute and ":" in str(match_minute):
            try:
                mm = int(str(match_minute).split(":")[0])
                if 45 < mm <= 50:
                    return 2  # 中场(按分钟推断)
                if mm > 50:
                    return 3  # 下半场
                if mm > 120:
                    return 4  # 加时
                return 1  # 上半场
            except Exception:
                pass
        # 兜底: 用已开赛时间
        if elapsed_min is not None and elapsed_min > 50:
            return 3
        return 1
    if ms == 110 or ms == 0:
        return 0
    # 未知状态: 如果已开赛超 10min 兜底为 live
    if elapsed_min is not None and elapsed_min > 10:
        return 1
    return 0


def normalize_match(m: dict, struct: dict = None):
    """归一化一场比赛, 含 6 大市场全部赔率。优先用 structure 数据(struct 不为空时)。"""
    src = struct if struct is not None else m
    # 1X2 全场
    oh, od, oa = _extract_1x2(src)
    if oh is None and struct is not m:
        oh, od, oa = _extract_1x2(m)
    # OU 全场
    ou_line, ou_over, ou_under = _extract_ou(src, HPID_OU_FULL)
    if ou_over is None and struct is not m:
        ou_line, ou_over, ou_under = _extract_ou(m, HPID_OU_FULL)
    # AH 全场
    ah_line, ah_home, ah_away = _extract_ah(src, HPID_AH_FULL)
    if ah_home is None and struct is not m:
        ah_line, ah_home, ah_away = _extract_ah(m, HPID_AH_FULL)
    # 半场 1X2
    h1_oh, h1_od, h1_oa = _extract_half_1x2(src)
    if h1_oh is None and struct is not m:
        h1_oh, h1_od, h1_oa = _extract_half_1x2(m)
    # 半场 OU
    h_ou_line, h_ou_over, h_ou_under = _extract_ou(src, HPID_OU_HALF)
    if h_ou_over is None and struct is not m:
        h_ou_line, h_ou_over, h_ou_under = _extract_ou(m, HPID_OU_HALF)
    # 半场 AH
    h_ah_line, h_ah_home, h_ah_away = _extract_ah(src, HPID_AH_HALF)
    if h_ah_home is None and struct is not m:
        h_ah_line, h_ah_home, h_ah_away = _extract_ah(m, HPID_AH_HALF)

    # 状态: 优先从 m 读, 缺失则回退到 struct. msc 里 S0 才是当前比分.
    # ms: 1=进行中, 110=未开赛; mststi 在 structure 里常滞后/为 0.
    def _get(field):
        if m and m.get(field) is not None:
            return m.get(field)
        if struct and struct.get(field) is not None:
            return struct.get(field)
        return None

    ms_val = _get("ms")
    mststi = _get("mststi")
    mprmc = _get("mprmc")
    cts = _get("cts")
    ct = _get("ct")
    msc = _get("msc")
    mhs, mas = _score_from_msc_leisu(msc)
    # 结构里有时 mhs=home_score 但 mas 缺失, 做二次兜底
    if mhs is None:
        try:
            mhs = int(_get("mhs"))
        except Exception:
            mhs = None
    if mas is None:
        try:
            mas = int(_get("mas"))
        except Exception:
            mas = None

    # 计算已开赛分钟(用于状态兜底)
    mgt = m.get("mgt") if m else None
    if mgt is None and struct is not None:
        mgt = struct.get("mgt")
    elapsed_min = None
    try:
        mgt_ms = int(mgt)
        if mgt_ms > 0:
            elapsed_min = (time.time() * 1000 - mgt_ms) / 60000
    except Exception:
        elapsed_min = None

    match_minute = _match_minute_from_msc(msc)
    if not match_minute:
        match_minute = mprmc if mprmc and str(mprmc) not in ("0", "") else ""
    match_state = _match_status_from_ms(ms_val, mststi, mprmc, elapsed_min, match_minute)

    return {
        "id": str(m.get("mid") or src.get("mid") or ""),
        "home": m.get("mhn") or src.get("mhn") or "",
        "away": m.get("man") or src.get("man") or "",
        "commence_time": _iso_from_ms(m.get("mgt") or src.get("mgt")),
        "league": m.get("tnjc") or src.get("tnjc") or "",
        "sport": m.get("csna") or src.get("csna") or "",
        # 1X2 全场
        "odds_h": oh,
        "odds_d": od,
        "odds_a": oa,
        # 全场大小
        "ou_line": ou_line,
        "ou_over": ou_over,
        "ou_under": ou_under,
        # 全场让球
        "ah_line": ah_line,
        "ah_home": ah_home,
        "ah_away": ah_away,
        # 半场 1X2
        "h1_odds_h": h1_oh,
        "h1_odds_d": h1_od,
        "h1_odds_a": h1_oa,
        # 半场大小
        "h_ou_line": h_ou_line,
        "h_ou_over": h_ou_over,
        "h_ou_under": h_ou_under,
        # 半场让球
        "h_ah_line": h_ah_line,
        "h_ah_home": h_ah_home,
        "h_ah_away": h_ah_away,
        # 状态
        "match_state": match_state,  # 标准化: 0=未开赛, 1=上半场, 3=下半场, -1=结束, >=6=异常
        "score_home": mhs,
        "score_away": mas,
        "match_minute": match_minute,
        "kickoff_countdown": ct,
        "kickoff_ms": cts,
        "bookmakers_count": 1,
    }


# ═══ Playwright 模式: 导航 SPA 完成鉴权, 拦截 SPA 自发 API 调用 ═══
# 原理: 浏览器 TLS 指纹天然正确, SPA 会用 FingerprintJS 生成正确的 checkid,
#       我们只需拦截 SPA → API 的响应即可。

_CACHED_FEED = None
_CACHE_TIME = 0
FEED_TTL = int(os.getenv("LEISU_FEED_TTL", "60"))

# ═══ 会话配置: 从 config/leisu_session.json 热重载 (避免硬编码 token 过期就崩) ═══
# 优先级: config/leisu_session.json 的 token/deep_link > 模块级默认值(已过期, 仅兼容)。
# 配置变更后无需重启 bridge, 下次调用自动重载并清 feed 缓存立即生效。
_token_expired = False  # Playwright 拦截到 getUserInfoPB 0401013 时置 True

_session_cache = {"mtime": -1, "data": None}

def _load_session():
    """读取乐鱼会话配置, 带 mtime 热重载. 返回 {token, session_id, deep_link}。"""
    global _session_cache, _CACHED_FEED, _CACHE_TIME
    try:
        mtime = os.path.getmtime(_SESSION_FILE)
    except OSError:
        mtime = 0
    if _session_cache["data"] is not None and mtime == _session_cache["mtime"]:
        return _session_cache["data"]
    data = {"token": TOKEN, "session_id": SESSION_PREFIX, "deep_link": None, "api": API_DEFAULT}
    if mtime > 0:
        try:
            with open(_SESSION_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            for k in ("token", "session_id", "deep_link", "api"):
                if cfg.get(k):
                    data[k] = cfg[k]
        except Exception as e:
            print(f"[leisu_session] 读取失败, 用默认(已过期)配置: {e}", flush=True)
    if not data.get("deep_link"):
        data["deep_link"] = (
            f"https://user-pc-new.realcpf.com/?token={data['token']}"
            f"&gr=b&tm=1&lg=zh&mk=0&stm=blue"
            f"&api={data['api']}"
            f"&skinColor=2&sessionId={data['session_id']}")
    if mtime != _session_cache["mtime"]:
        _CACHED_FEED, _CACHE_TIME = None, 0  # 配置变更 → 立即生效
    _session_cache = {"mtime": mtime, "data": data}
    return data


def _pw_fetch_combined():
    """
    启动 Playwright, 导航 SPA, 同时捕获 getMatchBaseInfoByOddsPB 和
    structureMatchBaseInfoByMidsPB 两个端点. 返回 (odds_data, structures_dict)。
    structures_dict: {str(mid): structure_match_dict}

    修正 (2026-07-20):  SPA 会多次调用这两个接口(不同域名、不同 mid 列表),
    不能只取第一次响应。现在合并所有响应, 按 mid 去重.
    """
    from playwright.sync_api import sync_playwright

    sess = _load_session()
    deep_link = sess["deep_link"]
    odds_data = {}   # {str(mid): match_dict}
    structures = {} # {str(mid): match_dict}
    last_pw_fail = None

    def on_response(resp):
        global _token_expired
        url = resp.url
        try:
            text = resp.text()
            obj = json.loads(text)
        except Exception:
            return
        # 账户校验失败 → 明确标记 token 过期, 供 build_feed 输出可读错误
        if "getUserInfoPB" in url and obj.get("code") == "0401013":
            _token_expired = True
            return
        if obj.get("code") != "0000000":
            return
        dec = decode_payload(obj.get("data"))
        if dec is None:
            return
        if "getMatchBaseInfoByOddsPB" in url:
            lst = dec.get("data") if isinstance(dec, dict) else dec
            if isinstance(lst, list):
                for m in lst:
                    if isinstance(m, dict) and m.get("mid"):
                        odds_data[str(m["mid"])] = m
        elif "structureMatchBaseInfoByMidsPB" in url:
            lst = dec.get("data") if isinstance(dec, dict) else dec
            if isinstance(lst, list):
                for m in lst:
                    if isinstance(m, dict) and m.get("mid"):
                        structures[str(m["mid"])] = m

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=CHROME_PATH, headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-gpu", "--disable-dev-shm-usage"])
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                ignore_https_errors=True)
            page = ctx.new_page()
            page.on("response", on_response)
            # 导航到深链
            page.goto(deep_link, wait_until="networkidle", timeout=15000)
            # 等 SPA 加载 + 滚动触发更多 API
            for _ in range(8):
                page.mouse.wheel(0, 1500)
                time.sleep(1.0)
            time.sleep(3)
            browser.close()
    except Exception as e:
        last_pw_fail = str(e)

    if last_pw_fail:
        raise RuntimeError(f"Playwright 启动失败: {last_pw_fail}")
    if not odds_data and not structures:
        raise RuntimeError("未捕获到任何比赛数据")

    return list(odds_data.values()), structures


def fetch_odds_list_pw():
    """Playwright 模式: 返回 (matches, structures_by_mid)."""
    return _pw_fetch_combined()


# ═══ Feed 构建 (统一入口) ═══

def build_feed():
    """返回 {leagues: {league_name: [FixtureEntry...]}, updated_at, error}。
    走 Playwright, 同时拉取 structureMatchBaseInfoByMidsPB (12+ 场全量) +
    getMatchBaseInfoByOddsPB (1X2 赔率), 合并生成完整 feed。"""
    global _CACHED_FEED, _CACHE_TIME
    now = time.time()
    if _CACHED_FEED and (now - _CACHE_TIME) < FEED_TTL:
        return _CACHED_FEED

    try:
        matches, structures = fetch_odds_list_pw()
    except Exception as e_pw:
        global _token_expired
        if _token_expired:
            err_msg = "乐鱼 TOKEN 已过期(0401013 账户信息已过期), 请更新 config/leisu_session.json 的 token"
        else:
            err_msg = f"Playwright 模式失败: {e_pw}"
        _CACHED_FEED = {"error": err_msg, "leagues": {}, "updated_at": int(now)}
        _CACHE_TIME = now
        return _CACHED_FEED

    # 合并: 优先 structures 里全量数据, odds_list 补充赔率
    # structures 有 12+ 场, odds_list 通常只有 1-2 场
    combined = {}
    # 先加 structures (有完整队名/联赛/时间)
    for mid, s in structures.items():
        combined[mid] = (s, s)  # (odds_match, structure)
    # 再加 odds_list (有赔率)
    for m in (matches if isinstance(matches, list) else []):
        if not isinstance(m, dict):
            continue
        mid = str(m.get("mid") or "")
        if not mid:
            continue
        if mid in combined:
            combined[mid] = (m, combined[mid][1])  # odds 替换, 保留 structure
        else:
            combined[mid] = (m, m)  # 没有 structure, 用自身

    if not combined:
        _CACHED_FEED = {"error": "未采集到任何比赛数据", "leagues": {}, "updated_at": int(now)}
        _CACHE_TIME = now
        return _CACHED_FEED

    fb = []
    for mid, (odds_m, struct_m) in combined.items():
        if isinstance(odds_m, dict) and odds_m.get("csna") == "足球":
            fb.append((mid, odds_m, struct_m))
        elif isinstance(struct_m, dict) and struct_m.get("csna") == "足球":
            fb.append((mid, struct_m, struct_m))

    if not fb:
        _CACHED_FEED = {"error": "无足球比赛数据", "leagues": {}, "updated_at": int(now)}
        _CACHE_TIME = now
        return _CACHED_FEED

    leagues = {}
    normalized = []
    for mid, odds_m, struct_m in fb:
        norm = normalize_match(odds_m, struct_m)
        lg = norm["league"] or "其他"
        # 永久屏蔽 VS- 模拟联赛 (PANDA 独家 EAFC24/25 等产品包装).
        # ⚠️ 注意: VS- 前缀下可能混入被 PANDA 误标为真赛事的盘(如"VS-世界杯2026...EAFC25"
        # 实为真实 WC 淘汰赛). 真 WC 盘口应走 sharp 源(The Odds API), 不从本 feed 取.
        # 严禁在下方加 "EAFC"/"PANDA" 关键词屏蔽——会误杀真 WC 比赛.
        if lg.startswith("VS-"):
            continue
        leagues.setdefault(lg, []).append(norm)
        normalized.append(norm)

    # 存快照 + 检测水位
    try:
        from pipeline.leisu_store import store_and_detect
        stats = store_and_detect(normalized)
        if stats["signals_total"] > 0:
            print(f"[leisu_store] {stats}", flush=True)
    except Exception as e:
        print(f"[leisu_store] 失败: {e}", flush=True)

    # 存实时比分
    try:
        from pipeline.leisu_live_scores import save_live_score, init_scores_db, filter_live_matches
        init_scores_db()
        live = filter_live_matches(normalized)
        for m in live:
            save_live_score(m)
        if live:
            print(f"[leisu_live_scores] {len(live)} 场 live 已存", flush=True)
    except Exception as e:
        print(f"[leisu_live_scores] 失败: {e}", flush=True)

    _CACHED_FEED = {"leagues": leagues, "updated_at": int(now), "error": None}
    _CACHE_TIME = now
    return _CACHED_FEED


# ═══ 离线自测 ═══

if __name__ == "__main__":
    import sys
    sample_path = r"D:\Architecture\odds_raw.json"
    if os.path.exists(sample_path):
        sample = json.load(open(sample_path, encoding="utf-8"))
        dec = decode_payload(sample["baseInfo_odds"])
        print("decode ok, matches:", len(dec.get("data", [])))
        fb = [m for m in dec["data"] if m.get("csna") == "足球"]
        print("football matches:", len(fb))
        for m in fb[:3]:
            print(normalize_match(m))
    else:
        print("离线样本不存在, 跳过")
