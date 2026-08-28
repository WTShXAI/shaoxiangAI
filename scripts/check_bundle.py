"""
检查浏览器实际加载的 LeagueSchedule 块里有没有 showEmpty 状态 + 拆分逻辑。
"""
import time
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = b.contexts[0].pages[0]
    page.goto("http://127.0.0.1:9000/", wait_until="domcontentloaded")
    try: page.wait_for_function("() => document.querySelectorAll('button').length >= 100", timeout=12000)
    except: pass

    # 取当前 JS bundle URL + 检查是否含"个无赛程"字符串
    info = page.evaluate("""() => {
      const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
      const hasEmpty = scripts.some(u => u && u.includes('/assets/'));
      return {
        scripts: scripts.filter(u => u && u.includes('/assets/')),
        // 找按钮区里"全部"chip 后面的所有 chip 文本
        chipsAfterAll: (() => {
          const allChip = Array.from(document.querySelectorAll('span')).find(s => (s.textContent||'').trim() === '全部');
          if (!allChip) return [];
          let n = allChip.nextElementSibling, out = [];
          for (let i = 0; i < 30 && n; i++, n = n.nextElementSibling) {
            out.push((n.textContent||'').trim().slice(0, 30));
          }
          return out;
        })()
      };
    }""")
    import json
    print(json.dumps(info, ensure_ascii=False, indent=2))
