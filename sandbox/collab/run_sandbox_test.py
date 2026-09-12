#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""沙箱端到端场景运行器 (S1 协议机制 / S2 回放循环 / S3 失败路径).

用法: python run_sandbox_test.py
每轮在独立日志上运行, 断言全部通过输出 PASS, 任一失败输出 FAIL 并 exit 1。
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import collab  # noqa: E402

PY = sys.executable


def run(journal, *args):
    return subprocess.run([PY, os.path.join(HERE, 'collab.py'), '--journal', journal, *args],
                          capture_output=True, text=True)


def mock_obs(journal):
    subprocess.run([PY, os.path.join(HERE, 'mock_observer.py'), '--journal', journal],
                   capture_output=True, text=True)


def mock_exec(journal):
    subprocess.run([PY, os.path.join(HERE, 'mock_executor.py'), '--journal', journal],
                   capture_output=True, text=True)


class Assertion:
    def __init__(self):
        self.fails = []

    def check(self, cond, msg):
        print(f'  {"✓" if cond else "✗"} {msg}')
        if not cond:
            self.fails.append(msg)


def load_tasks(journal):
    events = collab._load(journal)
    return collab._fold(events)


def s1_protocol(journal, A):
    """S1 协议机制: 全状态机 + 异常路径."""
    print('── S1 协议机制 ──')
    if os.path.exists(journal):
        os.remove(journal)
    # 观察写入
    run(journal, 'obs', '--task', 'T1', '--pri', 'P1', '--text', '测试异常1')
    run(journal, 'obs', '--task', 'T2', '--pri', 'P2', '--text', '测试异常2')
    tasks = load_tasks(journal)
    A.check(tasks['T1']['status'] == 'open', 'S1 观察写入→open')
    # 重复观察拒绝(脚本 exit 2)
    rc = run(journal, 'obs', '--task', 'T1', '--pri', 'P1', '--text', '重复').returncode
    A.check(rc != 0, 'S1 重复观察被拒绝')
    # 互斥认领
    run(journal, 'claim', '--task', 'T1', '--by', 'executor')
    tasks = load_tasks(journal)
    A.check(tasks['T1']['status'] == 'claimed', 'S1 认领→claimed')
    rc = run(journal, 'claim', '--task', 'T1', '--by', 'executor2').returncode
    A.check(rc != 0, 'S1 重复认领被拒绝(互斥)')
    # 未认领先修复被拒
    rc = run(journal, 'fix', '--task', 'T2', '--note', 'x').returncode
    A.check(rc != 0, 'S1 未认领任务不可修复')
    # 修复
    run(journal, 'fix', '--task', 'T1', '--note', '已修复')
    tasks = load_tasks(journal)
    A.check(tasks['T1']['status'] == 'fixed', 'S1 修复→fixed')
    # 验证 ok → done
    run(journal, 'validate', '--task', 'T1', '--ok', '--note', '回测通过')
    tasks = load_tasks(journal)
    A.check(tasks['T1']['status'] == 'done', 'S1 验证ok→done')
    # T2: claim → fix → reject → 回滚记录 → reopen
    run(journal, 'claim', '--task', 'T2', '--by', 'executor')
    run(journal, 'fix', '--task', 'T2', '--note', '尝试')
    run(journal, 'validate', '--task', 'T2', '--reject', '--note', '回测回退')
    tasks = load_tasks(journal)
    A.check(tasks['T2']['status'] == 'rejected', 'S1 验证reject→rejected')
    ev = [e['act'] for e in tasks['T2']['events']]
    A.check('rollback' in ev, 'S1 reject 自动回滚记录')
    run(journal, 'reopen', '--task', 'T2')
    tasks = load_tasks(journal)
    A.check(tasks['T2']['status'] == 'open', 'S1 rejected→reopen 闭环')
    # 不变量
    rc = run(journal, 'verify').returncode
    A.check(rc == 0, 'S1 verify 不变量通过')


def s2_replay(journal, A):
    """S2 回放循环: 真实异常全自动处理."""
    print('── S2 真实异常回放 ──')
    if os.path.exists(journal):
        os.remove(journal)
    mock_obs(journal)
    tasks = load_tasks(journal)
    n_obs = len(tasks)
    A.check(n_obs >= 20, f'S2 观察回放 ≥20 条 (实际 {n_obs})')
    # 三轮处理循环(模拟多周期)
    for _ in range(3):
        mock_exec(journal)
    tasks = load_tasks(journal)
    open_n = sum(1 for st in tasks.values() if st['status'] == 'open')
    claimed = sum(1 for st in tasks.values() if st['status'] == 'claimed')
    done = sum(1 for st in tasks.values() if st['status'] == 'done')
    A.check(open_n == 0, f'S2 无残留 open (实际 {open_n})')
    A.check(claimed == 0, f'S2 无残留 claimed (实际 {claimed})')
    A.check(done >= 20, f'S2 绝大多数 done (实际 {done})')
    rc = run(journal, 'verify').returncode
    A.check(rc == 0, 'S2 verify 不变量通过')
    # 幂等: 再跑一轮 observer+executor, 不产生新 open
    mock_obs(journal)
    mock_exec(journal)
    tasks = load_tasks(journal)
    open_n = sum(1 for st in tasks.values() if st['status'] == 'open')
    A.check(open_n == 0, 'S2 幂等重放无新 open')


def s3_failure(journal, A):
    """S3 失败路径: 强制失败任务正确流转 rejected+回滚."""
    print('── S3 失败路径 ──')
    if os.path.exists(journal):
        os.remove(journal)
    run(journal, 'obs', '--task', 'ANOM_X_FORCE_FAIL', '--pri', 'P1', '--text', '演练失败路径')
    run(journal, 'claim', '--task', 'ANOM_X_FORCE_FAIL', '--by', 'mock_executor')
    run(journal, 'fix', '--task', 'ANOM_X_FORCE_FAIL', '--note', '尝试修复')
    run(journal, 'validate', '--task', 'ANOM_X_FORCE_FAIL', '--reject', '--note', '回测否决')
    tasks = load_tasks(journal)
    st = tasks['ANOM_X_FORCE_FAIL']
    acts = [e['act'] for e in st['events']]
    A.check(st['status'] == 'rejected', 'S3 强制失败→rejected')
    A.check('rollback' in acts, 'S3 回滚记录存在')
    A.check(st.get('validate_note', '') != '', 'S3 拒绝原因留痕')
    # reopen 重开后可再次处理(闭环)
    run(journal, 'reopen', '--task', 'ANOM_X_FORCE_FAIL')
    tasks = load_tasks(journal)
    A.check(tasks['ANOM_X_FORCE_FAIL']['status'] == 'open', 'S3 rejected→reopen')


def main():
    A = Assertion()
    print('══ 哨响双 AI 黑板协作 · 沙箱端到端测试 ══')
    with tempfile.TemporaryDirectory() as td:
        s1_protocol(os.path.join(td, 's1.jsonl'), A)
        s2_replay(os.path.join(td, 's2.jsonl'), A)
        s3_failure(os.path.join(td, 's3.jsonl'), A)
    print('════════════════════════════')
    if A.fails:
        print(f'FAIL: {len(A.fails)} 项断言失败')
        for f in A.fails:
            print(f'  - {f}')
        sys.exit(1)
    print('PASS: 全部断言通过')
    json.dump({'result': 'PASS', 'fails': []}, open(os.path.join(HERE, 'last_run.json'), 'w', encoding='utf-8'))


if __name__ == '__main__':
    main()
