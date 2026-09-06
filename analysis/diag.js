(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const byText=t=>[...document.querySelectorAll('*')].find(e=>e.textContent.trim()===t&&e.children.length===0);
  byText('体育')?.click(); await sleep(300);
  byText('已结算注单')?.click(); await sleep(1000);
  byText('30天内')?.click(); await sleep(1200);
  const btns=[...document.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(Boolean);
  const navBtns=[...document.querySelectorAll('button')].filter(b=>/page|下一|上一|Next|Prev|跳转/i.test(b.textContent)||/^\d+$/.test(b.textContent.trim()));
  const inputs=[...document.querySelectorAll('input')].map(i=>({v:i.value,ph:i.placeholder}));
  const t=document.body.innerText;
  return JSON.stringify({total:(t.match(/总投注单数：\s*(\d+)/)||[])[1], navBtns:navBtns.map(b=>b.textContent.trim()+' disabled='+b.disabled), inputs, firstRow:(document.querySelector('table tbody tr')?.querySelector('td')?.innerText||'NONE').slice(0,40)});
})()
