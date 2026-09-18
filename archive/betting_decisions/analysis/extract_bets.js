(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const byText = t => [...document.querySelectorAll('*')].find(e => e.textContent.trim() === t && e.children.length === 0);
  const nextBtn = () => [...document.querySelectorAll('button')].find(b => /Next page/i.test(b.textContent) && !b.disabled);
  // click 30天内 to set range
  const f = byText('30天内');
  if (f) f.click();
  await sleep(1000);
  const all = [];
  for (let p = 0; p < 12; p++) {
    const rows = Array.from(document.querySelectorAll('table tbody tr'));
    rows.forEach(r => all.push(Array.from(r.querySelectorAll('td')).map(td => td.innerText.replace(/\n+/g, ' ').trim())));
    const n = nextBtn();
    if (!n) break;
    n.click();
    await sleep(900);
  }
  const total = (document.body.innerText.match(/总投注单数：\s*(\d+)/) || [])[1];
  const amt = (document.body.innerText.match(/总投注额：\s*([\d.,]+)/) || [])[1];
  const win = (document.body.innerText.match(/总输赢：\s*([-\d.,]+)/) || [])[1];
  return JSON.stringify({ pages: all.length, total, amt, win, rows: all });
})()
