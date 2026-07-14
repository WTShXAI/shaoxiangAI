/**
 * 哨响AI — Content Script
 * 注入博彩网站页面, 自动检测赔率表格, 推送到 background.js
 *
 * 适配器优先级:
 *  1. 域名专用适配器 (如 williamhill.js 检测 WH 特有 DOM)
 *  2. 通用适配器 generic.js (检测标准 1X2 表格)
 */

(function () {
  'use strict';

  const HOST = window.location.hostname;

  // 去重: 同一页面只推送一次 (除非用户手动触发)
  let lastPushed = '';

  /** 提取页面标题中的主客队 */
  function extractTeamsFromTitle() {
    const title = document.title || '';
    // 常见格式: "Team A vs Team B" / "Team A - Team B"
    const vsMatch = title.match(/(.+?)\s+(?:vs\.?|v\.?|-)\s+(.+?)(?:\s*[-–|].*)?$/i);
    if (vsMatch) {
      return { home: vsMatch[1].trim(), away: vsMatch[2].trim() };
    }
    return null;
  }

  /** 主入口: 检测赔率并推送 */
  function detectAndPush() {
    let odds = null;

    // 1. 尝试域名专用适配器
    if (HOST.includes('williamhill')) {
      odds = window.__WHAdapter__ ? window.__WHAdapter__.extract() : null;
    }

    // 2. 回退到通用适配器
    if (!odds && window.__GenericAdapter__) {
      odds = window.__GenericAdapter__.extract();
    }

    if (!odds) {
      console.log('[哨响AI] 未检测到赔率表格 (当前页: ' + HOST + ')');
      return;
    }

    const key = `${odds.home}|${odds.away}`;
    if (key === lastPushed) return;
    lastPushed = key;

    const data = {
      home: odds.home,
      away: odds.away,
      source: HOST.replace('www.', ''),
      h: odds.h,
      d: odds.d,
      a: odds.a,
    };

    // 尝试读取实时比分 (如果页面有)
    const scoreEl = document.querySelector('[data-testid="score"], .score, .live-score');
    if (scoreEl) {
      const txt = scoreEl.textContent?.trim() || '';
      const sm = txt.match(/(\d+)\s*[-–:]\s*(\d+)/);
      if (sm) {
        data.score = `${sm[1]}-${sm[2]}`;
      }
    }
    const minEl = document.querySelector('[data-testid="minute"], .minute, .clock');
    if (minEl) {
      const m = parseInt(minEl.textContent?.trim() || '');
      if (!isNaN(m)) data.minute = m;
    }

    console.log('[哨响AI] 推送赔率:', data);
    chrome.runtime.sendMessage({ type: 'odds_data', data }, (resp) => {
      if (chrome.runtime.lastError) {
        console.warn('[哨响AI] 推送失败:', chrome.runtime.lastError);
      } else {
        console.log('[哨响AI] 推送响应:', resp);
      }
    });
  }

  // 页面加载后延迟检测 (等DOM渲染)
  setTimeout(detectAndPush, 2000);

  // 监听手动触发事件 (popup触发)
  window.addEventListener('message', (e) => {
    if (e.data?.type === 'shouxiang_scan') {
      lastPushed = '';  // 重置去重
      detectAndPush();
    }
  });
})();
