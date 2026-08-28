"""哨响AI 后端 脱离式启动器 (DETACHED, 跨会话存活)

启动 D:/Architecture/bridge_service.py -> 0.0.0.0:9000
与 gq/start_collector.py 同模式: 使用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
子进程独立成新会话, 不被父 shell/Bash 工具进程树回收.

用法:
  D:/Architecture/.venv/Scripts/python.exe start_backend.py
"""
import subprocess, sys, os, time

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")  # 2026-08-28: pythonw 无窗口不抢焦点
script = os.path.join(ROOT, "bridge_service.py")
log = os.path.join(ROOT, "backend_daemon.log")

if not os.path.exists(VENV_PY):
    print(f"[ERR] 找不到 venv python: {VENV_PY}")
    sys.exit(1)
if not os.path.exists(script):
    print(f"[ERR] 找不到后端入口: {script}")
    sys.exit(1)

with open(log, "a", encoding="utf-8") as f:
    f.write(f"\n--- Detached backend start at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")

proc = subprocess.Popen(
    [VENV_PY, "-u", script],
    stdout=open(log, "a", encoding="utf-8"),
    stderr=subprocess.STDOUT,
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    cwd=ROOT,
)
with open(log, "a", encoding="utf-8") as f:
    f.write(f"DETACHED backend PID={proc.pid}\n")
print(f"Backend launched (detached) PID: {proc.pid}  (日志: {log})")
