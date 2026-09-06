"""
Verify: 0 场联赛 chip 是否真的不渲染了; "+N 个无赛程" 按钮是否出现。
"""
import asyncio, time, json
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = b.contexts[0].pages[0]
    page.goto("http://127.0.0.1:9000/", wait_until="domcontentloaded")
    try: page.wait_for_load_state("networkidle", timeout=10000)
    except: pass
    # 用业务就绪信号等 home 渲染完成
    try: page.wait_for_function("() => document.querySelectorAll('button').length >= 100", timeout=12000)
    except: pass
    time.sleep(1.0)

    info = page.evaluate("""() => {
      const chips = Array.from(document.querySelectorAll('span'))
        .filter(s => /\\d+/.test(s.textContent || '') && (s.textContent||'').length < 30)
        .map(s => (s.textContent||'').trim());
      const zeroChips = chips.filter(t => /\\b0\\b/.test(t) || t.endsWith(' 0'));
      const btn0 = Array.from(document.querySelectorAll('span'))
        .find(s => (s.textContent||'').includes('个无赛程'));
      const allLeagueText = (document.querySelector('span.bg-field-500\\\\/15, span.bg-field-500\\\\/15')||{}).textContent || '';
      // 取 chip 区里含数字 0 的前 5 个文本作为样本
      return {
        totalChips: chips.length,
        zeroChipsCount: zeroChips.length,
        sampleZeroChips: zeroChips.slice(0, 8),
        toggleButtonText: btn0 ? (btn0.textContent||'').trim() : null,
        toggleVisible: !!btn0,
        allLeagueBadge: Array.from(document.querySelectorAll('span'))
          .filter(s => /个联赛/.test(s.textContent||''))
          .map(s => (s.textContent||'').trim())[0] || ''
      };
    }""")
    import json
    print(json.dumps(info, ensure_ascii=False, indent=2))
    # 同时也截一张全屏图给涛哥
    page.screenshot(path=r"D:\\Architecture\\.edge_agent_profile\\home_after_collapse.png", full_page=False)
    print("screenshot saved")
