#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生产守护 (prod_guardian) — 2026-09-24 事故后建立。

背景: 09-19 移除看门狗后, ws_collector / autonomous_monitor / efootball_probe
三者各自静默死亡且互不知晓, 仅 bridge_service + network_watchdog 存活 → 极易误判
"系统正常"。09-23 08:55 ~ 09-24 断流 25 小时才被发现。

职责 (只做存活保障 + 告警, 不做业务判定):
  1. 保证 ws_collector / efootball_probe 常驻; 死了就按官方入口重新拉起 (脱离式)。
  1b. 代拉周期任务 (跑完即退型): autonomous_monitor --cycle / gq_token_watch, 每小时一次
      —— 二者原靠外部调度, 实测会静默停摆且无自愈 (09-22 起), 现纳入本守护。
  2. 监听 gq/.env 的 GQ_REQUEST_ID: 一旦变化 → 认定换了新 token, 立刻拉起采集器
     (采集器本身热加载 .env, 但进程已死时必须有人把它拉起来 —— 这是本次缺口)。
  3. events.db 数据新鲜度监测 (只读): odds_changes 最新 tick 超过阈值未推进 → 告警。
  4. 乐鱼 token 失效 (code=0401013) 检测: 失效期间**不空转拉起**采集器, 每小时告警一次;
     token 一换就自动恢复。避免 2 分钟一轮 playwright 空转烧 CPU。

用法:
    python scripts/prod_guardian.py            # 常驻 (300s/拍)
    python scripts/prod_guardian.py --once     # 单拍自检并退出 (巡检/测试用)

注册 (与 network_watchdog 同款: 锁文件防多实例, 每日重拉, 活着则秒退):
    schtasks /Create /TN "ShaoxiangAI_ProdGuardian" /SC DAILY /ST 00:00 /F ^
      /TR "\"D:\\Architecture\\.venv\\Scripts\\pythonw.exe\" \"D:\\Architecture\\scripts\\prod_guardian.py\""

只读约束: 本脚本**不写** events.db (仅 sqlite ro 模式读 max(captured_at))。
IR-32: 不涉及任何跨庄共识字样。

⚠ 运维铁律 (2026-09-24 差点踩坑): 进程表里同一目标会同时出现**两条** pythonw —
   .venv shim (CPU=0, 父) + 系统 Python312 worker (CPU 持续增长, 子, 真身)。
   这是**正常单实例, 不是双写/双实例, 绝不能误杀** (见 gq-token-rotate skill 第 4 节)。
   辨别方法: 看 PPID 父子关系 + CPU 时间, 别只看进程数量。
   本守护**只做拉起, 从不杀任何进程** —— 保持这个性质, 避免重演 09-19 杀拉循环。
日志: logs/prod_guardian.log  (>5MB 自动砍半)
"""
import os
import re
import subprocess
import sys
import time
import sqlite3
from datetime import datetime

ROOT = r'D:\Architecture'
VENV_PY = os.path.join(ROOT, '.venv', 'Scripts', 'python.exe')
VENV_PYW = os.path.join(ROOT, '.venv', 'Scripts', 'pythonw.exe')
LOG = os.path.join(ROOT, 'logs', 'prod_guardian.log')
LOCK = os.path.join(ROOT, 'logs', 'prod_guardian.lock')
ENV_FILE = os.path.join(ROOT, 'gq', '.env')
WS_LOG = os.path.join(ROOT, 'gq', 'ws_daemon.log')
EVENTS_DB = os.path.join(ROOT, 'data', 'events.db')

CHECK_SEC = 300            # 巡检间隔
STALE_SEC = 3600           # events.db tick 停滞阈值 (60min) → 告警
TOKEN_ALERT_SEC = 3600     # token 失效告警节流
ROTATE_BYTES = 5 * 1024 * 1024
TOKEN_DEAD_MARK = '0401013'
MARK_FRESH_SEC = 1800        # 失效标记新鲜度窗口: 超过此秒数视为历史残留, 不再判定失效

# 常驻目标: (名称, 进程识别串, 启动命令, stdout 日志文件)
TARGETS = [
    ('ws_collector', 'ws_collector.py', [VENV_PY, os.path.join(ROOT, 'gq', 'start_collector.py')], None),
    ('efootball_probe', 'efootball_probe.py',
     [VENV_PY, os.path.join(ROOT, 'gq', 'efootball_probe.py'), '--loop'],
     os.path.join(ROOT, 'gq', 'efootball_daemon.out.log')),
]

# 周期任务 (非常驻, 跑完即退, 由本守护按间隔代拉):
#   autonomous_monitor 原靠 WorkBuddy 自动化每小时触发, gq_token_watch 靠 HOURLY;
#   两者实测都会静默停摆 (09-22 起) 且无自愈 → 2026-09-24 一并纳入本守护。
# (名称, 命令, 间隔秒, stdout 日志文件)
JOBS = [
    ('autonomous_monitor', [VENV_PY, os.path.join(ROOT, 'scripts', 'autonomous_monitor.py'), '--cycle'],
     3600, os.path.join(ROOT, 'logs', 'autonomous_monitor.log')),
    ('gq_token_watch', [VENV_PY, os.path.join(ROOT, 'scripts', 'gq_token_watch.py')],
     3600, os.path.join(ROOT, 'logs', 'gq_token_watch.log')),
]


def _log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line, flush=True)


def _rotate():
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > ROTATE_BYTES:
            with open(LOG, 'r', encoding='utf-8', errors='replace') as f:
                data = f.read()
            keep = data[-ROTATE_BYTES // 2:]
            with open(LOG, 'w', encoding='utf-8') as f:
                f.write('... (truncated)\n' + keep)
    except Exception:
        pass


def acquire_lock():
    """锁文件防多实例; 已有活实例则本进程退出。"""
    if os.path.exists(LOCK):
        try:
            pid = int(open(LOCK, encoding='utf-8').read().strip())
            try:
                import psutil
                if psutil.pid_exists(pid):
                    return False
            except Exception:
                out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'],
                                     capture_output=True, text=True,
                                     creationflags=0x08000000).stdout or ''
                if str(pid) in out:
                    return False
        except Exception:
            pass
    try:
        with open(LOCK, 'w', encoding='utf-8') as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return True


def _alive(pattern):
    """存活识别 (2026-09-24 加固): 必须同时满足
         (a) 进程名是 python*;
         (b) 某个命令行参数**以目标脚本名结尾**;
       不能只用子串匹配 —— 实测 shell/工具进程的 cmdline 会整段携带脚本正文,
       子串匹配会把"提到过 ws_collector.py 的无关进程"误判为存活, 导致永不拉起。
    """
    try:
        import psutil
        me = os.getpid()
        pat = pattern.replace('/', '\\')
        for p in psutil.process_iter(['name', 'cmdline']):
            try:
                if p.pid == me:
                    continue
                if 'python' not in (p.info.get('name') or '').lower():
                    continue
                cl = p.info.get('cmdline') or []
                if any(str(x).replace('/', '\\').endswith(pat) for x in cl):
                    return True
            except Exception:
                continue
        return False
    except ImportError:
        out = subprocess.run(['tasklist', '/FO', 'CSV'], capture_output=True,
                             text=True, creationflags=0x08000000).stdout or ''
        return 'python' in out.lower()  # 退化: 无法精确识别, 保守认为存活


def _launch(name, cmd, stdout_file):
    try:
        fh = open(stdout_file, 'a', encoding='utf-8') if stdout_file else subprocess.DEVNULL
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        _log(f'[LAUNCH] {name} → PID={proc.pid}')
        return True
    except Exception as e:
        _log(f'[LAUNCH][ERR] {name}: {type(e).__name__} {e}')
        return False


def read_token():
    """读 gq/.env 的 GQ_REQUEST_ID (不打印真实值)。"""
    try:
        for line in open(ENV_FILE, encoding='utf-8', errors='replace'):
            if line.startswith('GQ_REQUEST_ID='):
                return line.split('=', 1)[1].strip()
    except Exception:
        pass
    return ''


def env_fingerprint():
    """gq/.env 整体指纹 (2026-09-24 修正)。

    早期只监听 GQ_REQUEST_ID —— 但换号存在"**只有 sessionId 变、token 没变**"的场景
    (见 gq-token-rotate skill 第 0 节判断表), 此时只改 GQ_H5_URL, GQ_REQUEST_ID 不变
    → 守护检测不到变化、不会拉起采集器。故改为对整个 .env 做哈希, 任一行变化即触发。
    只存哈希, 不落盘、不打印任何真实值。
    """
    try:
        import hashlib
        with open(ENV_FILE, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return ''


def token_dead_from_log():
    """从 ws_daemon.log 检测乐鱼鉴权失败标记 (2026-09-24 修正: 必须时间感知 + 恢复感知)。

    早期实现只看"尾部 200KB 是否含 0401013" —— 换新 token 后旧标记仍在尾部,
    会把已恢复的系统继续误判为失效, 导致采集器永不拉起。
    2026-09-24 第一版改为"取最后一次标记, 仅在 MARK_FRESH_SEC 内认失效" —— 但仍有一个缺口:
    换号后采集器用旧 token 写的新 0401013 标记在 30 分钟内会让守护持续误判失效,
    即便采集器已修好连上新 token 写出成功刷新, 守护仍卡在失效态不拉起。
    现加入**恢复证据**: 若尾部窗口内存在晚于最后一次失效标记的"[REG] 刷新完成"成功行,
    即认定已恢复 → 返回 False (不失效)。
    """
    try:
        if not os.path.exists(WS_LOG):
            return False
        with open(WS_LOG, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200 * 1024))
            lines = f.read().splitlines()
        last_ts = None
        mark_ts = None          # 最后一次 0401013 失效标记时间戳
        ok_ts = None            # 最后一次成功刷新时间戳
        for ln in lines:
            m = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', ln)
            if m:
                try:
                    last_ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
                except Exception:
                    pass
                if '[REG] 刷新完成' in ln and last_ts is not None:
                    ok_ts = last_ts     # 成功刷新 (列表非空登记)
                continue
            # 标记行自身无时间戳 (如 "[WARN] 比赛列表 code=0401013"), 沿用上方最近时间戳
            if TOKEN_DEAD_MARK in ln and last_ts is not None:
                mark_ts = last_ts          # 正序扫描, 持续覆盖 → 最终保留**最后一次**
        # 恢复证据优先: 成功刷新晚于最后一次失效标记 → 已恢复
        if ok_ts is not None and mark_ts is not None and ok_ts > mark_ts:
            return False
        if mark_ts is None:
            return False
        return (datetime.now() - mark_ts).total_seconds() < MARK_FRESH_SEC
    except Exception:
        return False


def data_age_sec():
    """events.db 最新 tick 距今秒数; 读不到返回 None (只读, ro 模式)。"""
    try:
        con = sqlite3.connect('file:' + EVENTS_DB.replace('\\', '/') + '?mode=ro', uri=True)
        try:
            row = con.execute('select max(captured_at) from odds_changes').fetchone()
        finally:
            con.close()
        if not row or row[0] is None:
            return None
        return max(0.0, time.time() - float(row[0]))
    except Exception as e:
        _log(f'[FRESH][ERR] {type(e).__name__} {e}')
        return None


def run_jobs(state):
    """周期任务代拉 (跑完即退型脚本)。放在 token 门禁**之前** ——
    autonomous_monitor 的预测补算/叙事增量不依赖 GQ, gq_token_watch 正是 token 告警源,
    两者在 token 失效期间反而更该跑。
    """
    now = time.time()
    for name, cmd, interval, out_f in JOBS:
        key = 'job_' + name
        if now - state.get(key, 0) < interval:
            continue
        state[key] = now
        try:
            fh = open(out_f, 'a', encoding='utf-8') if out_f else subprocess.DEVNULL
            proc = subprocess.Popen(
                cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            _log(f'[JOB] {name} → PID={proc.pid} (间隔 {interval}s)')
        except Exception as e:
            _log(f'[JOB][ERR] {name}: {type(e).__name__} {e}')


def tick(state, force_launch=False):
    """单拍巡检。state: dict(可变, 保存跨拍状态)。"""
    run_jobs(state)
    fp = env_fingerprint()
    if fp and state.get('last_env_fp') and fp != state['last_env_fp']:
        _log('[ENV] 检测到 gq/.env 已变更 (token 或 sessionId) → 清除失效态, 立即拉起采集器')
        state['token_dead'] = False
        state['last_alert'] = 0
        force_launch = True
    state['last_env_fp'] = fp or state.get('last_env_fp')

    dead = token_dead_from_log()
    if dead and not state.get('token_dead'):
        _log('[TOKEN][ALERT] 乐鱼 token 失效 (code=0401013)。'
             '需更新 gq/.env 的 GQ_REQUEST_ID; 更新后本守护将在 5 分钟内自动拉起采集器, 无需人工重启。')
        state['token_dead'] = True
    elif not dead:
        state['token_dead'] = False

    if state.get('token_dead'):
        now = time.time()
        if now - state.get('last_alert', 0) > TOKEN_ALERT_SEC:
            _log('[TOKEN][ALERT] 仍处失效态 — 采集器暂停拉起 (避免空转),'
                 ' 等待新 token 写入 gq/.env')
            state['last_alert'] = now
        if not force_launch:
            return

    for name, pattern, cmd, out_f in TARGETS:
        if _alive(pattern):
            continue
        _log(f'[DOWN] {name} 不在进程表 → 拉起')
        _launch(name, cmd, out_f)

    age = data_age_sec()
    if age is None:
        _log('[FRESH][WARN] 无法读取 events.db 最新 tick')
    elif age > STALE_SEC:
        if not state.get('stale'):
            _log(f'[STALE][ALERT] events.db 最新 tick 已 {age/3600:.1f} 小时未推进 (阈值 {STALE_SEC/3600:.1f}h) → 疑似断流')
            state['stale'] = True
    else:
        if state.get('stale'):
            _log(f'[FRESH] 数据恢复流动 (滞后 {age/60:.0f} 分钟)')
            state['stale'] = False


def main():
    once = '--once' in sys.argv
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    _rotate()
    if not acquire_lock():
        _log('[LOCK] 已有活实例, 本进程秒退')
        return 0
    _log(f'[BOOT] prod_guardian PID={os.getpid()} 间隔={CHECK_SEC}s ({"单拍" if once else "常驻"})')

    state = {'last_env_fp': env_fingerprint(), 'token_dead': False, 'last_alert': 0, 'stale': False}
    tick(state)
    if once:
        _log('[ONCE] 单拍完成, 退出')
        return 0

    while True:
        try:
            time.sleep(CHECK_SEC)
            tick(state)
            _rotate()
        except KeyboardInterrupt:
            _log('[EXIT] 收到中断, 退出')
            return 0
        except Exception as e:
            _log(f'[LOOP][ERR] {type(e).__name__} {e}')
            time.sleep(CHECK_SEC)


if __name__ == '__main__':
    sys.exit(main())
