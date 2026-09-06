/**
 * William Hill DOM Adapter (样板)
 * WH 页面结构:
 *   赔率表格通常有 .event-price 或 .sp-o-market 容器
 *   1X2三列: home / draw / away 按钮内有 price 文本
 */
window.__WHAdapter__ = {
  extract() {
    // 找 WH 特有的赔率容器
    const containers = document.querySelectorAll(
      '.sp-o-market, .event-price, [data-market-type="WIN_DRAW_WIN"], ' +
      '[class*="Outright"] [class*="price"], [data-testid="odds-button"]'
    );

    if (containers.length >= 3) {
      const prices = [];
      containers.forEach((el, i) => {
        if (i >= 3) return;
        const txt = el.textContent?.trim().replace(/[^\d.]/g, '') || '';
        const v = parseFloat(txt);
        if (v > 1 && v < 100) prices.push(v);
      });

      if (prices.length === 3) {
        const title = document.title || '';
        const vs = title.match(/(.+?)\s+(?:vs?\.?|-)\s+(.+?)(?:[\s-].*)?$/i);
        return {
          home: vs ? vs[1].trim() : 'UnknownHome',
          away: vs ? vs[2].trim() : 'UnknownAway',
          h: prices[0], d: prices[1], a: prices[2],
        };
      }
    }

    // 回退: 用父容器的 data-event-name 属性
    const eventEl = document.querySelector('[data-event-name]');
    if (eventEl) {
      const eventName = eventEl.getAttribute('data-event-name') || '';
      const parts = eventName.split(/\s+(?:vs?\.?|v)\s+/i);
      // 找这个事件下的所有价格按钮
      const btns = eventEl.querySelectorAll('[class*="price"], [class*="odds"], [class*="btn"]');
      const nums = [];
      btns.forEach(b => {
        const v = parseFloat((b.textContent || '').trim());
        if (!isNaN(v) && v > 1 && v < 100) nums.push(v);
      });
      if (nums.length >= 3 && parts.length >= 2) {
        return {
          home: parts[0].trim(),
          away: parts[1].trim(),
          h: nums[0], d: nums[1], a: nums[2],
        };
      }
    }

    return null;
  }
};
