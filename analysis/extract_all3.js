(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const byText = t => [...document.querySelectorAll('*')].find(e => e.textContent.trim() === t && e.children.length === 0);
  const collect = () => Array.from(document.querySelectorAll('table tbody tr')).map(r => Array.from(r.querySelectorAll('td')).map(td => td.innerText.replace(/\n+/g, ' ').trim()));
  byText('体育')?.click(); await sleep(300);
  byText('已结算注单')?.click(); await sleep(1000);
  byText('30天内')?.click(); await sleep(1200);
  const all = [];
  const jumpInput = () => [...document.querySelectorAll('input')].find(i => /^\d+$/.test(i.value) && i.value !== '50' && !/2026/.test(i.value));
  const jumpBtn = () => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '跳转');
  for (let p = 1; p <= 7; p++) {
    if (p > 1) {
      const ji = jumpInput();
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(ji, String(p));
      ji.dispatchEvent(new Event('input', { bubbles: true }));
      jumpBtn()?.click();
      await sleep(1100);
    }
    const rows = collect();
    if (rows.length === 0) break;
    all.push(...rows);
  }
  const t = document.body.innerText;
  return JSON.stringify({
    rows: all,
    total: (t.match(/总投注单数：\s*(\d+)/) || [])[1],
    amt: (t.match(/总投注额：\s*([\d.,]+)/) || [])[1],
    win: (t.match(/总输赢：\s*([-\d.,]+)/) || [])[1]
  });
})()
