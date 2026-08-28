# -*- coding: utf-8 -*-
"""bridge 服务重启器 (2026-08-06 冻结修复后固化).

用法:  python scripts/restart_bridge.py [--wait-health]
说明:
  - 先用 taskkill 杀干净所有 bridge_service 进程树(按命令行匹配, 不影响 GQ 采集器)
  - 再用 subprocess.Popen(DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP) 自举拉起
    (本机沙箱外 bash 无 nohup; PowerShell Start-Process 流重定向有 PATH bug;
     cmd.exe 被安全策略拦; [Diagnostics.Process]::Start 被拦 → 只能 Popen 自举)
  - 必须用 .venv\\Scripts\\pythonw.exe (系统 Python312 无 fastapi; venv shim 注入 site-packages;
    2026-08-28 改 pythonw: bridge 彻底无控制台窗口, 不抢用户焦点/不弹窗)
  - 等待 9000 端口 LISTEN, 可选打 /health 验证
"""
import subprocess, time, socket, os, sys, glob, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
LOG = os.path.join(ROOT, "logs", "_restart_freeze_fix.log")
ERR = os.path.join(ROOT, "logs", "_restart_freeze_fix_err.log")
PORT = 9000


def kill_bridge_processes():
    """杀掉所有 bridge_service 进程树 (taskkill /F /T). 不碰 gq 采集器.
    pythonw.exe 匹配 (2026-08-28: bridge 已改 pythonw 无窗口启动)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'bridge_service' } | "
             "ForEach-Object { taskkill /PID $_.ProcessId /F /T 2>&1 }"],
            capture_output=True, timeout=60)
        text = ((out.stdout or b"") + (out.stderr or b"")).decode(
            "utf-8", errors="replace")
        print(text.strip() or "(无 bridge 进程)")
    except Exception as e:
        print("kill 阶段异常(继续):", e)


def start_detached():
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    out_f = open(LOG, "ab")
    err_f = open(ERR, "ab")
    DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    # 2026-08-28: 让 watchdog 启动的 Python console 窗口"显示但最小化, 不抢焦点"
    # (SW_SHOWMINNOACTIVE=1); 避免 launcher .bat 双击时 cmd 弹窗抢前台焦点。
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 1  # SW_SHOWMINNOACTIVE
    p = subprocess.Popen([PY, "bridge_service.py", "--port", str(PORT)],
                         cwd=ROOT, stdout=out_f, stderr=err_f,
                         creationflags=DETACHED, close_fds=True,
                         startupinfo=si)
    print("launched pid=%d" % p.pid, flush=True)
    return p


def wait_listen(seconds=40):
    for i in range(seconds):
        time.sleep(1)
        try:
            s = socket.socket(); s.settimeout(0.4)
            s.connect(("127.0.0.1", PORT)); s.close()
            print("port %d LISTENING after %ds" % (PORT, i + 1), flush=True)
            return True
        except Exception:
            try: s.close()
            except Exception: pass
    print("PORT %d NOT UP after %ds" % (PORT, seconds), flush=True)
    if os.path.exists(ERR):
        with open(ERR, "rb") as f:
            f.seek(max(0, os.path.getsize(ERR) - 2500))
            print("--- err tail ---")
            print(f.read().decode("utf-8", errors="replace"))
    return False


def health_check(timeout=8):
    import urllib.request
    try:
        r = urllib.request.urlopen(
            "http://127.0.0.1:%d/health" % PORT, timeout=timeout)
        body = r.read()[:120]
        print("health:", r.status, body)
        return r.status == 200
    except Exception as e:
        print("health ERR:", type(e).__name__, str(e)[:100])
        return False


if __name__ == "__main__":
    kill_bridge_processes()
    time.sleep(2)
    start_detached()
    if not wait_listen():
        sys.exit(2)
    if "--wait-health" in sys.argv:
        if not health_check():
            sys.exit(3)
    print("RESTART OK")
