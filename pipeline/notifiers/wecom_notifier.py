# -*- coding: utf-8 -*-
"""pipeline/notifiers/wecom_notifier.py — 哨响AI 交易信号 → 企业微信群机器人推送

铁律
----
- 非阻塞: 所有 HTTP 发送走线程池, 绝不阻塞主分析流程。
- 去重: 同一 match_id + direction 在 DEDUP_WINDOW 秒内不重复推送。
- 容错: 企业微信 API 任何异常均捕获 + 日志, 不向上抛出 (不崩主流程)。
- 环境变量: WECOM_WEBHOOK_URL 从 .env 读取。

企业微信群机器人配置 (3 步):
  1. 企业微信 → 群聊 → 右键「群管理」→「添加群机器人」
  2. 复制 Webhook 地址 (形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx)
  3. 填入 .env 的 WECOM_WEBHOOK_URL

支持多群推送: 逗号分隔多个 webhook URL。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("wecom_notifier")

# ── 去重窗口 (秒) ──
DEDUP_WINDOW: float = 300.0  # 5 分钟

# ── 手动加载 .env (无 dotenv 依赖) ──
def _load_env() -> None:
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
    """逗号分隔环境变量 → 去空.strip 列表。"""
    raw = os.getenv(key, default)
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class _DedupEntry:
    timestamp: float = 0.0


class WecomNotifier:
    """企业微信群机器人信号推送器。

    Usage::

        notifier = WecomNotifier()
        await notifier.send_signal({...})
        await notifier.notify_signals([{...}, {...}])
    """

    def __init__(
        self,
        webhook_urls: Optional[List[str]] = None,
    ) -> None:
        self._webhook_urls: List[str] = (
            webhook_urls
            if webhook_urls is not None
            else _env_list("WECOM_WEBHOOK_URL")
        )
        self._dedup: Dict[str, _DedupEntry] = {}
        self._enabled = bool(self._webhook_urls)
        if not self._enabled:
            logger.info(
                "WecomNotifier 未配置 (WECOM_WEBHOOK_URL 缺失), 静默禁用推送。"
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── 去重 ─────────────────────────────────────────────────────

    def _dedup_key(self, signal: Dict[str, Any]) -> Optional[str]:
        mid = signal.get("mid", "")
        direction = signal.get("selection") or signal.get("best_direction", "")
        if not mid or not direction:
            return None
        return f"{mid}|{direction}"

    def _is_duplicate(self, signal: Dict[str, Any]) -> bool:
        key = self._dedup_key(signal)
        if not key:
            return False
        now = time.time()
        entry = self._dedup.get(key)
        if entry is not None and (now - entry.timestamp) < DEDUP_WINDOW:
            return True
        self._dedup[key] = _DedupEntry(timestamp=now)
        self._maybe_cleanup(now)
        return False

    def _maybe_cleanup(self, now: float) -> None:
        if len(self._dedup) > 1000:
            cutoff = now - 600.0
            expired = [k for k, v in self._dedup.items() if v.timestamp < cutoff]
            for k in expired:
                del self._dedup[k]

    # ── 消息格式化 ───────────────────────────────────────────────

    @staticmethod
    def format_signal(signal: Dict[str, Any]) -> str:
        """将 signal dict 格式化为企业微信 Markdown 推送文本。

        企业微信机器人支持 markdown 类型消息。
        """
        league = signal.get("league", signal.get("competition", "?"))
        home = signal.get("home", "?")
        away = signal.get("away", "?")
        direction = signal.get("selection") or signal.get("best_direction", "?")
        odds = signal.get("odds", 0.0)
        stake = signal.get("stake", 0.0)
        edge = signal.get("edge_pct", 0.0)
        ev = signal.get("ev_pct", 0.0)
        decision = signal.get("decision", "BET")
        ts = signal.get("timestamp") or datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        gate_label = (
            decision.upper()
            if decision == "BET"
            else signal.get("note", decision)
        )

        edge_color = "info" if edge > 0 else "warning"
        lines = [
            "## 🚨 哨响AI · 交易信号",
            f"> **{league}** · {home} vs {away}",
            "",
            f"**方向**: <font color=\"info\">{direction}</font>  |  赔率: **{odds:.2f}**",
            f"**注码**: ¥{stake:,.0f}  |  Edge: <font color=\"{edge_color}\">{edge:+.1f}%</font>  |  EV: {ev:+.1f}%",
            f"**信心**: [{gate_label}]",
            "",
            f"⏰ {ts}",
        ]
        return "\n".join(lines)

    # ── 发送核心 ─────────────────────────────────────────────────

    async def _send_to_webhook(self, webhook_url: str, content: str) -> bool:
        """向单个 webhook 发送 markdown 消息。"""
        import aiohttp

        payload = {
            "msgtype": "markdown",
            "markdown": {"content": content},
        }
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(webhook_url, json=payload) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        if body.get("errcode") == 0:
                            return True
                        logger.warning(
                            "企业微信 webhook 返回错误: %s (url=%s...)",
                            body,
                            webhook_url[:60],
                        )
                        return False
                    body = await resp.text()
                    logger.warning(
                        "企业微信 webhook HTTP %s (url=%s...): %s",
                        resp.status,
                        webhook_url[:60],
                        body[:200],
                    )
                    return False
        except aiohttp.ClientError as e:
            logger.warning(
                "企业微信网络异常 (url=%s...): %s", webhook_url[:60], e
            )
            return False
        except Exception as e:
            logger.error(
                "企业微信发送未知异常 (url=%s...): %s",
                webhook_url[:60],
                e,
                exc_info=True,
            )
            return False

    def _send_sync_threadsafe(self, content: str) -> None:
        """同步发送 (在线程中跑 event loop, 供非 async 上下文调用)。"""
        import threading

        async def _send_all() -> None:
            for url in self._webhook_urls:
                await self._send_to_webhook(url, content)

        def _run():
            try:
                asyncio.run(_send_all())
            except Exception as e:
                logger.error("企业微信同步线程异常: %s", e)

        t = threading.Thread(target=_run, daemon=True, name="wecom-notify")
        t.start()

    # ── 公共方法 ─────────────────────────────────────────────────

    async def send_signal(self, signal: Dict[str, Any]) -> bool:
        """异步发送单个信号 (带去重)。"""
        if not self._enabled:
            return False
        if self._is_duplicate(signal):
            logger.debug(
                "信号已去重: mid=%s direction=%s",
                signal.get("mid"),
                signal.get("selection"),
            )
            return False
        content = self.format_signal(signal)
        results = await asyncio.gather(
            *[self._send_to_webhook(url, content) for url in self._webhook_urls],
            return_exceptions=True,
        )
        success = any(r is True for r in results)
        if success:
            logger.info(
                "企业微信推送成功: %s vs %s",
                signal.get("home"),
                signal.get("away"),
            )
        return success

    def send_signal_sync(self, signal: Dict[str, Any]) -> bool:
        """同步 fire-and-forget 版本 (供 autopilot 等非 async 上下文)。"""
        if not self._enabled:
            return False
        if self._is_duplicate(signal):
            return False
        content = self.format_signal(signal)
        self._send_sync_threadsafe(content)
        logger.info(
            "企业微信推送已调度: %s vs %s",
            signal.get("home"),
            signal.get("away"),
        )
        return True

    async def notify_signals(self, signals: List[Dict[str, Any]]) -> int:
        """批量推送 (带去重)。返回实际推送数。"""
        if not self._enabled or not signals:
            return 0
        sent = 0
        for sig in signals:
            ok = await self.send_signal(sig)
            if ok:
                sent += 1
        return sent

    def notify_signals_sync(self, signals: List[Dict[str, Any]]) -> int:
        """同步批量 fire-and-forget。"""
        if not self._enabled or not signals:
            return 0
        sent = 0
        for sig in signals:
            ok = self.send_signal_sync(sig)
            if ok:
                sent += 1
        return sent


# ── 进程级单例 ────────────────────────────────────────────────────

_NOTIFIER: Optional[WecomNotifier] = None


def get_wecom_notifier() -> WecomNotifier:
    """获取 WecomNotifier 进程级单例 (惰性初始化)。"""
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = WecomNotifier()
    return _NOTIFIER


__all__ = [
    "WecomNotifier",
    "get_wecom_notifier",
    "DEDUP_WINDOW",
]
