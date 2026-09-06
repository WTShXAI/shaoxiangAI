# -*- coding: utf-8 -*-
"""哨响AI 后端(bridge :9000)看门狗 (2026-08-15 新增).

背景: bridge_service 之前无看门狗, 一旦静默退出(被杀/OOM/会话回收)全站前端进不去,
只能人工重启. GQ 采集器已有 ShaoxiangGQ_Watchdog, 此处补齐 bridge 的等效护栏.

行为:
  - 先打 /health (含端口 LISTEN 兜底). 健康则什么都不做, 直接退出 (幂等, 可高频跑).
  - 仅当 9000 失联时才 kill + 脱离式拉起 (逻辑复用 restart_bridge.py).
  - 必须用 .venv/Scripts/pythonw.exe (2026-08-28: 改 pythonw 彻底无窗口, 不抢焦点; 系统 Python312 无 fastapi).

注册 (参考 GQ 看门狗): 计划任务 ShaoxiangBridge_Watchdog, 每 5 分钟, 用 venv pythonw.exe 跑本脚本.
"""
import subprocess, time, socket, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
PORT = 9000
LOG = os.path.join(ROOT, "logs", "bridge_watchdog.log")
ERR = os.path.join(ROOT, "logs", "bridge_watchdog_err.log")


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def is_healthy():
    """端口 LISTEN + /health 200 才算健康."""
    # 1) 端口连通
    try:
        s = socket.socket(); s.settimeout(0.5)
        s.connect(("127.0.0.1", PORT)); s.close()
    except Exception:
        return False
    # 2) health 端点
    try:
        import urllib.request
        r = urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=5)
        return r.status == 200
    except Exception:
        return False


def kill_bridge_processes():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'bridge_service' } | "
             "ForEach-Object { taskkill /PID $_.ProcessId /F /T 2>&1 }"],
            capture_output=True, timeout=60)
        text = ((out.stdout or b"") + (out.stderr or b"")).decode("utf-8", errors="replace")
        return text.strip() or "(无 bridge 进程)"
    except Exception as e:
        return "kill 异常(继续): %s" % e


def start_detached():
    out_f = open(LOG, "ab"); err_f = open(ERR, "ab")
    DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 1  # SW_SHOWMINNOACTIVE: 显示但最小化, 不抢焦点
    p = subprocess.Popen([PY, "bridge_service.py", "--port", str(PORT)],
                         cwd=ROOT, stdout=out_f, stderr=err_f,
                         creationflags=DETACHED, close_fds=True,
                         startupinfo=si)
    return p.pid


def wait_listen(seconds=40):
    for i in range(seconds):
        time.sleep(1)
        try:
            s = socket.socket(); s.settimeout(0.4)
            s.connect(("127.0.0.1", PORT)); s.close()
            return i + 1
        except Exception:
            try: s.close()
            except Exception: pass
    return -1


def main():
    with open(LOG, "ab") as f:
        f.write(("\n[%s] watchdog tick\n" % _ts()).encode("utf-8"))

    if is_healthy():
        with open(LOG, "ab") as f:
            f.write(("[%s] HEALTHY, 跳过重启\n" % _ts()).encode("utf-8"))
        return 0

    # 失联 -> 拉起
    with open(LOG, "ab") as f:
        f.write(("[%s] UNHEALTHY, 尝试重启 bridge\n" % _ts()).encode("utf-8"))

    kill_msg = kill_bridge_processes()
    time.sleep(2)
    pid = start_detached()
    with open(LOG, "ab") as f:
        f.write(("[%s] launched pid=%s; kill: %s\n" % (_ts(), pid, kill_msg[:200])).encode("utf-8"))

    waited = wait_listen()
    if waited > 0:
        with open(LOG, "ab") as f:
            f.write(("[%s] LISTEN after %ds, 重启成功\n" % (_ts(), waited)).encode("utf-8"))
        return 0
    else:
        with open(LOG, "ab") as f:
            f.write(("[%s] 重启失败: 端口 %d 未起来\n" % (_ts(), PORT)).encode("utf-8"))
        return 2


if __name__ == "__main__":
    sys.exit(main())
