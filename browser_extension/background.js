/**
 * 哨响AI — Chrome MV3 Service Worker
 * 管理 WebSocket 连接, 接收 content script 推送的赔率数据, 转发到 bridge_service
 */
const BRIDGE_WS = 'ws://localhost:9000/ws/odds_ingest';
let ws = null;
let reconnectTimer = null;
let reconnectDelay = 1000;

function connect() {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  
  try {
    ws = new WebSocket(BRIDGE_WS);
    
    ws.onopen = () => {
      console.log('[哨响AI] WebSocket 已连接 ->', BRIDGE_WS);
      reconnectDelay = 1000;
      updateBadge('ON', '#22c55e');
    };
    
    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        console.log('[哨响AI] 收到回复:', msg);
        // 存储最新决策
        if (msg.status === 'analyzed') {
          chrome.storage.local.set({ lastDecision: msg });
        }
      } catch (e) {
        console.warn('[哨响AI] 消息解析失败:', e);
      }
    };
    
    ws.onclose = () => {
      console.log('[哨响AI] WebSocket 断开, 5s后重连');
      updateBadge('OFF', '#ef4444');
      scheduleReconnect();
    };
    
    ws.onerror = (err) => {
      console.warn('[哨响AI] WebSocket 错误:', err);
      updateBadge('ERR', '#f59e0b');
    };
  } catch (e) {
    console.error('[哨响AI] WebSocket 连接失败:', e);
    scheduleReconnect();
  }
}

function scheduleReconnect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    connect();
  }, reconnectDelay);
}

function updateBadge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
}

// 接收 content script 消息并转发
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'odds_data') {
    const data = msg.data;
    if (!data || !data.home || !data.away) {
      sendResponse({ status: 'error', detail: 'invalid data' });
      return true;
    }
    
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data));
      sendResponse({ status: 'sent', match: `${data.home} vs ${data.away}` });
    } else {
      connect();
      sendResponse({ status: 'error', detail: 'websocket not connected, reconnecting...' });
    }
    return true;
  }
  
  if (msg.type === 'status') {
    sendResponse({
      connected: ws ? ws.readyState === WebSocket.OPEN : false,
      wsUrl: BRIDGE_WS,
    });
    return true;
  }
});

// 启动时连接
connect();

// 心跳保持 (每30s)
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping' }));
  }
}, 30000);
