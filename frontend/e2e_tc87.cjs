const { chromium } = require('playwright-core');

(async () => {
  const errors = [];
  let browser;
  try {
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-gpu'] });
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text()); });
    page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));

    await page.goto('http://127.0.0.1:9000/', { waitUntil: 'load', timeout: 30000 });
    await page.getByText('全部', { exact: true }).first().waitFor({ timeout: 15000 });
    await page.waitForTimeout(9000);

    const info = await page.evaluate(() => {
      const txt = document.body.innerText || '';
      const dateTabRe = /\d{1,2}\.\d{1,2} 周[一二三四五六日]/;
      const scoreRe = /\b\d+:\d+\b/g;
      return {
        hasAllChip: txt.includes('全部'),
        hasDateTab: dateTabRe.test(txt),
        hasTitle: txt.includes('赛程'),
        oddsDecimal: /\d+\.\d+/.test(txt),
        scoreRows: (txt.match(scoreRe) || []).length,
        bodyLen: txt.length,
      };
    });
    await page.screenshot({ path: 'D:/Architecture/frontend/tc87_e2e.png', fullPage: false });

    // 点击第一行打开分析弹窗，验证新头部（联赛/比分/状态/Tabs）
    let modalInfo = { opened: false, hasHeader: false, hasTabs: false };
    const rowCount = await page.locator('div.group').count();
    if (rowCount > 0) {
      await page.locator('div.group').first().evaluate(el => el.click());
      try {
        await page.waitForTimeout(1500);
        const backdrop = page.locator('div.fixed.inset-0.z-50');
        await backdrop.waitFor({ timeout: 5000 });
        modalInfo.opened = true;
        // 若弹出 18+ 年龄验证，先点击进入
        const enterBtn = page.getByText('我已满 18 岁，进入');
        if (await enterBtn.count()) await enterBtn.click();
        await page.waitForTimeout(1500);
        const txt = await backdrop.innerText();
        modalInfo.hasHeader = /LIVE DECODE|概率排名主推|全链路决策卡/.test(txt);
        modalInfo.hasTabs = /概率排名主推/.test(txt) && /全链路决策卡/.test(txt);
        modalInfo.hasScoreOrVs = /\d+:\d+|VS/.test(txt);
        await page.screenshot({ path: 'D:/Architecture/frontend/tc87_modal_e2e.png', fullPage: false });
      } catch {}
    }

    console.log('E2E_OK ' + JSON.stringify({ ...info, modalInfo, errors }, null, 2));
  } catch (e) {
    console.log('E2E_FAIL: ' + e.message);
    console.log(JSON.stringify(errors, null, 2));
  } finally {
    if (browser) await browser.close();
  }
})();
