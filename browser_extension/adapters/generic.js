/**
 * Generic 1X2 Odds Adapter
 * 通用赔率提取器 — 检测页面中最可能的 1X2 赔率表格
 * 优先级: 带 odds/price 类名的表格 > 数据表格 > title 回退
 */
window.__GenericAdapter__ = {
  extract() {
    // 策略1: 按 data-odds / class 找表格
    const oddsRows = document.querySelectorAll(
      '[data-odds], [data-market="1x2"], [data-sport="soccer"] tr, ' +
      '.odds-table tr, .bet-table tr, .market-table tr'
    );
    for (const row of oddsRows) {
      const cells = row.querySelectorAll('td, th');
      const nums = [];
      for (const c of cells) {
        const v = parseFloat(c.textContent?.trim());
        if (v > 1.0 && v < 100) nums.push(v);
      }
      if (nums.length === 3) {
        const teams = window.__GenericAdapter__._teams();
        return { home: teams.home, away: teams.away, h: nums[0], d: nums[1], a: nums[2] };
      }
    }

    // 策略2: 找所有包含恰好3个 1.0-100 数字的行
    const allRows = document.querySelectorAll('tr');
    for (const row of allRows) {
      const nums = [];
      const cells = row.querySelectorAll('td');
      for (const c of cells) {
        const v = parseFloat(c.textContent?.trim());
        if (!isNaN(v) && v > 1.0 && v < 100) nums.push(v);
      }
      if (nums.length === 3) {
        const teams = window.__GenericAdapter__._teams();
        return { home: teams.home, away: teams.away, h: nums[0], d: nums[1], a: nums[2] };
      }
    }

    return null;
  },

  _teams() {
    const title = document.title || '';
    const vsMatch = title.match(/(.+?)\s+(?:vs\.?|v\.?|-)\s+(.+?)(?:\s*[-–|].*)?$/i);
    if (vsMatch) return { home: vsMatch[1].trim(), away: vsMatch[2].trim() };
    // 回退: 找 h1/h2 中的对阵
    const h1 = document.querySelector('h1, h2');
    if (h1) {
      const t = h1.textContent || '';
      const m = t.match(/(.+?)\s+(?:vs\.?|v\.?|-)\s+(.+)/i);
      if (m) return { home: m[1].trim(), away: m[2].trim() };
    }
    return { home: 'UnknownHome', away: 'UnknownAway' };
  }
};
