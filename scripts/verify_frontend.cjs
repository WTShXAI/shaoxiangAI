const { chromium } = require('D:/Architecture/frontend/node_modules/playwright');

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const OUT = 'D:/Architecture/_verify';

(async () => {
  const browser = await chromium.launch({
    executablePath: EDGE,
    headless: true,
    args: ['--no-sandbox', '--disable-gpu']
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('PAGEERROR: ' + e.message));

  // 1) 首页
  await page.goto('http://localhost:9000/', { waitUntil: 'networkidle', timeout: 30000 }).catch(e => console.log('home nav err:', e.message));
  await page.waitForTimeout(3500);
  const rootLen = await page.$eval('#root', el => el.innerHTML.length).catch(() => 0);
  await page.screenshot({ path: OUT + '/shot_home.png', fullPage: false });
  console.log('HOME root_inner_len =', rootLen);

  // 2) 尝试 LiveScores 路由 (SPA hash/history 未知, 两种都试)
  for (const path of ['/livescores', '/#/livescores', '/live-scores']) {
    await page.goto('http://localhost:9000' + path, { waitUntil: 'networkidle', timeout: 30000 }).catch(e => console.log('nav err', path, e.message));
    await page.waitForTimeout(2500);
    await page.screenshot({ path: OUT + '/shot_' + path.replace(/[\/#]/g, '_') + '.png', fullPage: false });
  }

  // 3) 抓页面可见文本, 看是否有"实时"/"比赛"等关键词 (证明数据填充)
  const bodyText = await page.evaluate(() => document.body.innerText.slice(0, 600)).catch(() => '');
  console.log('VISIBLE_TEXT_SAMPLE =', JSON.stringify(bodyText));

  console.log('CONSOLE_ERRORS =', JSON.stringify(errors.slice(0, 12)));
  await browser.close();
})();
