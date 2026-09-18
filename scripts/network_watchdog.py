#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""网络断连记录仪 (2026-09-15, 用户: 每天早上网络会断开几秒).

每 5s 双目标 ping (223.5.5.5 AliDNS / 119.29.29.29 DNSPod), 双失 = 候选断,
连续 2 拍双失 = 确认断连; 任一恢复 = 断连结束。
只记录状态跃迁 (DOWN/UP + 持续时长) 与每日汇总, 正常不落日志 (零刷屏)。

自守护: 锁文件防多实例; schtasks 每日 00:00 重拉 (活着则秒退)。
日志: logs/network_watchdog.log (>5MB 自动砍半); 状态: logs/network_watchdog_state.json
"""
import json
import os
import subprocess
import sys
import time

LOG = r'D:\Architecture\logs\network_watchdog.log'
STATE = r'D:\Architecture\logs\network_watchdog_state.json'
LOCK = r'D:\Architecture\logs\network_watchdog.lock'
TARGETS = ['223.5.5.5', '119.29.29.29']
INTERVAL = 5          # 秒/拍
CONFIRM = 2           # 连续 N 拍双失才确认断连 (滤单包丢失)
ROTATE_BYTES = 5 * 1024 * 1024


def _alive_pid(pid):
    try:
        out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'], capture_output=True,
                             text=True, creationflags=0x08000000).stdout
        return str(pid) in out
    except Exception:
        return False


def acquire_lock():
    if os.path.exists(LOCK):
        try:
            pid = int(open(LOCK, encoding='utf-8').read().strip())
            if _alive_pid(pid):
                print(f'已有实例运行 (pid={pid}), 退出')
                sys.exit(0)
        except Exception:
            pass
    with open(LOCK, 'w', encoding='utf-8') as f:
        f.write(str(os.getpid()))


def ping(ip):
    try:
        r = subprocess.run(['ping', '-n', '1', '-w', '1500', ip], capture_output=True,
                           text=True, creationflags=0x08000000, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def log(line):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > ROTATE_BYTES:
            with open(LOG, 'r', encoding='utf-8', errors='replace') as f:
                keep = f.readlines()[-2000:]
            with open(LOG, 'w', encoding='utf-8') as f:
                f.writelines(keep)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def save_state(d):
    try:
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def main():
    acquire_lock()
    log(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] watch start (pid={os.getpid()}, interval={INTERVAL}s)')
    misses = 0
    down_since = None
    day = time.strftime('%Y-%m-%d')
    day_stats = {'dips': 0, 'total_s': 0.0, 'max_s': 0.0}
    while True:
        now = time.time()
        # 新的一天: 写昨日汇总
        today = time.strftime('%Y-%m-%d')
        if today != day:
            log(f'[{day} 汇总] 断连 {day_stats["dips"]} 次, 累计 {day_stats["total_s"]:.0f}s, 最长 {day_stats["max_s"]:.0f}s')
            day = today
            day_stats = {'dips': 0, 'total_s': 0.0, 'max_s': 0.0}
        oks = [ping(ip) for ip in TARGETS]
        if any(oks):
            if down_since is not None:
                dur = now - down_since
                day_stats['dips'] += 1
                day_stats['total_s'] += dur
                day_stats['max_s'] = max(day_stats['max_s'], dur)
                log(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] UP   (断 {dur:.0f}s, 自 {time.strftime("%H:%M:%S", time.localtime(down_since))})')
                down_since = None
            misses = 0
        else:
            misses += 1
            if misses >= CONFIRM and down_since is None:
                down_since = now - INTERVAL * (misses - 1)
                log(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] DOWN (网络断开)')
        save_state({'ts': now, 'down': down_since is not None,
                    'down_since': down_since, 'today': day, **day_stats})
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
