# -*- coding: utf-8 -*-
"""pipeline/notifiers/wx_notifier.py — 哨响AI 交易信号 → 微信推送 (Server酱 / PushPlus)

铁律
----
- 非阻塞: HTTP 发送走线程池, 绝不阻塞主分析流程。
- 去重: 同一 match_id + direction 在 DEDUP_WINDOW 秒内不重复推送。
- 容错: 任何异常均捕获 + 日志, 不崩主流程。

Server酱 配置 (2分钟):
  1. 浏览器打开 https://sct.ftqq.com/
  2. 微信扫码登录 → 点击「SendKey」→ 复制
  3. 填入 .env: WX_PUSH_URL=https://sctapi.ftqq.com/YOUR_SENDKEY.send

PushPlus 配置 (替代):
  1. 打开 https://www.pushplus.plus/ → 微信扫码 → 复制 Token
  2. 填入 .env: WX_PUSH_URL=https://www.pushplus.plus/send/YOUR_TOKEN
"""
from __future__ import annotations

import json as _json_mod
import logging
import os
import time
import urllib.request as _req
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("wx_notifier")

DEDUP_WINDOW: float = 300.0  # 5 min

# ── 手动加载 .env (无 dotenv 依赖) ──
def _load_env() -> None:
    """从项目根 .env 文件加载环境变量 (纯标准库, 无 dotenv 依赖)。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env_path = os.path.join(root, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val

_load_env()


def _env_list(key: str, default: str = "") -> List[str]:
    raw = os.getenv(key, default)
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class _DedupEntry:
    timestamp: float = 0.0


class WxNotifier:
    """微信推送器 (Server酱 / PushPlus / 企业微信webhook 通用)。

    .env 配置:
      WX_PUSH_URL=https://sctapi.ftqq.com/YOUR_SENDKEY.send
      或
      WX_PUSH_URL=https://www.pushplus.plus/send/YOUR_TOKEN
      或 (企业微信群机器人 PC端创建)
      WX_PUSH_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
    """

    def __init__(self, webhook_urls: Optional[List[str]] = None):
        self._urls = webhook_urls if webhook_urls else _env_list("WX_PUSH_URL")
        self._dedup: Dict[str, _DedupEntry] = {}
        self._enabled = bool(self._urls)
        if not self._enabled:
            logger.info("WxNotifier 未配置 (WX_PUSH_URL 缺失), 静默禁用")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _is_duplicate(self, signal: Dict[str, Any]) -> bool:
        mid = signal.get("mid", "")
        direction = signal.get("selection") or signal.get("best_direction", "")
        if not mid or not direction:
            return False
        key = f"{mid}|{direction}"
        now = time.time()
        entry = self._dedup.get(key)
        if entry and (now - entry.timestamp) < DEDUP_WINDOW:
            return True
        self._dedup[key] = _DedupEntry(timestamp=now)
        if len(self._dedup) > 1000:
            cutoff = now - 600.0
            self._dedup = {k: v for k, v in self._dedup.items() if v.timestamp >= cutoff}
        return False

    @staticmethod
    def format_signal(signal: Dict[str, Any]) -> str:
        league = signal.get("league") or signal.get("competition") or "?"
        home = signal.get("home", "?")
        away = signal.get("away", "?")
        direction = signal.get("selection") or signal.get("best_direction") or "?"
        odds = signal.get("odds", 0.0)
        stake = signal.get("stake", 0.0)
        edge = signal.get("edge_pct", 0.0)
        ev = signal.get("ev_pct", 0.0)
        ts = signal.get("timestamp") or datetime.now(timezone.utc).strftime("%H:%M")

        return (
            f"## 🚨 哨响AI · 交易信号\n\n"
            f"> **{league}** · {home} vs {away}\n\n"
            f"- 方向: **{direction}** | 赔率: **{odds:.2f}**\n"
            f"- 注码: ¥{stake:,.0f} | Edge: {edge:+.1f}% | EV: {ev:+.1f}%\n\n"
            f"⏰ {ts}"
        )

    def _send(self, url: str, content: str) -> bool:
        """用标准库 urllib 发送 (零外部依赖, form-encoded)。"""
        import urllib.parse as _parse

        is_wecom = "qyapi.weixin.qq.com" in url
        is_pushplus = "pushplus.plus" in url

        if is_wecom:
            payload = {"msgtype": "markdown", "markdown": {"content": content}}
            data = _json_mod.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        elif is_pushplus:
            payload = {"title": "哨响AI 交易信号", "content": content, "template": "html"}
            data = _parse.urlencode(payload).encode("utf-8")
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        else:
            # Server酱 / 通用 webhook: form-encoded
            payload = {"title": "哨响AI 交易信号", "desp": content}
            data = _parse.urlencode(payload).encode("utf-8")
            headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            req = _req.Request(url, data=data, headers=headers, method="POST")
            with _req.urlopen(req, timeout=15) as resp:
                body_raw = resp.read().decode("utf-8", errors="replace")
                if resp.status == 200:
                    # PushPlus 返回 code=200 才算成功
                    if is_pushplus:
                        body = _json_mod.loads(body_raw)
                        if body.get("code") == 200:
                            return True
                        logger.warning("PushPlus 返回: %s", body.get("msg", body_raw[:200]))
                        return False
                    return True
                logger.warning("推送 HTTP %s: %s", resp.status, body_raw[:200])
                return False
        except Exception as e:
            logger.warning("推送异常 (url=%s...): %s", url[:50], e)
            return False

    def _send_sync_threadsafe(self, content: str) -> None:
        """在线程中同步发送 (fire-and-forget)。"""
        import threading

        def _run():
            for url in self._urls:
                self._send(url, content)

        t = threading.Thread(target=_run, daemon=True, name="wx-push")
        t.start()

    def send_signal(self, signal: Dict[str, Any]) -> bool:
        """同步发送单个信号 (带去重), 在调用线程中阻塞。"""
        if not self._enabled or self._is_duplicate(signal):
            return False
        content = self.format_signal(signal)
        return any(self._send(u, content) for u in self._urls)

    def send_signal_async(self, signal: Dict[str, Any]) -> bool:
        """fire-and-forget 版本: 新线程发送, 不阻塞调用者。"""
        if not self._enabled or self._is_duplicate(signal):
            return False
        self._send_sync_threadsafe(self.format_signal(signal))
        logger.info("微信推送已调度: %s vs %s", signal.get("home"), signal.get("away"))
        return True

    def notify_signals(self, signals: List[Dict[str, Any]], async_mode: bool = True) -> int:
        """批量推送。async_mode=True 时 fire-and-forget。"""
        if not self._enabled or not signals:
            return 0
        sent = 0
        for s in signals:
            ok = self.send_signal_async(s) if async_mode else self.send_signal(s)
            if ok:
                sent += 1
        return sent


# ── 单例 ──
_NOTIFIER: Optional[WxNotifier] = None

def get_wx_notifier() -> WxNotifier:
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = WxNotifier()
    return _NOTIFIER


__all__ = ["WxNotifier", "get_wx_notifier", "DEDUP_WINDOW"]
