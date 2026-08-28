"""维护窗口: 干净暂停 GQ 采集器 (禁用看门狗任务 + 杀 shim+worker), 不动 bridge。
仅供维护窗口使用; 恢复请用 gq_resume.py。
"""
import subprocess, sys, time, os
try:
    import psutil
except ImportError:
    print("NO_PSUTIL"); sys.exit(2)

def find_collector_pids():
    pids = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
            if "auto_collector" in cmd or "start_collector" in cmd:
                pids.append(p.info["pid"])
        except Exception:
            continue
    return pids

print("[1] disable watchdog scheduled task")
r = subprocess.run(["schtasks", "/change", "/disable", "/tn", "ShaoxiangGQ_Watchdog"],
                  capture_output=True, text=True)
print("    schtasks rc=%s out=%s" % (r.returncode, (r.stdout or r.stderr).strip()[:200]))

print("[2] find collector procs")
pids = find_collector_pids()
print("    found PIDs:", pids)
for pid in pids:
    try:
        p = psutil.Process(pid)
        print("    kill PID=%d cmd=%s" % (pid, " ".join(p.cmdline())[:120]))
        p.terminate()
    except Exception as e:
        print("    terminate err %d: %s" % (pid, e))

time.sleep(3)
# SIGKILL any survivors
survivors = find_collector_pids()
for pid in survivors:
    try:
        psutil.Process(pid).kill()
        print("    force-killed PID=%d" % pid)
    except Exception:
        pass
time.sleep(1)

remaining = find_collector_pids()
print("[3] remaining collector procs:", remaining)
print("RESULT:", "PAUSED_OK" if not remaining else "STILL_RUNNING:%s" % remaining)
