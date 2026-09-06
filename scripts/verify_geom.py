"""精准验证: 三栏几何 + Dashboard 头 MiniMetric 文本."""
import json, time
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = b.contexts[0].pages[0]
    page.goto("http://127.0.0.1:9000/", wait_until="domcontentloaded")
    try: page.wait_for_function("() => document.querySelectorAll('button').length >= 100", timeout=12000)
    except: pass
    time.sleep(1.0)
    info = page.evaluate("""() => {
      const search = document.querySelector('input[placeholder="搜索联赛"]');
      const aside = search ? search.closest('aside') : null;
      const asideRect = aside ? aside.getBoundingClientRect() : null;
      // 右栏 LivePanel: 找含 "全场波胆" 的祖先容器
      let liveWrap = null;
      const all = Array.from(document.querySelectorAll('*'));
      for (const e of all) {
        if ((e.textContent||'').includes('全场波胆')) { liveWrap = e; break; }
      }
      // 向上找带固定宽度的 panel 容器
      let panel = liveWrap;
      let liveW = null;
      while (panel && panel !== document.body) {
        const w = panel.getBoundingClientRect().width;
        if (w > 200 && w < 360) { liveW = Math.round(w); break; }
        panel = panel.parentElement;
      }
      return {
        asideWidth: asideRect ? Math.round(asideRect.width) : null,
        hasAnalyzableMetric: document.body.textContent.includes('可分析'),
        hasLiveMetric: document.body.textContent.includes('进行中'),
        livePanelWidth: liveW,
        viewportW: window.innerWidth,
      };
    }""")
    print(json.dumps(info, ensure_ascii=False, indent=2))
