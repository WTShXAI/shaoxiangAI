"""Take a settled screenshot of the home page (waits for fixture data to load)."""
import time
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:9000/"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.clearBrowserCache")
    cdp.send("Network.clearBrowserCookies")
    page.goto(URL, wait_until="domcontentloaded")
    # Wait for analysis buttons (fixture fetch done) or 25s max
    t0 = time.time()
    while time.time() - t0 < 25:
        n = page.evaluate("() => Array.from(document.querySelectorAll('button')).filter(b=>b.textContent.trim()==='分析').length")
        if n >= 100:
            print(f"settled @ {time.time()-t0:.1f}s, analysis_buttons={n}")
            break
        time.sleep(0.5)
    page.screenshot(path=r"D:\Architecture\.edge_agent_profile\home_settled.png", full_page=False)
    print("screenshot saved: home_settled.png")
