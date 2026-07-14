"""
Playwright 浏览器自动化代理
===========================
独立进程: 打开真实Chromium浏览器 → 访问投注页面 → 读取DOM赔率 → WebSocket推送

用法:
    python playwright_agent.py --url "https://www.08a2zp.vip:9967/game/sport/ob?enName=YBTY"
"""
from __future__ import annotations
import asyncio, json, time, sys, os, re
from playwright.async_api import async_playwright

BRIDGE_WS = "ws://127.0.0.1:9000/ws/realtime"
THROTTLE_MS = 500  # DOM变化检测间隔

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


class OddsScraper:
    """赔率抓取器 — 语义锚点 + 结构性扫描"""

    @staticmethod
    async def scrape(page) -> dict | None:
        """从当前页面提取所有可用赔率数据"""
        result = await page.evaluate("""() => {
            const num = s => { const m = String(s).match(/(\\d+\\.\\d{2})/); return m ? parseFloat(m[1]) : null; };
            const ctx = { h:null, d:null, a:null, ah:[], ou:[], score:null, minute:null, cs:[] };

            // 1. 1X2 语义锚点
            document.querySelectorAll('*').forEach(el => {
                const t = (el.innerText || '').trim();
                if(/主胜|home|win/i.test(t) && t.length < 10) ctx._hEl = el;
                if(/平局|draw|tie/i.test(t) && t.length < 10) ctx._dEl = el;
                if(/客胜|away/i.test(t) && t.length < 10) ctx._aEl = el;
            });

            const nearOdds = el => {
                if(!el) return null;
                let cur = el;
                for(let i=0; i<5; i++){
                    cur = cur.parentElement;
                    if(!cur) break;
                    const kids = cur.querySelectorAll('*');
                    for(const k of kids){
                        const v = num(k.innerText);
                        if(v && v >= 1.01 && v <= 999) return v;
                    }
                }
                return null;
            };
            ctx.h = nearOdds(ctx._hEl);
            ctx.d = nearOdds(ctx._dEl);
            ctx.a = nearOdds(ctx._aEl);

            // 2. 亚盘 — 找"让球"相关行
            document.querySelectorAll('tr, .row, [class*=row]').forEach(row => {
                const text = row.innerText || '';
                if(/让球|handicap|盘口/i.test(text)){
                    const nums = [];
                    row.querySelectorAll('td, span, div').forEach(c => {
                        const v = num(c.innerText);
                        if(v) nums.push(v);
                    });
                    if(nums.length >= 2) ctx.ah.push(nums);
                }
            });

            // 3. 大小球
            document.querySelectorAll('tr, .row, [class*=row]').forEach(row => {
                const text = row.innerText || '';
                if(/大小|over.*under|总进球/i.test(text)){
                    const nums = [];
                    row.querySelectorAll('td, span, div').forEach(c => {
                        const v = num(c.innerText);
                        if(v) nums.push(v);
                    });
                    if(nums.length >= 2) ctx.ou.push(nums);
                }
            });

            // 4. 波胆 — 如果页面有波胆表
            document.querySelectorAll('table').forEach(tbl => {
                if(/波胆|correct score|cs:/i.test(tbl.innerText || '')){
                    tbl.querySelectorAll('td, th').forEach(c => {
                        const v = num(c.innerText);
                        if(v && v > 1.5) ctx.cs.push(v);
                    });
                }
            });

            // 5. 比分 & 时间
            const title = document.title || '';
            const sm = title.match(/(\\d+)\\s*[:：-]\\s*(\\d+)/);
            if(sm) ctx.score = sm[1] + ':' + sm[2];
            const mm = document.body.innerText.match(/(\\d{1,3})['\\u2032]/);
            if(mm) ctx.minute = parseInt(mm[1]);

            // 清理临时属性
            delete ctx._hEl; delete ctx._dEl; delete ctx._aEl;
            return ctx;
        }""")

        if not result.get('h'):
            return None
        return result


async def run_agent(target_url: str):
    """主循环: 连接页面 → 定时抓取 → WS推送"""
    print(f"[Agent] 启动 Playwright, 目标: {target_url}")

    async with async_playwright() as p:
        # 启动 Chromium (有头模式, 用户可见)
        browser = await p.chromium.launch(
            headless=False,  # 有头模式—用户可以看到浏览器操作
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-infobars',
                '--ignore-certificate-errors',  # 忽略自签名证书
            ]
        )

        context = await browser.new_context(
            viewport={'width': 1400, 'height': 900},
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        )

        page = await context.new_page()

        # 注入反检测脚本 (页面加载前)
        await page.add_init_script(STEALTH_JS)

        # 连接 WebSocket 到 bridge
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
                print("[Agent] WebSocket 已连接")
                return True
            except Exception:
                ws_retry += 1
                return False

        # 导航到目标页面
        print(f"[Agent] 正在加载页面...")
        try:
            await page.goto(target_url, wait_until='networkidle', timeout=30000)
        except Exception as e:
            print(f"[Agent] 页面加载警告: {e}")
            await page.wait_for_timeout(5000)

        print(f"[Agent] 页面已加载, 开始监控赔率...")

        last_hash = ''
        home_team = ''
        away_team = ''

        while True:
            try:
                # 提取标题中的队名
                title = await page.title()
                parts = re.split(r'[vs\-–—]', title, maxsplit=1)
                if len(parts) >= 2:
                    home_team = parts[0].strip()[:20]
                    away_team = parts[1].strip()[:20]

                # 抓取赔率
                data = await OddsScraper.scrape(page)
                if data:
                    data['home'] = home_team or '主队'
                    data['away'] = away_team or '客队'
                    data['ts'] = int(time.time() * 1000)
                    data['source'] = 'playwright'

                    h = f"{data['h']:.2f}" if data.get('h') else '0'
                    d_hash = f"{data.get('d',0):.2f}" if data.get('d') else '0'
                    a_hash = f"{data.get('a',0):.2f}" if data.get('a') else '0'
                    current_hash = f"{h}|{d_hash}|{a_hash}"

                    if current_hash != last_hash:
                        last_hash = current_hash
                        print(f"[Agent] 赔率变化: {h}/{d_hash}/{a_hash} — {title[:30]}")

                        # 推送到 bridge
                        if await ensure_ws():
                            try:
                                await ws.send(json.dumps({
                                    'type': 'odds_update',
                                    'payload': data
                                }, ensure_ascii=False))

                                # 同时 HTTP 推送 (兼容旧接口)
                                import aiohttp
                                async with aiohttp.ClientSession() as session:
                                    await session.post(
                                        f'http://127.0.0.1:9000/api/terminal/ingest',
                                        json=data,
                                        timeout=aiohttp.ClientTimeout(total=2)
                                    )
                            except Exception as e:
                                print(f"[Agent] 推送失败: {e}")
                                ws = None  # 重置 WS 以便重连

            except Exception as e:
                print(f"[Agent] 抓取循环异常: {e}")

            await asyncio.sleep(THROTTLE_MS / 1000)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://www.08a2zp.vip:9967/game/sport/ob?enName=YBTY")
    args = parser.parse_args()

    asyncio.run(run_agent(args.url))
