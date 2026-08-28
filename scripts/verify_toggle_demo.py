"""验证 toggle: 改 5 个 chip badge 为 0, 截 chip 区."""
import time, json
from playwright.sync_api import sync_playwright

JS_MOCK = r"""
() => {
  const all = Array.from(document.querySelectorAll('span')).find(s => (s.textContent||'').trim() === '全部');
  if (!all) return 'no all chip';
  let n = all.nextElementSibling, modified = 0;
  for (let i = 0; i < 5 && n; i++, n = n.nextElementSibling) {
    const badge = n.querySelector('span:last-child');
    if (badge && /^\d+$/.test((badge.textContent||'').trim())) {
      badge.textContent = '0';
      badge.className = 'text-[9px] px-1 rounded bg-ember-500/15 text-ember-500';
      modified++;
    }
  }
  return 'modified=' + modified;
}
"""

JS_PROBE = r"""
() => {
  const all = Array.from(document.querySelectorAll('span')).find(s => (s.textContent||'').trim() === '全部');
  let n = all.nextElementSibling, chips = [];
  while (n) { chips.push((n.textContent||'').trim()); n = n.nextElementSibling; }
  return {
    totalChips: chips.length,
    first3: chips.slice(0, 3),
    last3: chips.slice(-3),
    hasToggle: chips.some(c => c.includes('无赛程') || c === '收起')
  };
}
"""

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = b.contexts[0].pages[0]
    page.goto("http://127.0.0.1:9000/", wait_until="domcontentloaded")
    try: page.wait_for_function("() => document.querySelectorAll('button').length >= 100", timeout=12000)
    except: pass
    time.sleep(0.5)
    print("mock:", page.evaluate(JS_MOCK))
    time.sleep(0.3)
    print("probe:", json.dumps(page.evaluate(JS_PROBE), ensure_ascii=False, indent=2))
    page.screenshot(path=r"D:\\Architecture\\.edge_agent_profile\\home_collapse_demo.png", full_page=False, clip={"x": 80, "y": 0, "width": 970, "height": 180})
    print("screenshot saved")
