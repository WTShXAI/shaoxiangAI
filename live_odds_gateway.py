"""
哨响AI 实时赔率网关 (Live Odds Gateway)
=========================================
解决前端JS跨域获取投注页面数据的问题。

架构:
    SPA (localhost:3000) → /api/odds/live → 本服务 → Playwright渲染投注页面(有cookie) → 返回JSON赔率

启动:
    python live_odds_gateway.py --port 9112 --cookies cookies.json

前端调用 (fetch):
    fetch('http://127.0.0.1:9112/api/odds/live')
    fetch('http://127.0.0.1:9112/api/odds/live?enName=YBTY')

端点:
    GET /api/odds/live?enName=YBTY     → 最新赔率JSON
    GET /api/odds/cached                → 上次抓取结果 (免额外开销)
    GET /health                         → 健康检查
"""
from __future__ import annotations

import asyncio
import json
import time
import os
import sys
from pathlib import Path
from typing import Optional

# 强制 UTF-8 输出 (Windows GBK 兼容)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

# ── 复用 playwright_agent 的组件 ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from playwright_agent import STEALTH_JS, OddsScraper, load_cookies

# ── 配置 ──
DEFAULT_URL = "https://www.08a2zp.vip:9967/game/sport/ob?enName=YBTY"
CACHE_TTL = 10  # 秒, 缓存时间
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

app = FastAPI(
    title="哨响AI 实时赔率网关",
    description="Playwright驱动的投注页面赔率采集 + CORS代理",
    version="2.0.0",
)

# CORS: 允许本地前端任意源调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 全局状态 ──
class GatewayState:
    """网关全局状态: playwright实例 + 最新缓存"""
    browser = None
    context = None
    page = None
    last_result: Optional[dict] = None
    last_ts: float = 0
    cookies_path: str = ""
    target_url: str = DEFAULT_URL
    lock = asyncio.Lock()

state = GatewayState()


async def ensure_browser():
    """确保浏览器已启动并登录"""
    if state.browser and state.browser.is_connected():
        return True

    print("[Gateway] 启动 Playwright 浏览器 (无头)...")
    play = await async_playwright().__aenter__()
    state.browser = await play.chromium.launch(
        headless=True,
        args=[
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-infobars',
            '--ignore-certificate-errors',
        ]
    )

    state.context = await state.browser.new_context(
        viewport={'width': 1400, 'height': 900},
        locale='zh-CN',
        timezone_id='Asia/Shanghai',
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
    )

    # 加载cookies
    if state.cookies_path:
        cookies = load_cookies(state.cookies_path)
        if cookies:
            pw_cookies = []
            for c in cookies:
                ss = c.get("sameSite") or "Lax"
                if ss not in ("Strict", "Lax", "None"):
                    ss = "Lax"
                pw_cookies.append({
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ".08a2zp.vip"),
                    "path": c.get("path", "/"),
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "secure": bool(c.get("secure", True)),
                    "sameSite": ss,
                })
                exp = c.get("expirationDate") or c.get("expiry") or c.get("expires")
                if exp and not c.get("session", False):
                    try:
                        pw_cookies[-1]["expires"] = int(float(exp))
                    except (ValueError, TypeError):
                        pass
            await state.context.add_cookies(pw_cookies)
            print(f"[Gateway] ✓ 已注入 {len(pw_cookies)} 个cookies")

    state.page = await state.context.new_page()
    await state.page.add_init_script(STEALTH_JS)

    print("[Gateway] 浏览器就绪")
    return True


async def fetch_live_odds() -> dict:
    """核心: 加载投注页面 → 读取赔率 → 返回JSON"""
    async with state.lock:
        # 检查缓存是否有效
        now = time.time()
        if state.last_result and (now - state.last_ts) < CACHE_TTL:
            return {**state.last_result, "cached": True, "cached_ts": state.last_ts}

        await ensure_browser()
        page = state.page

        try:
            print(f"[Gateway] 加载页面...")
            await page.goto(state.target_url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(3000)

            # 检查登录状态
            current_url = page.url
            if "login" in current_url.lower():
                print(f"[Gateway] ⚠ 被重定向到登录: {current_url}")
                return {"error": "登录失效, 请刷新cookies", "login_url": current_url}

            # 提取队名
            title = await page.title()
            import re
            parts = re.split(r'[vs\-–—]', title, maxsplit=1)
            home_team = parts[0].strip()[:20] if len(parts) >= 2 else ""
            away_team = parts[1].strip()[:20] if len(parts) >= 2 else ""

            # 抓取赔率
            data = await OddsScraper.scrape(page)
            if not data:
                # 再等一轮渲染
                await page.wait_for_timeout(3000)
                data = await OddsScraper.scrape(page)

            result = {
                "success": data is not None,
                "data": data,
                "home": home_team or (data.get("teams", [""])[0] if data else ""),
                "away": away_team or (data.get("teams", [""])[1:] or [""])[0] if data else "",
                "ts": int(now * 1000),
                "url": state.target_url,
                "cached": False,
            }

            if data:
                result["odds_1x2"] = {
                    "home": data.get("h"),
                    "draw": data.get("d"),
                    "away": data.get("a"),
                }
                result["handicap"] = data.get("ah", [])
                result["over_under"] = data.get("ou", [])
                result["correct_score"] = data.get("cs", [])
                result["live_score"] = data.get("score")
                result["minute"] = data.get("minute")
                print(f"[Gateway] ✓ 成功提取赔率 | {result['odds_1x2']}")
            else:
                print(f"[Gateway] ⚠ 页面已加载但未检测到赔率数据")

            # 更新缓存
            state.last_result = result
            state.last_ts = now

            return result

        except Exception as e:
            print(f"[Gateway] 抓取异常: {e}")
            return {"error": str(e), "success": False}


@app.on_event("startup")
async def startup():
    """启动时预初始化浏览器"""
    print("[Gateway] 服务启动中...")
    try:
        await ensure_browser()
        print("[Gateway] 浏览器预初始化完成")
    except Exception as e:
        print(f"[Gateway] 浏览器初始化失败 (将延迟到首次请求): {e}")


@app.on_event("shutdown")
async def shutdown():
    """清理浏览器资源"""
    if state.browser:
        try:
            await state.browser.close()
            print("[Gateway] 浏览器已关闭")
        except Exception:
            pass


# ── API 端点 ──

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "browser_connected": state.browser is not None and state.browser.is_connected(),
        "cookies_loaded": bool(state.cookies_path),
        "target": state.target_url,
        "last_fetch_ts": state.last_ts,
    }


@app.get("/api/odds/live")
async def odds_live(enName: str = Query("YBTY", description="投注页面联赛编码")):
    """获取最新实时赔率 (必要时启动Playwright渲染)"""
    # 更新target URL (支持不同联赛)
    if enName:
        state.target_url = f"https://www.08a2zp.vip:9967/game/sport/ob?enName={enName}"

    result = await fetch_live_odds()
    return result


@app.get("/api/odds/cached")
async def odds_cached():
    """返回上次抓取的缓存结果 (无额外开销)"""
    if state.last_result:
        return {**state.last_result, "cached": True, "cached_age": time.time() - state.last_ts}
    return {"error": "暂无缓存", "success": False}


@app.post("/api/odds/refresh")
async def odds_refresh(enName: str = Query("YBTY")):
    """强制刷新 (清除缓存)"""
    async with state.lock:
        state.last_result = None
        state.last_ts = 0
    if enName:
        state.target_url = f"https://www.08a2zp.vip:9967/game/sport/ob?enName={enName}"
    result = await fetch_live_odds()
    return result


def main():
    import uvicorn
    import argparse

    parser = argparse.ArgumentParser(description="哨响AI 实时赔率网关")
    parser.add_argument("--port", type=int, default=9112, help="监听端口 (默认9112)")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认127.0.0.1)")
    parser.add_argument("--cookies", default="", help="cookies.json 路径")
    parser.add_argument("--url", default=DEFAULT_URL, help="投注页面URL")
    args = parser.parse_args()

    state.cookies_path = args.cookies
    state.target_url = args.url

    print(f"\n{'='*50}")
    print(f"哨响AI 实时赔率网关 v2.0")
    print(f"{'='*50}")
    print(f"  监听:    http://{args.host}:{args.port}")
    print(f"  目标:    {state.target_url}")
    print(f"  Cookies: {state.cookies_path or '未配置(无登录)'}")
    print(f"  前端调用: fetch('http://{args.host}:{args.port}/api/odds/live')")
    print(f"{'='*50}\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
