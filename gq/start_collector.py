"""GQ 采集器 脱离式启动器 (DETACHED, 跨会话存活)

【状态 2026-08-27】主采集器已切换为 ws_collector.py (乐鱼 WS 实时推送流, 全市场+内容).
本启动器现拉起 ws_collector (守护模式 -d 0 = 无限)。
看门狗已整体移除 (2026-09-19, ShaoxiangGQ_Watchdog 计划任务 + watchdog_collector.py +
collector_supervisor.py): 过期 token 期间的 5 分钟杀拉循环曾造成重启风暴与双实例互踩。
存活性由每小时 autonomous_monitor 采集停滞告警接手 (发现停滞 → 人工/会话执行本文件重启)。

与 launcher.py(CREATE_NO_WINDOW) 的区别: 使用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
子进程独立成新会话, 不被父 shell/调度进程树回收 —— 解决"启动后随 Bash 会话结束被杀死"的问题.
"""
import subprocess, sys, os, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 强制使用 venv Python, 防止被系统 Python 拉起造成双实例写库
# 2026-09-16 修正: 优先用控制台 python.exe。
# 原 pythonw.exe 在 DETACHED 下偶发卡死在初始化前(stdout 句柄/控制台子系统问题),
# 导致 detached 实例起不来; 控制台 python.exe + DETACHED_PROCESS 不弹窗且 stdout 已重定向到日志, 稳定。
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(VENV_PY):
    VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")

script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ws_collector.py")
log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ws_daemon.log")

with open(log, "a", encoding="utf-8") as f:
    f.write(f"\n--- Detached start at {time.strftime('%Y-%m-%d %H:%M:%S')} (venv={VENV_PY}) ---\n")

proc = subprocess.Popen(
    [VENV_PY, "-u", script, "-d", "0"],
    stdout=open(log, "a", encoding="utf-8"),
    stderr=subprocess.STDOUT,
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    cwd=os.path.dirname(script),
)
with open(log, "a", encoding="utf-8") as f:
    f.write(f"DETACHED PID={proc.pid}\n")
print(f"GQ WS Collector launched (detached) PID: {proc.pid}")
