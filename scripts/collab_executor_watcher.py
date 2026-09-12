#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""执行 AI 唤醒器 (2026-09-13): 轮询黑板, 新 open 条目 → 唤醒 hermes 执行.

闭环: 前端⚡上报 / 哨响观察 → 黑板 open → 本 watcher 发现 → hermes.exe -z
按 executor_brief.md 认领处理 → 结论写回黑板 → 前端协作面板可见。

用法: python scripts/collab_executor_watcher.py [--interval 120]
建议挂计划任务(每5分钟)或常驻。锁定: 同时仅一个 hermes 实例在工作。
"""
import argparse
import os
import subprocess
import sys
import time

ROOT = r'D:\Architecture'
JOURNAL = os.path.join(ROOT, 'data', 'collab_journal.jsonl')
COLLAB = os.path.join(ROOT, 'sandbox', 'collab', 'collab.py')
PY = os.path.join(ROOT, '.venv', 'Scripts', 'python.exe')
HERMES = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'hermes', 'bin', 'hermes.exe')
LOCK = os.path.join(ROOT, 'data', 'collab_executor.lock')
BRIEF = os.path.join(ROOT, 'sandbox', 'collab', 'executor_brief.md')


def count_open():
    r = subprocess.run([PY, COLLAB, '--journal', JOURNAL, 'listx', '--status', 'open'],
                       capture_output=True, text=True)
    return sum(1 for line in r.stdout.splitlines() if line.strip().startswith('open'))


def wake_hermes(n_open):
    prompt = (
        f"读取 {BRIEF} 并严格按其执行。黑板当前有 {n_open} 条 open 任务, "
        f"逐条认领处理并写回。只处理黑板任务, 不要做其他事。"
    )
    r = subprocess.run([HERMES, '-z', prompt, '--in', ROOT, '--yolo'],
                       capture_output=True, text=True, timeout=1800)
    return r.returncode, (r.stdout or '')[-2000:]


def main(interval):
    print(f'[watcher] 启动: 轮询 {JOURNAL} (间隔 {interval}s)')
    while True:
        try:
            if os.path.exists(LOCK):
                age = time.time() - os.path.getmtime(LOCK)
                if age > 1800:   # 死锁清理
                    os.remove(LOCK)
            n = count_open()
            if n > 0 and not os.path.exists(LOCK):
                open(LOCK, 'w').write(str(time.time()))
                try:
                    print(f'[watcher] {n} 条 open → 唤醒 hermes')
                    rc, out = wake_hermes(n)
                    print(f'[watcher] hermes rc={rc} | {out[-500:]}')
                finally:
                    if os.path.exists(LOCK):
                        os.remove(LOCK)
        except Exception as e:
            print(f'[watcher] 异常: {e}')
        time.sleep(interval)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--interval', type=int, default=120)
    main(ap.parse_args().interval)
