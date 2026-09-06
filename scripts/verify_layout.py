"""验证首页三栏重构后的 DOM 结构."""
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
      const txt = (el) => (el ? (el.textContent||'').trim() : '');
      // 左栏搜索框
      const search = document.querySelector('input[placeholder="搜索联赛"]');
      // Dashboard 头 MiniMetric 标签
      const metricLabels = Array.from(document.querySelectorAll('div')).filter(d =>
        ['今日','进行中','可分析'].includes((d.textContent||'').trim()) && d.children.length <= 2
      ).map(d => txt(d));
      // 左栏 "全部联赛" 按钮
      const allBtn = Array.from(document.querySelectorAll('button')).find(b => (b.textContent||'').trim() === '全部联赛');
      // 右栏 LivePanel "全场波胆"
      const livePanel = Array.from(document.querySelectorAll('*')).some(e => (e.textContent||'') && (e.textContent||'').includes('全场波胆'));
      // 旧 chip 横排 "全部" span 是否还在 (应该不在)
      const oldAllChip = Array.from(document.querySelectorAll('span')).find(s => (s.textContent||'').trim() === '全部' && s.className.includes('cursor-pointer'));
      // 左栏联赛列表项数 (含 badge 的 button)
      const leagueBtns = Array.from(document.querySelectorAll('button')).filter(b => {
        const t = (b.textContent||'').trim();
        return /\\d/.test(t) && t.length < 40 && !['全部联赛','加载更多','收起'].includes(t);
      }).length;
      return {
        hasSearchBox: !!search,
        dashboardMetrics: metricLabels,
        hasAllLeagueBtn: !!allBtn,
        hasLivePanel: livePanel,
        oldChipRowGone: !oldAllChip,
        leagueListBtnCount: leagueBtns,
        bodyHasLeagueSchedule: document.body.textContent.includes('联赛赛程'),
      };
    }""")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    page.screenshot(path=r"D:\Architecture\.edge_agent_profile\home_threecol.png", full_page=False)
    print("screenshot saved")
