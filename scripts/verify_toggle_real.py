"""用 ?demo=zero 触发 5 个 0 场联赛, 验证 toggle 按钮 + 截图."""
import time
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = b.contexts[0].pages[0]
    page.goto("http://127.0.0.1:9000/?demo=zero", wait_until="domcontentloaded")
    try: page.wait_for_function("() => document.querySelectorAll('button').length >= 100", timeout=12000)
    except: pass
    time.sleep(0.8)

    # 默认态截图: 应该看到 5 个 chip 消失 + 末尾 "+5 个无赛程" 按钮
    page.screenshot(path=r"D:\\Architecture\\.edge_agent_profile\\home_collapsed.png", full_page=False, clip={"x": 80, "y": 0, "width": 970, "height": 180})
    print("home_collapsed.png saved (默认折叠态)")

    # 找 toggle 按钮, 点击
    page.evaluate(r"""() => {
      const btn = Array.from(document.querySelectorAll('span')).find(s => (s.textContent||'').includes('个无赛程'));
      if (btn) btn.click();
    }""")
    time.sleep(0.4)
    page.screenshot(path=r"D:\\Architecture\\.edge_agent_profile\\home_expanded.png", full_page=False, clip={"x": 80, "y": 0, "width": 970, "height": 200})
    print("home_expanded.png saved (展开态)")
