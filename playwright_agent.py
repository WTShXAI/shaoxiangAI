"""
Playwright 浏览器自动化代理 v2.0
=================================
增强版: 打开真实Chromium浏览器 → 访问投注页面 → 发现API接口 → 读取DOM赔率 → WebSocket推送

三种运行模式:
  1. 监控模式 (默认): 连续抓取DOM赔率 → WS推送到bridge:9000
  2. 发现模式 (--discover): 拦截网络请求, 捕获真实API端点+响应JSON, 保存到 data/odds_api_capture.json
  3. 深挖模式 (--h5-deep): 在发现基础上, 自动跟随H5跳转 + 尝试多种内部路由探测赔率数据API
  4. 网关模式 (--gateway): 在指定端口启动FastAPI服务, 供前端无CORS调用

用法:
    # 先导出cookies (浏览器 → F12 → Application → Cookies → export JSON)
    # 监控模式 (有头, 看到浏览器窗口)
    python playwright_agent.py --cookies cookies.json

    # 发现模式 (一次性, 捕获API调用)
    python playwright_agent.py --discover --cookies cookies.json

    # 无头网关 (供前端调用, 无CORS问题)
    python playwright_agent.py --gateway --port 9112

Target URL (默认):
    https://www.08a2zp.vip:9967/game/sport/ob?enName=YBTY
"""
from __future__ import annotations

import asyncio
import json
import time
import sys
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright

# 强制 UTF-8 输出 (Windows GBK 兼容)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── Configuration ──
DEFAULT_URL = "https://www.08a2zp.vip:9967/game/sport/ob?enName=YBTY"
BRIDGE_WS = "ws://127.0.0.1:9000/ws/realtime"
THROTTLE_MS = 500
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── 反检测脚本 ──
STEALTH_JS = """
// 隐藏 webdriver 特征
Object.defineProperty(navigator, 'webdriver', { get: () => false });
// 伪造 chrome 对象
window.chrome = { runtime: {} };
// 伪造权限查询
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
    Promise.resolve({ state: Notification.permission }) :
    originalQuery(parameters)
);
// 覆盖 plugins 长度
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
// 覆盖 languages
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
"""

# ── 已知的API端点模式 (从JS bundle逆向) ──
ODDS_API_PATTERNS = [
    r"/page/fd/api/v1/[A-Za-z0-9]{10,}",
    r"/act/api/v1/[A-Za-z0-9]{10,}",
    r"/site/api/v1/sec/[A-Za-z0-9]{10,}",
    r"/api/v1/launcher/webTerminal",
    r"/api/v1/history",
    r"/api/v1/sec/[A-Za-z0-9]{10,}",
]

# ── H5跳转相关 (白标聚合器 → 第三方H5 app) ──
H5_LAUNCH_ENDPOINTS = [
    r"/game/api/v1/venue/launch",
    r"/api/v1/launcher/webTerminal",
    r"/page/fd/api/v1/venue/launch",
]
H5_HOST_PATTERNS = [
    r"app-h5\.realcpf\.com",
    r"api\.wnbtmel\.com",
    r"app-h5\.",
]

# ── H5 内部路由探针 (常见投注App hash路由) ──
H5_DEEP_ROUTES = [
    # 体育/赛事
    "sport", "sports", "football", "soccer", "zuqiu",
    "live", "inplay", "today", "todayMatch", "today_match",
    "match", "matches", "event", "events",
    "home", "index", "main",
    "competition", "league", "leagues",
    "basketball", "basket", "lanqiu",
    # 英文+数字组合
    "sport/football", "sport/live", "sport/today",
    "football/live", "football/today",
    "sport/basketball",
    "live/football", "live/1",
    "eventList", "matchList",
    "ESports", "esports",
    # 酒店规格 (酒店规格 -> 酒店规格 -> 酒店规格)
    "venue/1", "venue/YBTY", "venue",
    "sport/ob", "ob",
    # 简写
    "game", "games", "bet", "bets",
    "odds", "list",
    "data", "api",
    "1", "2", "3",
]

# 深挖模式: 一次只试前半段高概率路由 + 余下作为fallback
H5_PRIMARY_ROUTES = [
    "sport", "sports", "football", "live", "inplay",
    "today", "match", "home", "basketball",
]
H5_FALLBACK_ROUTES = [r for r in H5_DEEP_ROUTES if r not in H5_PRIMARY_ROUTES]


def load_cookies(path: str) -> list[dict]:
    """加载 cookies.json (标准JSON数组格式或Netscape格式)"""
    if not os.path.isfile(path):
        print(f"[Agent] ⚠ cookies 文件不存在: {path}")
        return []

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    # JSON array of cookie objects
    try:
        cookies = json.loads(raw)
        if isinstance(cookies, list):
            print(f"[Agent] 已加载 {len(cookies)} 个 cookies from {path}")
            return cookies
    except json.JSONDecodeError:
        pass

    # Netscape cookie file format (简化处理)
    print(f"[Agent] ⚠ 无法解析cookies文件, 请使用JSON数组格式")
    return []


class ApiDiscoverer:
    """API发现器 — 拦截网络请求, 捕获真实数据端点"""

    def __init__(self):
        self.captured_requests: list[dict] = []
        self.odds_api_response: Optional[dict] = None
        self.api_endpoint: Optional[str] = None
        self._resolved = asyncio.Event()

    def setup(self, page) -> None:
        """注册网络拦截"""
        async def on_response(response):
            url = response.url
            status = response.status

            # 只关注本站API
            if "api/v1" not in url and "page/fd" not in url:
                return

            # 尝试读body (可能流式, 只尝试一次)
            try:
                body = await response.body()
                body_text = body.decode("utf-8", errors="replace")
            except Exception:
                body_text = f"<body read error>"

            entry = {
                "url": url,
                "status": status,
                "time": datetime.now().isoformat(),
                "body_preview": body_text[:2000],
            }

            # 检查是否是odds数据端点 (大JSON响应, 含odds/market相关字段)
            if status == 200 and len(body_text) > 500:
                for pat in ODDS_API_PATTERNS:
                    if re.search(pat, url):
                        entry["matched_pattern"] = pat
                        self.api_endpoint = url
                        try:
                            self.odds_api_response = json.loads(body_text)
                            entry["parsed"] = True
                        except json.JSONDecodeError:
                            entry["parsed"] = False
                        self._resolved.set()
                        break

            self.captured_requests.append(entry)
            if self.api_endpoint:
                print(f"[Discover] 🎯 发现数据端点: {url[:100]} ({status}, {len(body_text)} bytes)")

        page.on("response", on_response)

    def get_report(self) -> dict:
        return {
            "captured_count": len(self.captured_requests),
            "api_endpoint": self.api_endpoint,
            "has_api_response": self.odds_api_response is not None,
            "requests": self.captured_requests,
        }

    async def wait_for_api(self, timeout: float = 15) -> bool:
        """等待API端点被发现"""
        try:
            await asyncio.wait_for(self._resolved.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


class OddsScraper:
    """赔率DOM抓取器 — 多层次策略: 精确属性 > 语义锚点 > 结构性扫描"""

    @staticmethod
    async def scrape(page) -> dict | None:
        """从当前页面提取所有可用赔率数据"""
        result = await page.evaluate("""() => {
            const num = s => {
                if (s === null || s === undefined) return null;
                const m = String(s).match(/(\\d+\\.\\d{2,4})/);
                return m ? parseFloat(m[1]) : null;
            };
            const ctx = { h:null, d:null, a:null, ah:[], ou:[], score:null, minute:null, cs:[], teams:[] };

            // ── 策略1: data属性直接命中 (常见于React SPAs) ──
            document.querySelectorAll('[data-odds],[data-h],[data-d],[data-a],[data-odd]').forEach(el => {
                const v = num(el.getAttribute('data-odds') || el.getAttribute('data-odd') ||
                             el.getAttribute('data-h') || el.innerText);
                if (v && v >= 1.01 && v <= 999) {
                    if (el.getAttribute('data-type') === 'home' || /home|主胜/.test(el.className)) ctx.h = v;
                    else if (el.getAttribute('data-type') === 'draw' || /draw|平/.test(el.className)) ctx.d = v;
                    else if (el.getAttribute('data-type') === 'away' || /away|客胜/.test(el.className)) ctx.a = v;
                    else if (!ctx.h) ctx.h = v; // 兜底: 第一个为主胜
                }
            });

            // ── 策略2: 语义锚点 —— 找 "主胜/平局/客胜" 附近赔率 ──
            if (!ctx.h || !ctx.d || !ctx.a) {
                document.querySelectorAll('*').forEach(el => {
                    const t = (el.innerText || '').trim().slice(0,10);
                    if(/^(主胜|主\\s*$|home)/i.test(t) && t.length < 8) ctx._hEl = el;
                    if(/^(平局|平|draw)/i.test(t) && t.length < 8) ctx._dEl = el;
                    if(/^(客胜|客\\s*$|away)/i.test(t) && t.length < 8) ctx._aEl = el;
                });

                const nearOdds = (el, fallback) => {
                    if(!el) return fallback;
                    let cur = el;
                    for(let i=0; i<5; i++){
                        cur = cur.parentElement;
                        if(!cur) break;
                        for(const k of cur.querySelectorAll('span,div,td,em,strong')){
                            const v = num(k.innerText);
                            if(v && v >= 1.01 && v <= 50) return v;
                        }
                    }
                    return fallback;
                };
                if (!ctx.h) ctx.h = nearOdds(ctx._hEl, ctx.h);
                if (!ctx.d) ctx.d = nearOdds(ctx._dEl, ctx.d);
                if (!ctx.a) ctx.a = nearOdds(ctx._aEl, ctx.a);
            }

            // ── 策略3: 找所有1.01-50的数值, 取出最可能的三组 ──
            if (!ctx.h || !ctx.d || !ctx.a) {
                const odds = [];
                document.querySelectorAll('span,div,td,em,strong').forEach(el => {
                    const v = num(el.innerText);
                    if (v && v >= 1.01 && v <= 50 && el.offsetHeight > 0) odds.push(v);
                });
                // 去重取前三
                const unique = [...new Set(odds)].sort((a,b)=>a-b);
                if (unique.length >= 3) {
                    if (!ctx.h) ctx.h = unique[0];
                    if (!ctx.d) ctx.d = unique[1];
                    if (!ctx.a) ctx.a = unique[2];
                }
            }

            // ── 亚盘 ──
            document.querySelectorAll('tr, [class*=row], [class*=table]').forEach(row => {
                const text = row.innerText || '';
                if(/让球|handicap|盘口|让分|<\\/?font/i.test(text)){
                    const nums = [];
                    row.querySelectorAll('td, span, div, em').forEach(c => {
                        const v = num(c.innerText);
                        if(v) nums.push(v);
                    });
                    if(nums.length >= 2) ctx.ah.push(nums);
                }
            });

            // ── 大小球 ──
            document.querySelectorAll('tr, [class*=row], [class*=table]').forEach(row => {
                const text = row.innerText || '';
                if(/大小|over.*under|总进球|大.*小/i.test(text)){
                    const nums = [];
                    row.querySelectorAll('td, span, div, em').forEach(c => {
                        const v = num(c.innerText);
                        if(v) nums.push(v);
                    });
                    if(nums.length >= 2) ctx.ou.push(nums);
                }
            });

            // ── 波胆 ──
            document.querySelectorAll('table').forEach(tbl => {
                if(/波胆|correct.score|cs:|准确比分/i.test(tbl.innerText || '')){
                    tbl.querySelectorAll('td, th, span').forEach(c => {
                        const v = num(c.innerText);
                        if(v && v > 1.5 && v < 500) ctx.cs.push(v);
                    });
                }
            });

            // ── 队名 ──
            ctx.teams = document.title ? document.title.split(/vs|VS|[-–—]/).map(s=>s.trim()).filter(Boolean).slice(0,2) : [];

            // ── 比分 & 时间 ──
            const title = document.title || '';
            const sm = title.match(/(\\d+)\\s*[:：]\\s*(\\d+)/);
            if(sm) ctx.score = sm[1] + ':' + sm[2];

            // 清理临时属性
            delete ctx._hEl; delete ctx._dEl; delete ctx._aEl;
            return ctx;
        }""")

        return result


async def run_agent(
    target_url: str,
    cookies_path: str = "",
    discover: bool = False,
    headless: bool = False,
    h5_deep: bool = False,
):
    """主循环: 连接页面 → 定时抓取 → WS推送"""
    print(f"\n{'='*50}")
    print(f"[Agent] Playwright v2.0 | 模式: {'发现+深挖' if h5_deep else '发现' if discover else '监控'}")
    print(f"[Agent] 目标: {target_url}")
    print(f"[Agent] 有头模式: {not headless}")
    print(f"[Agent] Cookies: {cookies_path or '未加载'}")
    print(f"{'='*50}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-infobars',
                '--ignore-certificate-errors',
                '--disable-web-security',
            ]
        )

        context = await browser.new_context(
            viewport={'width': 1400, 'height': 900},
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                        '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        )

        # ── 加载cookies ──
        if cookies_path:
            cookies = load_cookies(cookies_path)
            if cookies:
                # 转换为Playwright格式
                pw_cookies = []
                for c in cookies:
                    # sameSite: None 在 Playwright 中会报错, 兜底为 "Lax"
                    ss = c.get("sameSite") or "Lax"
                    if ss not in ("Strict", "Lax", "None"):
                        ss = "Lax"
                    pw_cookie = {
                        "name": c.get("name", ""),
                        "value": c.get("value", ""),
                        "domain": c.get("domain", ".08a2zp.vip"),
                        "path": c.get("path", "/"),
                        "httpOnly": bool(c.get("httpOnly", False)),
                        "secure": bool(c.get("secure", True)),
                        "sameSite": ss,
                    }
                    # 多种字段名兼容 expirationDate / expiry / expires
                    exp = c.get("expirationDate") or c.get("expiry") or c.get("expires")
                    if exp and not c.get("session", False):
                        try:
                            pw_cookie["expires"] = int(float(exp))
                        except (ValueError, TypeError):
                            pass
                    pw_cookies.append(pw_cookie)

                await context.add_cookies(pw_cookies)
                print(f"[Agent] ✓ cookies已注入浏览器 ({len(pw_cookies)} 个)")
                for c in pw_cookies:
                    print(f"    - {c['name']}={c['value'][:8]}... domain={c['domain']} secure={c['secure']}")

        page = await context.new_page()
        await page.add_init_script(STEALTH_JS)

        # ── 发现模式: 注册网络拦截 ──
        discoverer = ApiDiscoverer()
        if discover:
            discoverer.setup(page)
            # 同时监听所有请求以便调试
            async def on_request(request):
                url = request.url
                if "api/v1" in url or "page/fd" in url or "sec/" in url:
                    print(f"[Net] {request.method} {url[:120]}")
            page.on("request", on_request)

        # ── 导航 ──
        print(f"[Agent] 正在加载页面...")
        try:
            response = await page.goto(target_url, wait_until='networkidle', timeout=45000)
            status = response.status if response else 0
            print(f"[Agent] 页面加载完成: HTTP {status}")
        except Exception as e:
            print(f"[Agent] 页面加载超时/异常: {e}")
            await page.wait_for_timeout(5000)

        # 检查是否被重定向到登录页
        current_url = page.url
        if "login" in current_url.lower() or "register" in current_url.lower():
            print(f"[Agent] ⚠ 被重定向到登录页: {current_url}")
            print(f"[Agent] ⚠ 需要提供有效的 cookies (导出后通过 --cookies 加载)")
            if not discover:
                print(f"[Agent] ⚠ 无有效会话, 5秒后退出")
                await asyncio.sleep(5)
                return

        # 等待DOM渲染
        await page.wait_for_timeout(3000)

        # ── 发现模式: 等待API并保存 ──
        if discover:
            print(f"[Agent] 等待API请求捕获 (最多15秒)...")
            found = await discoverer.wait_for_api(timeout=15)
            await page.wait_for_timeout(2000)  # 多等一会儿懒加载

            # 再手动扫描一下已发生的网络请求
            print(f"[Agent] 捕获网络请求: {len(discoverer.captured_requests)} 条")

            report = discoverer.get_report()

            # 未发现API端点 -> 尝试从请求列表猜测
            if not report["api_endpoint"] and report["captured_requests"]:
                # 找200且body最大的几条
                by_size = sorted(
                    [r for r in report["requests"] if r["status"] == 200],
                    key=lambda r: len(r["body_preview"]),
                    reverse=True
                )
                if by_size:
                    largest = by_size[0]
                    report["api_endpoint"] = largest["url"][:200]
                    print(f"[Agent] 未匹配已知模式, 最大响应来自: {largest['url'][:100]}")

            # 保存捕获结果
            capture_path = DATA_DIR / "odds_api_capture.json"
            with open(capture_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            print(f"[Agent] 发现报告已保存: {capture_path}")

            # 如果有API响应JSON, 再单独保存一份干净的
            if discoverer.odds_api_response:
                api_json_path = DATA_DIR / "odds_api_response.json"
                with open(api_json_path, "w", encoding="utf-8") as f:
                    json.dump(discoverer.odds_api_response, f, ensure_ascii=False, indent=2, default=str)
                print(f"[Agent] API响应JSON已保存: {api_json_path}")

            # 保存当前DOM (调试用)
            dom_path = DATA_DIR / "odds_page_dom.html"
            try:
                dom_content = await page.content()
                with open(dom_path, "w", encoding="utf-8") as f:
                    f.write(dom_content)
                print(f"[Agent] 当前DOM已保存: {dom_path} ({len(dom_content)} 字节)")
            except Exception as e:
                print(f"[Agent] DOM保存失败: {e}")

            # ── v2.1: 自动跟随 H5 跳转 (白标launchpad → 真实H5 app) ──
            h5_url = None
            h5_launch_response = None
            for r in discoverer.captured_requests:
                for pat in H5_LAUNCH_ENDPOINTS:
                    if re.search(pat, r["url"]):
                        try:
                            launch_data = json.loads(r["body_preview"])
                            if isinstance(launch_data.get("data"), dict):
                                d = launch_data["data"]
                                h5_url = d.get("h5Url") or d.get("activityUrl") or d.get("url")
                                h5_launch_response = launch_data
                                if h5_url:
                                    print(f"[H5] 🎯 检测到venue/launch响应, h5Url = {h5_url[:80]}...")
                                    # 安全: token和sessionId遮蔽
                                    safe_url = re.sub(r'(token|sessionId|api)=[^&]+', r'\1=XXX', h5_url)
                                    print(f"[H5]    完整URL(已遮蔽敏感参数): {safe_url}")
                                    break
                        except (json.JSONDecodeError, KeyError):
                            pass
                if h5_url:
                    break

            if h5_url:
                print(f"\n[H5] 自动跟随H5跳转 (v2.1)...")
                # 保存 launch 响应单独一份
                launch_path = DATA_DIR / "venue_launch_response.json"
                with open(launch_path, "w", encoding="utf-8") as f:
                    # token遮蔽
                    safe = json.dumps(h5_launch_response, ensure_ascii=False, default=str)
                    safe = re.sub(r'("token"|"sessionId"|"api")\s*:\s*"[^"]+"', r'\1: "XXX"', safe)
                    f.write(safe)
                print(f"[H5] venue/launch响应已保存(已遮蔽): {launch_path}")

                # 导航到H5 app (新page不共享cookies, h5Url自带token)
                try:
                    h5_page = await context.new_page()
                    # 复用同一个discoverer
                    discoverer.setup(h5_page)

                    print(f"[H5] 正在加载H5 app...")
                    await h5_page.goto(h5_url, wait_until='domcontentloaded', timeout=45000)
                    print(f"[H5] H5 app已加载, 当前URL: {h5_page.url[:120]}")
                    print(f"[H5] 等待H5 API请求 (20秒)...")
                    await h5_page.wait_for_timeout(20000)

                    # 抓H5 DOM
                    h5_dom_path = DATA_DIR / "h5_app_dom.html"
                    try:
                        h5_dom = await h5_page.content()
                        with open(h5_dom_path, "w", encoding="utf-8") as f:
                            f.write(h5_dom)
                        print(f"[H5] H5 DOM已保存: {h5_dom_path} ({len(h5_dom)} 字节)")
                    except Exception as e:
                        print(f"[H5] H5 DOM保存失败: {e}")

                    # 抓H5 API的title/URL信息
                    h5_title = await h5_page.title()
                    print(f"[H5] H5 page title: {h5_title}")

                    # 保存H5完整的网络捕获 (不过滤host, 捕获所有H5调用)
                    h5_capture_path = DATA_DIR / "odds_h5_capture.json"
                    # 收集H5页面所有请求
                    h5_requests = [r for r in discoverer.captured_requests
                                   if int(r.get("time", "2026-01-01T00:00:00").split("T")[1].split(":")[0] or "0") > 0
                                   and not any(x in r["url"] for x in [
                                       "www.08a2zp.vip:9967/site/api/v1/user/member/info",
                                       "www.08a2zp.vip:9967/site/api/v1/site/letter/",
                                       "www.08a2zp.vip:9967/act/api/v1/redPoint/",
                                       "www.08a2zp.vip:9967/site/api/v1/user/register/stop",
                                       "www.08a2zp.vip:9967/act/api/v1/advertising/",
                                       "www.08a2zp.vip:9967/site/api/v1/site/vipExclusiveDomain/",
                                       "www.08a2zp.vip:9967/site/api/v1/user/checkDedicatedManagerTag",
                                       "www.08a2zp.vip:9967/act/api/v1/dividend/",
                                       "www.08a2zp.vip:9967/site/api/v1/configuration/",
                                       "www.08a2zp.vip:9967/site/api/v1/launcher/",
                                       "www.08a2zp.vip:9967/site/api/v1/video/",
                                       "www.08a2zp.vip:9967/site/api/v1/site/venue/sort",
                                       "www.08a2zp.vip:9967/act/api/v1/activityShow/",
                                       "www.08a2zp.vip:9967/sact/api/v1/",
                                       "www.08a2zp.vip:9967/act/api/v1/memberTopActivity/",
                                       "www.08a2zp.vip:9967/page/fd/api/v1/common/floatIcon/",
                                       "www.08a2zp.vip:9967/game/api/v1/venue/launch",
                                       "www.08a2zp.vip:9967/site/api/v1/sec/",
                                       "www.08a2zp.vip:9967/site/api/v1/advertising/queryNoticeList",
                                       "www.08a2zp.vip:9967/act/api/v1/advertising/queryBannerList",
                                       "www.08a2zp.vip:9967/site/api/v1/site/letter/",
                                       "www.08a2zp.vip:9967/site/api/v1/user/member/jwt",
                                   ])]
                    # 去重URL
                    seen_urls = set()
                    h5_unique = []
                    for r in h5_requests:
                        if r["url"] not in seen_urls:
                            seen_urls.add(r["url"])
                            h5_unique.append(r)
                    h5_report = {
                        "h5_url": re.sub(r'(token|sessionId|api)=[^&]+', r'\1=XXX', h5_url),
                        "h5_title": h5_title,
                        "h5_api_calls_total": len(h5_requests),
                        "h5_api_unique_urls": len(h5_unique),
                        "h5_dom_size_bytes": len(h5_dom) if 'h5_dom' in dir() else 0,
                        "real_odds_endpoints": [
                            r["url"] for r in h5_unique
                            if r["status"] == 200 and len(r["body_preview"]) > 200
                        ],
                        "all_h5_requests": h5_unique,
                    }
                    with open(h5_capture_path, "w", encoding="utf-8") as f:
                        json.dump(h5_report, f, ensure_ascii=False, indent=2, default=str)
                    print(f"[H5] H5 API捕获已保存: {h5_capture_path}")
                    print(f"[H5] H5发现的大数据API端点: {len(h5_report['real_odds_endpoints'])} 个")
                    for ep in h5_report['real_odds_endpoints'][:10]:
                        print(f"    - {ep[:120]}")

                    # ── v2.2: H5 深挖模式 ──
                    if h5_deep:
                        print(f"\n{'='*50}")
                        print(f"[H5-DEEP] 开始深挖H5路由 (共{len(H5_PRIMARY_ROUTES)}条高概率路由)")
                        print(f"{'='*50}\n")

                        # 刷新 network 监听: 只记录H5深挖阶段的新请求
                        deep_requests = []
                        async def on_deep_response(response):
                            url = response.url
                            try:
                                body = await response.body()
                                deep_requests.append({
                                    "url": url,
                                    "status": response.status,
                                    "size": len(body),
                                    "body_preview": body.decode("utf-8", errors="replace")[:500],
                                })
                            except:
                                pass

                        h5_page.on("response", on_deep_response)

                        # 优先从已知真实后端根路径尝试
                        dbsport_base = "http://api-mirror.dbsportxxx14bl5.com"

                        deep_report = {
                            "routes_tried": [],
                            "successful_routes": [],
                            "dbsport_calls": [],
                            "by_route": {},
                        }

                        # 先直接探 api-mirror (绕过前端路由检查)
                        print(f"[H5-DEEP] 直接探测后端: {dbsport_base}")
                        # 注入 script 尝试 fetch 后端
                        try:
                            probe_result = await h5_page.evaluate(f"""
                                async () => {{
                                    const results = [];
                                    const paths = [
                                        "/api/v1/match/list", "/api/v1/odds/list",
                                        "/api/v1/sport/list", "/api/v1/competition/list",
                                        "/api/v1/event/list", "/api/v1/league/list",
                                        "/api/v1/football/list", "/api/v1/live/list",
                                        "/api/v1/home", "/api/v1/today",
                                        "/api/v1/sport/home", "/api/v1/event/today",
                                        "/api/v1/venue/1",
                                        "/api/v1/sport/1", "/api/v1/venue/1/match",
                                    ];
                                    for(const p of paths) {{
                                        try {{
                                            const r = await fetch('{dbsport_base}' + p, {{
                                                headers: {{'Content-Type': 'application/json'}}
                                            }});
                                            const text = await r.text();
                                            results.push({{path: p, status: r.status, size: text.length, body: text.slice(0,300)}});
                                        }} catch(e) {{
                                            results.push({{path: p, error: e.message}});
                                        }}
                                    }}
                                    return results;
                                }}
                            """)
                            deep_report["backend_probe"] = probe_result
                            print(f"[H5-DEEP] 后端探测完成: {len(probe_result)} 个端点")
                            for pr in probe_result:
                                if pr.get("status") == 200 and pr.get("size", 0) > 200:
                                    print(f"    ⚡ {pr['path']} HTTP {pr['status']} ({pr['size']} bytes)")
                                    deep_report["dbsport_calls"].append(pr)
                                elif pr.get("error"):
                                    print(f"    ✗ {pr['path']}: {pr['error'][:60]}")
                        except Exception as e:
                            print(f"[H5-DEEP] 后端探测异常: {e}")

                        # 试前端 hash 路由
                        for i, route in enumerate(H5_PRIMARY_ROUTES + H5_FALLBACK_ROUTES):
                            route_url = f"{h5_url.split('#')[0]}#/{route}"
                            route_key = f"/{route}"
                            print(f"[H5-DEEP]  [{i+1}/{len(H5_PRIMARY_ROUTES)+len(H5_FALLBACK_ROUTES)}] 尝试路由: {route_key}")

                            # 记录当前请求数 (后续增量)
                            before_count = len(deep_requests)

                            try:
                                await h5_page.goto(route_url, wait_until='domcontentloaded', timeout=10000)
                                await h5_page.wait_for_timeout(5000)
                            except Exception as e:
                                print(f"    ↺ 加载超时/异常 (继续): {str(e)[:50]}")
                                await h5_page.wait_for_timeout(3000)

                            # 检查增量请求
                            new_reqs = deep_requests[before_count:]
                            new_api_count = len(new_reqs)
                            new_data_bytes = sum(r.get("size", 0) for r in new_reqs)

                            route_info = {
                                "route": route_key,
                                "new_api_calls": new_api_count,
                                "new_data_bytes": new_data_bytes,
                                "urls": [r["url"][:120] for r in new_reqs],
                                "status": [r["status"] for r in new_reqs],
                            }
                            deep_report["by_route"][route_key] = route_info

                            if new_api_count > 0:
                                print(f"    → {new_api_count} 新API调用, {new_data_bytes} bytes")
                                if new_data_bytes > 1000:
                                    deep_report["successful_routes"].append(route_key)
                                    print(f"    ✓ 有效数据路由: {route_key} ({new_data_bytes} bytes)")
                                for nr in new_reqs[:5]:
                                    print(f"      · {nr['url'][:100]} ({nr['size']} bytes)")
                            else:
                                print(f"    · 无新API调用")

                            # 前几条命中就够, 不用全扫
                            if len(deep_report["successful_routes"]) >= 3:
                                print(f"[H5-DEEP] 已找到3个有效路由, 停止扫描")
                                break

                            # 限速: 避免被ban
                            await h5_page.wait_for_timeout(1000)

                        # 合并到最终的 H5 报告
                        deep_report["total_h5_routes_tried"] = i + 1
                        deep_report["total_dbsport_endpoints"] = len(deep_report.get("dbsport_calls", []))
                        deep_report["total_new_api_calls"] = sum(
                            v.get("new_api_calls", 0) for v in deep_report.get("by_route", {}).values()
                        )

                        deep_path = DATA_DIR / "h5_deep_report.json"
                        with open(deep_path, "w", encoding="utf-8") as f:
                            json.dump(deep_report, f, ensure_ascii=False, indent=2, default=str)
                        print(f"\n[H5-DEEP] 报告已保存: {deep_path}")
                        print(f"[H5-DEEP] 有效路由: {len(deep_report['successful_routes'])} 个")
                        for sr in deep_report['successful_routes']:
                            info = deep_report['by_route'].get(sr, {})
                            print(f"    ✓ {sr}: {info.get('new_api_calls', 0)} calls, {info.get('new_data_bytes', 0)} bytes")
                        print(f"[H5-DEEP] 后端直接端点探测: {len(deep_report.get('dbsport_calls', []))} 个返回数据")
                        for dbe in deep_report.get("backend_probe", []):
                            if dbe.get("status") == 200 and dbe.get("size", 0) > 200:
                                print(f"    ⚡ {dbe['path']}: {dbe.get('size', 0)} bytes")

                    await h5_page.close()
                except Exception as e:
                    print(f"[H5] H5跳转异常: {e}")
            else:
                print(f"[H5] 未发现venue/launch响应, 跳过H5跳转")

            print(f"\n[Agent] 发现模式完成。运行以下命令启动监控模式:")
            print(f"    python playwright_agent.py --cookies cookie.json")
            return

        # ── 监控模式: 连续抓取 → WS推送 ──
        print(f"[Agent] 开始监控赔率...")
        from websockets import connect as ws_connect
        ws = None
        ws_retry = 0

        async def ensure_ws():
            nonlocal ws, ws_retry
            if ws and ws.open:
                return True
            try:
                ws = await ws_connect(BRIDGE_WS, max_size=2**20)
                ws_retry = 0
                print("[Agent] WebSocket ✓")
                return True
            except Exception:
                ws_retry += 1
                return False

        last_hash = ""
        while True:
            try:
                title = await page.title()
                parts = re.split(r'[vs\-–—]', title, maxsplit=1)
                home_team = parts[0].strip()[:20] if len(parts) >= 2 else ""
                away_team = parts[1].strip()[:20] if len(parts) >= 2 else ""

                data = await OddsScraper.scrape(page)
                if data:
                    data['home'] = home_team or data.get('teams', ['主队', '客队'])[0]
                    data['away'] = away_team or (data.get('teams', ['主队', '客队'])[1:] or ['客队'])[0]
                    data['ts'] = int(time.time() * 1000)
                    data['source'] = 'playwright'

                    h = f"{data.get('h', 0):.2f}" if data.get('h') else '0'
                    d = f"{data.get('d', 0):.2f}" if data.get('d') else '0'
                    a = f"{data.get('a', 0):.2f}" if data.get('a') else '0'
                    ch = f"{h}|{d}|{a}"

                    if ch != last_hash:
                        last_hash = ch
                        print(f"[Agent] 赔率变化: {h}/{d}/{a} {'✓' if data.get('h') else '⚠无赔率'} — {title[:30]}")

                        if await ensure_ws():
                            try:
                                await ws.send(json.dumps({
                                    'type': 'odds_update',
                                    'payload': data
                                }, ensure_ascii=False))

                                import aiohttp
                                async with aiohttp.ClientSession() as session:
                                    await session.post(
                                        'http://127.0.0.1:9000/api/terminal/ingest',
                                        json=data,
                                        timeout=aiohttp.ClientTimeout(total=2)
                                    )
                            except Exception as e:
                                print(f"[Agent] 推送失败: {e}")
                                ws = None
                else:
                    print(f"[Agent] ⚠ 未检测到赔率数据, 页面可能未加载完成或需要登录")

            except Exception as e:
                print(f"[Agent] 抓取循环异常: {e}")

            await asyncio.sleep(THROTTLE_MS / 1000)


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Playwright 投注页面自动化采集代理 v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 发现API (需先导出cookies)
  python playwright_agent.py --discover --cookies cookies.json

  # 监控模式 (有头)
  python playwright_agent.py --cookies cookies.json

  # 无头监控, 指定页面
  python playwright_agent.py --headless --cookies cookies.json --url "https://..."
        """
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"投注页面URL (默认: {DEFAULT_URL})")
    parser.add_argument("--cookies", default="", help="cookies.json 路径 (从浏览器导出)")
    parser.add_argument("--discover", action="store_true", help="API发现模式 (捕获真实数据接口)")
    parser.add_argument("--headless", action="store_true", help="无头模式 (无GUI)")
    parser.add_argument("--h5-deep", action="store_true", dest="h5_deep", help="H5深挖模式 (试路由找赔率接口)")
    args = parser.parse_args()

    asyncio.run(run_agent(
        target_url=args.url,
        cookies_path=args.cookies,
        discover=args.discover,
        headless=args.headless,
        h5_deep=args.h5_deep,
    ))


if __name__ == "__main__":
    main()
