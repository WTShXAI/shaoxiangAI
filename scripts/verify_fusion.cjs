const { chromium } = require('D:/Architecture/frontend/node_modules/playwright');

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';

(async () => {
  const browser = await chromium.launch({
    executablePath: EDGE,
    headless: true,
    args: ['--no-sandbox', '--disable-gpu']
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });

  const log = (...a) => console.log(...a);

  await page.goto('http://localhost:9000/analysis-center', { waitUntil: 'domcontentloaded', timeout: 60000 });
  log('已打开 /analysis-center');

  // 等榜单卡片出现 (scan 可能要数十秒)
  try {
    await page.waitForSelector('text=重新扫描', { timeout: 120000 });
    log('扫描控制已渲染');
  } catch (e) {
    log('WARN: 未等到扫描控制:', e.message);
  }

  // 找任意一张可点击的比赛卡 (含 vs 的卡片按钮/div)
  const cardSel = 'div.cursor-pointer';
  let clicked = false;
  try {
    await page.waitForSelector(cardSel, { timeout: 120000 });
    const n = await page.$$eval(cardSel, (els) => els.length);
    log(`找到 ${n} 张可点击卡片`);
    if (n > 0) {
      await page.$$eval(cardSel, (els) => els[0].click());
      clicked = true;
      log('已点击首张卡片');
    }
  } catch (e) {
    log('WARN: 未找到可点击卡片:', e.message);
  }

  if (clicked) {
    // 1) 等抽屉 UI 出现
    try {
      await page.waitForFunction(
        () => /概率排名计算中|概率排名|把握度|主推|跨市场统一排名/.test(document.body.innerText),
        { timeout: 30000 }
      );
      log('抽屉已弹出');
    } catch (e) {
      log('WARN: 抽屉未弹出:', e.message);
    }
    // 2) 等 ranked API 返回并渲染完成(loading 文案消失,出现主推/把握度/跨市场统一排名)
    try {
      await page.waitForFunction(
        () => {
          const t = document.body.innerText;
          return !t.includes('概率排名计算中') && /主推|把握度|跨市场统一排名/.test(t);
        },
        { timeout: 30000 }
      );
      log('ranked 内容已渲染完成');
    } catch (e) {
      log('WARN: ranked 内容未在限定时间内完成渲染:', e.message);
    }
    await page.waitForTimeout(1500);
    await page.screenshot({ path: 'D:/Architecture/_verify/shot_analysis_drawer.png', fullPage: false });
    log('已截图: _verify/shot_analysis_drawer.png');
    // 截抽屉内文确认
    const bodyText = await page.evaluate(() => document.body.innerText.slice(0, 1200));
    log('--- 抽屉/页面文本(前1200字) ---');
    log(bodyText);
  } else {
    await page.screenshot({ path: 'D:/Architecture/_verify/shot_analysis_nodrawer.png', fullPage: false });
    log('未点击, 截图: _verify/shot_analysis_nodrawer.png');
  }

  log('CONSOLE_ERRORS=' + JSON.stringify(errors.slice(0, 10)));
  await browser.close();
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
