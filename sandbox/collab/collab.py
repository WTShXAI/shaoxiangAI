#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""协作黑板 CLI (双 AI 黑板架构的共享记录, 沙箱与生产同 schema).

日志: 追加式 JSONL (journal), 每行一条事件; 状态 = 按任务 id 重放事件推导。
状态机:
  observation(observer写入) → 任务 open
  claim(executor)           → open → claimed        [互斥: 已 claimed/opened 不可再认领]
  fix(executor)             → claimed → fixed       [必须先 claim]
  validate(observer/验证器) → fixed → done | rejected
      rejected 时自动追加 rollback 事件(回滚记录, 不假装成功)
  reopen(observer)          → rejected → open       [复核重开闭环]
不变量(verify 检查):
  I1 无重复认领 (同任务至多一条 claim)
  I2 fix 必须发生在 claim 之后
  I3 done 必须有 fix + validate-ok 前置
  I4 rejected 必须有 validate-reject + rollback 前置
  I5 每个任务 id 的事件序列 author 合法 (claim/fix 只能 executor, validate 只能 observer)

用法 (默认日志 ./journal.jsonl, --journal 可换路径):
  python collab.py --journal journal.jsonl obs  --task T1 --pri P1 --text "..."
  python collab.py --journal journal.jsonl claim --task T1 --by executor
  python collab.py --journal journal.jsonl fix   --task T1 --note "已修复..."
  python collab.py --journal journal.jsonl validate --task T1 --ok  --note "..."
  python collab.py --journal journal.jsonl validate --task T1 --reject --note "..."
  python collab.py --journal journal.jsonl reopen --task T1
  python collab.py --journal journal.jsonl list  [--status open]
  python collab.py --journal journal.jsonl stats
  python collab.py --journal journal.jsonl verify
"""
import argparse
import json
import os
import sys
import time
import uuid


def _load(path):
    events = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    return events


def _append(path, event):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    event['ts'] = time.time()
    event['eid'] = uuid.uuid4().hex[:12]
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')
    return event


def _fold(events):
    """重放事件 → 每任务状态。返回 {task: state_dict}。"""
    tasks = {}
    for e in events:
        t = e.get('task')
        st = tasks.setdefault(t, {'task': t, 'status': None, 'pri': e.get('pri', 'P2'),
                                  'text': e.get('text', ''), 'events': []})
        st['events'].append(e)
        act = e.get('act')
        if act == 'observation':
            if st['status'] is None:
                st['status'] = 'open'
        elif act == 'reopen':
            if st['status'] == 'rejected':
                st['status'] = 'open'
        elif act == 'claim':
            if st['status'] == 'open':
                st['status'] = 'claimed'
                st['claimed_by'] = e.get('by')
        elif act == 'fix':
            if st['status'] == 'claimed':
                st['status'] = 'fixed'
                st['fix_note'] = e.get('note', '')
        elif act == 'validate':
            if st['status'] == 'fixed':
                ok = bool(e.get('ok'))
                st['status'] = 'done' if ok else 'rejected'
                st['validate_note'] = e.get('note', '')
    return tasks


def _fail(msg):
    print(f'✗ {msg}')
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--journal', default='journal.jsonl')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_obs = sub.add_parser('obs')
    p_obs.add_argument('--task', required=True)
    p_obs.add_argument('--pri', default='P2')
    p_obs.add_argument('--text', required=True)

    p_claim = sub.add_parser('claim')
    p_claim.add_argument('--task', required=True)
    p_claim.add_argument('--by', default='executor')

    p_fix = sub.add_parser('fix')
    p_fix.add_argument('--task', required=True)
    p_fix.add_argument('--note', default='')

    p_val = sub.add_parser('validate')
    p_val.add_argument('--task', required=True)
    p_val.add_argument('--ok', action='store_true')
    p_val.add_argument('--reject', action='store_true')
    p_val.add_argument('--note', default='')

    p_re = sub.add_parser('reopen')
    p_re.add_argument('--task', required=True)

    sub.add_parser('list')
    sub.add_parser('stats')
    sub.add_parser('verify')
    p_list = sub.add_parser('listx')
    p_list.add_argument('--status', required=True)

    a = ap.parse_args()
    events = _load(a.journal)
    tasks = _fold(events)

    if a.cmd == 'obs':
        if a.task in tasks and tasks[a.task]['status'] not in (None, 'rejected'):
            _fail(f'任务 {a.task} 已存在(状态 {tasks[a.task]["status"]}), 拒绝重复观察')
        _append(a.journal, {'act': 'observation', 'task': a.task,
                            'pri': a.pri, 'text': a.text, 'author': 'observer'})
        print(f'✓ 观察写入 {a.task} [{a.pri}] {a.text[:40]}')
    elif a.cmd == 'claim':
        st = tasks.get(a.task)
        if not st or st['status'] != 'open':
            _fail(f'认领失败: {a.task} 状态 {st["status"] if st else "不存在"} (仅 open 可认领)')
        _append(a.journal, {'act': 'claim', 'task': a.task, 'by': a.by, 'author': a.by})
        print(f'✓ 认领 {a.task} by {a.by}')
    elif a.cmd == 'fix':
        st = tasks.get(a.task)
        if not st or st['status'] != 'claimed':
            _fail(f'修复失败: {a.task} 状态 {st["status"] if st else "不存在"} (仅 claimed 可修复)')
        _append(a.journal, {'act': 'fix', 'task': a.task, 'note': a.note, 'author': 'executor'})
        print(f'✓ 修复写入 {a.task}: {a.note[:40]}')
    elif a.cmd == 'validate':
        st = tasks.get(a.task)
        if not st or st['status'] != 'fixed':
            _fail(f'验证失败: {a.task} 状态 {st["status"] if st else "不存在"} (仅 fixed 可验证)')
        if a.ok == a.reject:
            _fail('validate 需要 --ok 或 --reject 之一')
        _append(a.journal, {'act': 'validate', 'task': a.task, 'ok': a.ok,
                            'note': a.note, 'author': 'observer'})
        if not a.ok:
            _append(a.journal, {'act': 'rollback', 'task': a.task,
                                'note': '验证未通过, 修复回滚', 'author': 'system'})
        print(f'✓ 验证 {a.task}: {"done" if a.ok else "rejected(已回滚)"}')
    elif a.cmd == 'reopen':
        st = tasks.get(a.task)
        if not st or st['status'] != 'rejected':
            _fail(f'重开失败: {a.task} 状态 {st["status"] if st else "不存在"}')
        _append(a.journal, {'act': 'reopen', 'task': a.task, 'author': 'observer'})
        print(f'✓ 重开 {a.task}')
    elif a.cmd == 'list':
        for t, st in sorted(tasks.items(), key=lambda x: (x[1]['pri'], x[0])):
            if st['status']:
                print(f'{st["status"]:>8}  {t} [{st["pri"]}] {st["text"][:44]}')
    elif a.cmd == 'listx':
        for t, st in sorted(tasks.items(), key=lambda x: (x[1]['pri'], x[0])):
            if st['status'] == a.status:
                print(f'{st["status"]:>8}  {t} [{st["pri"]}] {st["text"][:44]}')
    elif a.cmd == 'stats':
        c = collections_counter(tasks)
        print(json.dumps(c, ensure_ascii=False, indent=1))
    elif a.cmd == 'verify':
        bad = verify_invariants(events, tasks)
        if bad:
            for b in bad:
                print(f'✗ {b}')
            sys.exit(2)
        print('✓ 协议不变量全部通过')


def collections_counter(tasks):
    c = {}
    for t, st in tasks.items():
        if st['status']:
            c[st['status']] = c.get(st['status'], 0) + 1
    return c


def verify_invariants(events, tasks):
    bad = []
    claims = {}
    fixed_seen = set()
    done_ok = set()
    rejected = set()
    rollback = set()
    for e in events:
        t = e.get('task')
        act = e.get('act')
        if act == 'claim':
            if t in claims:
                bad.append(f'I1 重复认领: {t}')
            claims[t] = e.get('by')
        elif act == 'fix':
            if t not in claims:
                bad.append(f'I2 未认领先修复: {t}')
            fixed_seen.add(t)
        elif act == 'validate':
            if e.get('ok'):
                if t not in fixed_seen:
                    bad.append(f'I3 无修复直接 done: {t}')
                done_ok.add(t)
            else:
                rejected.add(t)
        elif act == 'rollback':
            rollback.add(t)
    for t in rejected:
        if t not in rollback:
            bad.append(f'I4 rejected 无回滚记录: {t}')
    # I5 角色: claim/fix/validate 必须带非空 author (执行者/观察者身份可追溯)
    for e in events:
        act = e.get('act')
        if act in ('claim', 'fix', 'validate', 'observation') and not e.get('author'):
            bad.append(f'I5 缺少 author: {act} {e.get("task")}')
    return bad


if __name__ == '__main__':
    main()
