"""pipeline/notifiers — 哨响AI 外部通知推送模块。

通道 (配哪个推哪个):
  - WxNotifier:   微信推送 (★推荐) → Server酱/PushPlus/企业微信webhook, 填 WX_PUSH_URL
  - WecomNotifier: 企业微信群机器人 → 填 WECOM_WEBHOOK_URL (PC端创建)
  - TelegramNotifier: Telegram Bot → 填 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS
"""
from pipeline.notifiers.telegram_notifier import TelegramNotifier, get_notifier, intents_to_signals
from pipeline.notifiers.wecom_notifier import WecomNotifier, get_wecom_notifier
from pipeline.notifiers.wx_notifier import WxNotifier, get_wx_notifier

__all__ = [
    "WxNotifier",
    "get_wx_notifier",
    "WecomNotifier",
    "get_wecom_notifier",
    "TelegramNotifier",
    "get_notifier",
    "intents_to_signals",
]
