"""GQ 守护启动器——启动后台采集进程 (主采集器 = ws_collector, 2026-08-27 起)"""
import subprocess, sys, os, time

script = os.path.join(os.path.dirname(__file__), "ws_collector.py")
log = os.path.join(os.path.dirname(__file__), "ws_daemon.log")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 强制使用 venv Python, 防止被系统 Python 拉起造成双实例写库
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
if not os.path.exists(VENV_PY):
    VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

with open(log, "a", encoding="utf-8") as f:
    f.write(f"\n--- Daemon start at {time.strftime('%Y-%m-%d %H:%M:%S')} (venv={VENV_PY}) ---\n")

proc = subprocess.Popen(
    [VENV_PY, "-u", script, "--daemon", "-d", "0"],
    stdout=open(log, "a", encoding="utf-8"),
    stderr=subprocess.STDOUT,
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    cwd=os.path.dirname(script),
)
with open(log, "a", encoding="utf-8") as f:
    f.write(f"PID={proc.pid}\n")
print(f"GQ WS Collector PID: {proc.pid}")
