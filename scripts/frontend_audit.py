"""哨响AI 前端全面审计 — Playwright CDP 驱动本地 Edge。

捕获每页: 全页截图 + 控制台错误/警告 + 失败请求(>=400/net error)
         + 结构化 DOM(导航/标题/按钮/关键文本/可见错误条)
输出: .edge_agent_profile/audit_<route>.png + audit_report.json

健壮性:
- txt() 用 textContent 回退, 避免 SVG/自定义元素的 innerText undefined 抛错
- extract_dom 整体 try/except; 失败也保留 console / failedRequests / 截图路径
- 任意路由异常被 main() 捕获, 不让单点崩全场
"""
import json
import os
import time

from playwright.sync_api import sync_playwright

EDGE_CDP = "http://127.0.0.1:9222"
BASE = "http://127.0.0.1:9000"
OUT = "D:/Architecture/.edge_agent_profile"
ROUTES = [
    ("home", "/"),
    ("live", "/live-scores"),
    ("results", "/match-results"),
]

os.makedirs(OUT, exist_ok=True)


EXTRACT_DOM_JS = r"""() => {
    // 健壮 txt: textContent 始终是 string, 无 undefined 抛错风险
    const txt = (el) => {
        if (!el) return '';
        try {
            const s = (el.innerText != null) ? el.innerText : (el.textContent != null ? el.textContent : '');
            return (s || '').toString().trim().replace(/\s+/g, ' ');
        } catch (e) { return ''; }
    };
    const safe = (q) => { try { return Array.from(document.querySelectorAll(q)); } catch (e) { return []; } };
    const nav = safe('nav a, [class*=sidebar] a, [class*=Sidebar] a, header a')
        .map(a => txt(a)).filter(Boolean).slice(0, 30);
    const headings = safe('h1,h2,h3').map(h => txt(h)).filter(Boolean).slice(0, 20);
    const buttonsRaw = safe('button, [role=button], a[class*=btn]');
    const buttons = buttonsRaw.map(b => txt(b)).filter(Boolean);
    // 错误条更克制: 只看特定容器, 不再 querySelectorAll('*') 避免性能+抛错
    const errContainers = safe('[class*=error], [class*=Error], [class*=empty], [class*=Empty], [class*=notFound]');
    const errLike = errContainers.map(e => txt(e)).filter(t => t && t.length < 200).slice(0, 8);
    const bodyText = txt(document.body);
    return {
        title: document.title,
        url: location.href,
        nav,
        headings,
        buttonCount: buttons.length,
        buttons: buttons.slice(0, 60),
        errLike,
        bodyLen: bodyText.length,
        scrollHeight: document.body.scrollHeight,
        viewportW: window.innerWidth,
        viewportH: window.innerHeight,
        // 增加: 检测 Google Fonts 是否成功加载 (1 = 加载, 0 = 未加载/失败)
        fontsReady: (document.fonts ? document.fonts.status : 'unknown'),
    };
}"""


def audit_route(browser, key, path):
    ctx = browser.contexts[0]
    page = ctx.new_page()
    console = []
    failed = []

    def on_console(msg):
        if msg.type in ("error", "warning"):
            console.append({"type": msg.type, "text": msg.text[:400]})

    def on_req_failed(req):
        failed.append({"url": req.url[:300], "err": str(req.failure)[:200]})

    def on_resp(resp):
        if resp.status >= 400:
            failed.append({"url": resp.url[:300], "status": resp.status})

    page.on("console", on_console)
    page.on("requestfailed", on_req_failed)
    page.on("response", on_resp)

    t0 = time.time()
    nav_err = None
    # 清缓存避免上次构建的旧 chunk 残留在浏览器里, 导致 React 拉到过期代码
    try:
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.clearBrowserCache")
        cdp.send("Network.clearBrowserCookies")
    except Exception:
        pass
    try:
        page.goto(BASE + path, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        nav_err = f"goto failed: {e}"
    # 等 SPA 渲染完成 + 一次轮询
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(2.5)
    # Home (LeagueSchedule) 会并行拉 20 个 /api/leagues/{sk}/fixtures,
    # networkidle 后还有数据 settle 期, 等"分析"按钮 ≥100 OR 12s 兜底.
    if path == "/":
        settle_deadline = time.time() + 12
        last_n = -1
        while time.time() < settle_deadline:
            try:
                n = page.evaluate(
                    "() => Array.from(document.querySelectorAll('button'))"
                    ".filter(b => b.textContent.trim() === '分析').length"
                )
            except Exception:
                n = 0
            if n >= 100 or n == last_n and n > 0:
                break
            last_n = n
            time.sleep(0.4)

    shot = os.path.join(OUT, f"audit_{key}.png")
    shot_ok = True
    try:
        page.screenshot(path=shot, full_page=True)
    except Exception as e:
        shot_ok = False
        shot = f"SCREENSHOT_FAIL: {e}"

    dom = {
        "title": "", "url": BASE + path,
        "loadSec": round(time.time() - t0, 1),
        "screenshot": shot if shot_ok else None,
        "nav": [], "headings": [], "buttonCount": 0, "buttons": [],
        "errLike": [], "bodyLen": 0,
        "scrollHeight": 0, "viewportW": 0, "viewportH": 0,
        "fontsReady": "unknown",
        "console": console,
        "failedRequests": failed,
        "navErr": nav_err,
    }
    try:
        extracted = page.evaluate(EXTRACT_DOM_JS)
        if isinstance(extracted, dict):
            dom.update(extracted)
    except Exception as e:
        dom["extractDomErr"] = str(e)[:300]

    page.close()
    return {key: dom}


def main():
    report = {}
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(EDGE_CDP)
        for key, path in ROUTES:
            try:
                report.update(audit_route(browser, key, path))
            except Exception as e:
                report[key] = {"fatal": str(e)}
    with open(os.path.join(OUT, "audit_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("[audit] done ->", os.path.join(OUT, "audit_report.json"))


if __name__ == "__main__":
    main()