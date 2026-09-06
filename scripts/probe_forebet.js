// forebet 可抓性验证: playwright-core 驱动本地 Edge, 过 Cloudflare 挑战抓数据
const { chromium } = require('D:/Architecture/frontend/node_modules/playwright-core');

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const URL = process.argv[2] || 'https://www.forebet.com/en/football-tips-and-predictions-for-today';

(async () => {
  const browser = await chromium.launch({
    executablePath: EDGE,
    headless: false,
    args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
  });
  const ctx = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
    locale: 'en-US',
  });
  const page = await ctx.newPage();
  try {
    await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
    // 等 Cloudflare 挑战 (最多 20s)
    for (let i = 0; i < 20; i++) {
      const title = await page.title().catch(() => '');
      if (!title.includes('Just a moment')) break;
      await page.waitForTimeout(1000);
    }
    const title = await page.title().catch(() => '(无标题)');
    console.log('最终标题:', title);
    const content = await page.content();
    console.log('页面大小:', content.length, 'bytes');
    // 检测关键标记
    const hasForebet = content.includes('forebet') || title.includes('Forebet');
    const hasTips = content.includes('prediction') || content.includes('Probability');
    console.log('含 forebet 标记:', hasForebet, '| 含预测数据:', hasTips);
    // 抓取样本: 比赛行
    const text = await page.evaluate(() => document.body ? document.body.innerText.slice(0, 500) : '');
    console.log('正文前 500 字:', text.replace(/\n+/g, ' | ').slice(0, 500));
    await page.screenshot({ path: 'logs/forebet_probe.png', fullPage: false });
    console.log('截图已存 logs/forebet_probe.png');
  } catch (e) {
    console.log('访问失败:', e.message);
  } finally {
    await browser.close();
  }
})();