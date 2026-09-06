# -*- coding: utf-8 -*-
"""pipeline/notifiers/telegram_notifier.py — 哨响AI 交易信号 → Telegram Bot 推送

铁律
----
- 非阻塞: 所有 HTTP 发送走异步 (aiohttp) 或线程池, 绝不阻塞主分析流程。
- 去重: 同一 match_id + direction 在 DEDUP_WINDOW 秒内不重复推送。
- 容错: Telegram API 任何异常均捕获 + 日志, 不向上抛出 (不崩主流程)。
- 环境变量: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS 从 .env 读取。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("telegram_notifier")

# ── 去重窗口 (秒) ──
DEDUP_WINDOW: float = 300.0  # 5 分钟

# ── 默认 Telegram API 基础 URL ──
TELEGRAM_API_BASE: str = "https://api.telegram.org"

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
    """去重记忆条目: 记录上次推送时间戳。"""
    timestamp: float = 0.0


class TelegramNotifier:
    """Telegram Bot 信号推送器。

    Usage::

        notifier = TelegramNotifier()
        await notifier.send_signal({...})
        await notifier.notify_signals([{...}, {...}])
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_ids: Optional[List[str]] = None,
    ) -> None:
        self._bot_token = bot_token if bot_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_ids: List[str] = chat_ids if chat_ids is not None else _env_list("TELEGRAM_CHAT_IDS")
        self._api_base = os.getenv("TELEGRAM_API_BASE", TELEGRAM_API_BASE)
        # 去重缓存: key = "mid|direction", value = _DedupEntry
        self._dedup: Dict[str, _DedupEntry] = {}
        self._enabled = bool(self._bot_token and self._chat_ids)
        if not self._enabled:
            logger.info("TelegramNotifier 未配置 (TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_IDS 缺失), 静默禁用推送。")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── 去重 ─────────────────────────────────────────────────────

    def _dedup_key(self, signal: Dict[str, Any]) -> Optional[str]:
        """构造去重键: mid + direction。若缺少字段返回 None (不过滤)。"""
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
        # 惰性清理过期的 key (每次清理耗时 O(n)，但 n 通常 << 1000)
        self._maybe_cleanup(now)
        return False

    def _maybe_cleanup(self, now: float) -> None:
        """惰性清理过期去重条目 (保留最近 10 分钟)。"""
        if len(self._dedup) > 1000:
            cutoff = now - 600.0
            expired = [k for k, v in self._dedup.items() if v.timestamp < cutoff]
            for k in expired:
                del self._dedup[k]

    # ── 消息格式化 ───────────────────────────────────────────────

    @staticmethod
    def format_signal(signal: Dict[str, Any]) -> str:
        """将 signal dict 格式化为 Telegram 推送文本。

        期望字段:
            league, home, away, selection, odds, stake,
            edge_pct, ev_pct, decision, timestamp
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
        ts = signal.get("timestamp") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # D-Gate / 信心标签
        gate_label = decision.upper() if decision == "BET" else signal.get("note", decision)

        lines = [
            "🚨 哨响AI · 交易信号",
            f"📺 [{league}] {home} vs {away}",
            f"🎯 [方向: {direction}] · 赔率 {odds:.2f}",
            f"💰 建议注码: ¥{stake:,.0f} | Edge: {edge:+.1f}% | EV: {ev:+.1f}%",
            f"📊 信心: [{gate_label}]",
            f"⏰ {ts}",
        ]
        return "\n".join(lines)

    # ── 异步发送核心 ─────────────────────────────────────────────

    async def _send_to_chat(self, chat_id: str, text: str) -> bool:
        """向单个 chat_id 发送消息。返回是否成功。"""
        import aiohttp

        url = f"{self._api_base}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return True
                    body = await resp.text()
                    logger.warning(
                        "Telegram API 返回 %s (chat_id=%s): %s",
                        resp.status, chat_id, body[:200],
                    )
                    return False
        except aiohttp.ClientError as e:
            logger.warning("Telegram 网络异常 (chat_id=%s): %s", chat_id, e)
            return False
        except Exception as e:
            logger.error("Telegram 发送未知异常 (chat_id=%s): %s", chat_id, e, exc_info=True)
            return False

    def _send_sync_threadsafe(self, text: str) -> None:
        """同步发送 (在独立线程中跑 event loop, 供非 async 上下文调用)。"""
        import threading

        async def _send_all() -> None:
            for cid in self._chat_ids:
                await self._send_to_chat(cid, text)

        def _run():
            try:
                asyncio.run(_send_all())
            except Exception as e:
                logger.error("Telegram 同步线程异常: %s", e)

        t = threading.Thread(target=_run, daemon=True, name="tg-notify")
        t.start()

    # ── 公共方法 ─────────────────────────────────────────────────

    async def send_signal(self, signal: Dict[str, Any]) -> bool:
        """异步发送单个信号 (带去重)。

        Args:
            signal: 信号字典, 需含 mid/selection 用于去重, 其余字段用于格式化。

        Returns:
            是否实际发送 (被去重拦截返回 False)。
        """
        if not self._enabled:
            return False
        if self._is_duplicate(signal):
            logger.debug("信号已去重: mid=%s direction=%s",
                         signal.get("mid"), signal.get("selection"))
            return False
        text = self.format_signal(signal)
        results = await asyncio.gather(
            *[self._send_to_chat(cid, text) for cid in self._chat_ids],
            return_exceptions=True,
        )
        success = any(r is True for r in results)
        if success:
            logger.info("Telegram 推送成功: %s vs %s", signal.get("home"), signal.get("away"))
        return success

    def send_signal_sync(self, signal: Dict[str, Any]) -> bool:
        """同步版本: 在线程中跑 async, 不阻塞调用线程 (fire-and-forget)。

        适合在非 async 上下文中调用 (如 autopilot 的 run_once)。
        注意: 这是 fire-and-forget, 返回值表示是否被去重拦截 (非发送成功)。
        """
        if not self._enabled:
            return False
        if self._is_duplicate(signal):
            return False
        text = self.format_signal(signal)
        self._send_sync_threadsafe(text)
        logger.info("Telegram 推送已调度 (fire-and-forget): %s vs %s",
                    signal.get("home"), signal.get("away"))
        return True

    async def notify_signals(self, signals: List[Dict[str, Any]]) -> int:
        """批量推送多个信号 (带去重)。

        Returns:
            实际推送的信号数。
        """
        if not self._enabled or not signals:
            return 0
        sent = 0
        for sig in signals:
            ok = await self.send_signal(sig)
            if ok:
                sent += 1
        return sent

    def notify_signals_sync(self, signals: List[Dict[str, Any]]) -> int:
        """同步批量: 每个信号 fire-and-forget。"""
        if not self._enabled or not signals:
            return 0
        sent = 0
        for sig in signals:
            ok = self.send_signal_sync(sig)
            if ok:
                sent += 1
        return sent


# ── 进程级单例 ────────────────────────────────────────────────────

_NOTIFIER: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """获取 TelegramNotifier 进程级单例 (惰性初始化)。"""
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = TelegramNotifier()
    return _NOTIFIER


# ── 便捷函数: 从 BetPlan.intents 提取 signal dicts ─────────────────

def intents_to_signals(intents: List[Any], league: str = "") -> List[Dict[str, Any]]:
    """把 BetPlan.intents (BetIntent 列表) 转为 signal dict 列表。

    这样 TelegramNotifier 不必导入 pipeline.strategy, 保持模块边界清晰。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    signals: List[Dict[str, Any]] = []
    for it in intents:
        signals.append({
            "mid": getattr(it, "mid", ""),
            "home": getattr(it, "home", ""),
            "away": getattr(it, "away", ""),
            "league": league,
            "market": getattr(it, "market", "1X2"),
            "selection": getattr(it, "selection", ""),
            "odds": getattr(it, "odds", 0.0),
            "model_prob": getattr(it, "model_prob", 0.0),
            "edge_pct": getattr(it, "edge_pct", 0.0),
            "ev_pct": getattr(it, "edge_pct", 0.0),  # BetIntent 无 ev_pct, 用 edge_pct 近似
            "stake": getattr(it, "stake", 0.0),
            "decision": "BET",
            "timestamp": now,
        })
    return signals


__all__ = [
    "TelegramNotifier",
    "get_notifier",
    "intents_to_signals",
    "DEDUP_WINDOW",
]
