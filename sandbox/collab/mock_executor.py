#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模拟执行 AI: 认领 → 修复 → 验证(含失败路径), 全自动处理黑板上的 open 任务.

修复剧本按任务前缀路由 (模拟真实执行 AI 的能力域):
  ANOM_*          → 修复成功并自验 (绝大多数)
  *_FORCE_FAIL    → 修复后自验失败 → 验证 reject (失败路径演练)
处理规则与真实执行 AI 章程一致: 先认领(互斥) → 修复 → 自验 → 提交验证。
"""
import json
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAIL_PATTERNS = ('FORCE_FAIL',)


def _collab(journal, *args):
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), 'collab.py'),
                        '--journal', journal, *args], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def open_tasks(journal):
    rc, out, _ = _collab(journal, 'listx', '--status', 'open')
    tasks = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == 'open':
            tasks.append(parts[1])
    return tasks


def run(journal):
    claimed = fixed = validated = rejected = 0
    for task in open_tasks(journal):
        rc, _, _ = _collab(journal, 'claim', '--task', task, '--by', 'mock_executor')
        if rc != 0:
            continue   # 被别人认领(互斥生效)
        claimed += 1
        force_fail = any(p in task for p in FAIL_PATTERNS)
        note = '修复完成(模拟): 数据回填+守卫+验证通过' if not force_fail else \
               '修复尝试失败(演练): 方案被验证否决, 回滚'
        _collab(journal, 'fix', '--task', task, '--note', note)
        fixed += 1
        if force_fail:
            _collab(journal, 'validate', '--task', task, '--reject',
                    '--note', '回测回退 46.7→17.1%, 拒绝并回滚')
            rejected += 1
        else:
            _collab(journal, 'validate', '--task', task, '--ok', '--note', '回测不回退, 审计0违反')
            validated += 1
    print(f'[mock_executor] 认领 {claimed} | 修复 {fixed} | 验证通过 {validated} | 验证拒绝 {rejected}')


if __name__ == '__main__':
    journal = sys.argv[sys.argv.index('--journal') + 1] if '--journal' in sys.argv else 'journal.jsonl'
    run(journal)
