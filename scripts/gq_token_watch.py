#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gq_token_watch.py — 乐鱼(GQ) token 过期检测 + Windows 系统通知
============================================================
用户需求 (2026-09-01): "检测到 token 过期, 直接发出电脑系统通知"

检测信号 (任一命中即告警, 避免静默断采):
  S1: gq/ws_daemon.log 近 WINDOW_H 内出现 "[ALERT] 乐鱼 token(requestid) 疑似失效"
      (auto_collector._flag_token_suspect 打印, 真实鉴权失败码 0401013)
  S2: gq/ws_daemon.log 近 WINDOW_H 内出现 "[ALERT] 连续" 且含 "token 失效"
      (_note_empty_list 空列表告警: code 正常但连续 8 轮空, 疑似 token 死)
  S3: data/events.db odds_snapshots max(captured_at) 距今 > STALE_MIN 分钟
      (WS 正常时每秒写快照 + 早盘补采; 长时间无写入 = 采集停摆, 大概率 token/页面问题)

通知: Windows toast (PowerShell Windows.UI.Notifications, 零第三方依赖)
去重: gq/.token_watch_state.json 记录上次告警时间, 同类型 30 分钟内不重复弹
用法:
  python gq_token_watch.py           # 单次检查 (自动化/手动)
  python gq_token_watch.py --force   # 忽略去重强制通知 (验证 toast 用)
  python gq_token_watch.py --check   # 只检测打印, 不弹通知 (dry-run)
退出码: 0=健康  1=检测到告警(已通知)  2=内部错误
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

ROOT = r"D:\Architecture"
LOG = os.path.join(ROOT, "gq", "ws_daemon.log")
EVENTS_DB = os.path.join(ROOT, "data", "events.db")
STATE = os.path.join(ROOT, "gq", ".token_watch_state.json")
APP_ID = "哨响AI GQ采集监控"

WINDOW_H = 2          # 日志窗口: 只看最近 2 小时的 [ALERT]
STALE_MIN = 15        # events.db 无写入超过 15 分钟 = 断采
DEDUP_MIN = 30        # 同类型告警 30 分钟内不重复弹
EMPTY_ALERT_KW = "token 失效"   # S2 空列表告警文案关键词


def _load_state() -> dict:
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st: dict):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        print(f"[warn] 状态写入失败: {e}")


def _tail(path: str, n=400) -> list:
    """读文件末尾 n 行 (大文件不整读)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
        return [ln.rstrip("\n") for ln in lines]
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[warn] 日志读取失败 {path}: {e}")
        return []


def _scan_log_alerts() -> list:
    """S1/S2: 在日志窗口内找 token 相关 [ALERT] 行."""
    hits = []
    now = time.time()
    cutoff = now - WINDOW_H * 3600
    for ln in _tail(LOG):
        if "[ALERT]" not in ln:
            continue
        # 行首形如 [2026-09-01 04:06:12] [ALERT] ...
        ts = None
        try:
            ts = datetime.strptime(ln[1:20], "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            continue
        if ts < cutoff:
            continue
        if "token(requestid) 疑似失效" in ln or ("[ALERT] 连续" in ln and EMPTY_ALERT_KW in ln):
            hits.append(ln)
    return hits


def _scan_stale_db() -> bool:
    """S3: events.db 断采检测."""
    try:
        import sqlite3
        c = sqlite3.connect(EVENTS_DB, timeout=5)
        ts = c.execute("select max(captured_at) from odds_snapshots").fetchone()[0]
        c.close()
        if not ts:
            return False
        age_min = (time.time() - ts) / 60.0
        return age_min > STALE_MIN
    except Exception as e:
        print(f"[warn] events.db 检查失败: {e}")
        return False


def send_toast(title: str, message: str) -> bool:
    """Windows toast 系统通知 (PowerShell Windows.UI.Notifications, 零依赖)."""
    # 转义: 去掉可能破坏 PowerShell 字符串的引号
    t = title.replace('"', "'").replace("`", "'")
    m = message.replace('"', "'").replace("`", "'")
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument,"
        " ContentType=WindowsRuntime] | Out-Null;"
        f"$tpl=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$txt=$tpl.GetElementsByTagName('text');"
        f"$txt.Item(0).AppendChild($tpl.CreateTextNode('{t}')) | Out-Null;"
        f"$txt.Item(1).AppendChild($tpl.CreateTextNode('{m}')) | Out-Null;"
        f"$toast=[Windows.UI.Notifications.ToastNotification]::new($tpl);"
        f"$nf=[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}');"
        "$nf.Show($toast)"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=25,
        )
        return r.returncode == 0
    except Exception as e:
        print(f"[warn] toast 发送失败: {e}")
        return False


def main():
    force = "--force" in sys.argv
    dry = "--check" in sys.argv

    alerts = _scan_log_alerts()
    stale = _scan_stale_db()

    # 汇总告警类型
    kinds = []
    detail = []
    if alerts:
        kinds.append("log_alert")
        detail.append("S1/S2: 日志出现 token 失效 ALERT: " + alerts[-1].strip())
    if stale:
        kinds.append("db_stale")
        detail.append(f"S3: events.db 超过 {STALE_MIN} 分钟无快照写入 (疑似断采/token 过期)")

    if not kinds:
        print(f"[ok] {datetime.now():%H:%M:%S} GQ 采集健康: 日志无 token ALERT, 快照写入正常")
        return 0

    state = _load_state()
    now = time.time()
    ts_key = "notify_ts"
    last_ts = state.get(ts_key, 0) or 0
    need_notify = force or (now - last_ts > DEDUP_MIN * 60)

    print(f"[!!] 检测到告警: {kinds}")
    for d in detail:
        print("     " + d)

    if dry:
        print(f"[dry-run] 不弹通知 (need_notify={need_notify})")
        return 1

    if not need_notify:
        print(f"[dedup] 距上次通知不足 {DEDUP_MIN} 分钟, 跳过")
        return 1

    title = "⚠ 乐鱼 GQ token 疑似过期"
    message = " | ".join(
        d.split(": ", 1)[1] if ": " in d else d for d in detail
    )[:200]
    ok = send_toast(title, message)
    if ok:
        state[ts_key] = now
        state["last_kinds"] = kinds
        _save_state(state)
        print(f"[toast] 系统通知已发送: {title}")
    else:
        print("[toast] 发送失败 (可查看 PowerShell 执行策略)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
