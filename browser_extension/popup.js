// popup.js — 哨响AI 插件弹窗逻辑

const dot = document.getElementById('dot');
const statusText = document.getElementById('statusText');
const scanBtn = document.getElementById('scanBtn');
const retryBtn = document.getElementById('retryBtn');
const lastMatchEl = document.getElementById('lastMatch');
const lastMatchText = document.getElementById('lastMatchText');
const decisionBox = document.getElementById('decisionBox');

// 检查连接状态
function checkStatus() {
  chrome.runtime.sendMessage({ type: 'status' }, (resp) => {
    if (resp && resp.connected) {
      dot.className = 'dot on';
      statusText.textContent = '已连接 bridge_service';
    } else {
      dot.className = 'dot off';
      statusText.textContent = '未连接';
    }
  });
}

// 显示上次决策
function loadLastDecision() {
  chrome.storage.local.get('lastDecision', (result) => {
    const d = result.lastDecision;
    if (d && d.match) {
      lastMatchEl.style.display = 'block';
      lastMatchText.textContent = `${d.match} | ${d.books || 0}庄 | 方向:${d.direction || '?'} | ${d.decision || 'PASS'}`;
      decisionBox.style.display = 'block';
      decisionBox.className = 'decision ' + ((d.decision === 'BET') ? 'bet' : 'pass');
      decisionBox.textContent = d.decision === 'BET' ? '✅ 建仓信号' : (d.decision_text || 'PASS · 观望');
    }
  });
}

// 扫描当前页面
scanBtn.addEventListener('click', () => {
  scanBtn.disabled = true;
  scanBtn.textContent = '扫描中...';
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) {
      chrome.tabs.sendMessage(tabs[0].id, { type: 'shouxiang_scan' }, (resp) => {
        // content script 可能未注入 (不在匹配域名)
        if (chrome.runtime.lastError) {
          statusText.textContent = '当前页面不支持 (不在匹配域名列表)';
        }
        scanBtn.disabled = false;
        scanBtn.textContent = '立即扫描当前页';
        setTimeout(loadLastDecision, 1500);
      });
    } else {
      scanBtn.disabled = false;
      scanBtn.textContent = '立即扫描当前页';
    }
  });
});

// 重新连接
retryBtn.addEventListener('click', () => {
  statusText.textContent = '重连中...';
  dot.className = 'dot off';
  chrome.runtime.sendMessage({ type: 'status' });
  setTimeout(checkStatus, 1000);
});

// 初始化
checkStatus();
loadLastDecision();

// 定期刷新
setInterval(checkStatus, 5000);
