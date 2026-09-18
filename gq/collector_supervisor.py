"""采集器外部监督进程 (2026-09-16, v2 修复风暴)

v1 问题: 无退避(每10s重启) + 硬杀残留的 headless Edge 清不掉 → 孤儿 OOM 死亡螺旋.
v2 修复:
 - 自身 PID 写入 gq/.supervisor.lock, 便于需要时精准停止
 - 指数退避: 距上次重启 <BACKOFF 秒则等待, 不紧循环
 - 每次重启前 taskkill /IM msedge.exe /F 清掉硬杀残留的 headless Edge(防 OOM 螺旋)
"""
import subprocess, time, os, sqlite3, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
SCRIPT = os.path.join(ROOT, "gq", "ws_collector.py")
LOG = os.path.join(ROOT, "gq", "ws_daemon.log")
SLOG = os.path.join(ROOT, "gq", "supervisor.log")
LOCK = os.path.join(ROOT, "gq", ".ws_collector.lock")
SPID = os.path.join(ROOT, "gq", ".supervisor.lock")
DB = os.path.join(ROOT, "data", "events.db")

STALE_SEC = 150
CHECK_SEC = 20
BACKOFF_SEC = 60
FAST_DEATH_SEC = 120

DETACHED = 0x00000008
NEWGROUP = 0x00000200


def ts():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slog(msg):
    with open(SLOG, "a", encoding="utf-8") as f:
        f.write(f"[{ts()}] {msg}\n")


def wlog(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n[SUPERVISOR-EXT] {msg}\n")


def db_max():
    try:
        c = sqlite3.connect(DB)
        v = c.execute("select max(captured_at) from odds_snapshots").fetchone()[0]
        return v or 0
    except Exception:
        return 0


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def read_lock_pid():
    try:
        s = open(LOCK, "r", encoding="utf-8").read().strip()
        return int(s) if s.isdigit() else None
    except Exception:
        return None


def kill_tree(pid):
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def kill_all_msedge():
    try:
        r = subprocess.run(["taskkill", "/IM", "msedge.exe", "/F"],
                           capture_output=True, timeout=15)
        out = r.stdout.decode("utf-8", "ignore").strip().replace("\n", " ")
        slog(f"清 msedge: {out[:140]}")
    except Exception as e:
        slog(f"清 msedge 失败: {e}")


def start_child():
    try:
        os.remove(LOCK)
    except Exception:
        pass
    return subprocess.Popen(
        [VENV_PY, SCRIPT, "-d", "0"],
        stdout=open(LOG, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        creationflags=DETACHED | NEWGROUP,
        cwd=os.path.dirname(SCRIPT),
    )


def main():
    with open(SPID, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    slog(f"=== supervisor v2 started pid={os.getpid()} ===")
    last_restart = 0.0
    child = None
    child_start = 0.0
    while True:
        try:
            now = time.time()
            lock_pid = read_lock_pid()
            alive = pid_alive(lock_pid)
            fresh = (now - db_max()) < STALE_SEC
            if alive and fresh:
                time.sleep(CHECK_SEC)
                continue
            since = now - last_restart
            if since < BACKOFF_SEC:
                time.sleep(BACKOFF_SEC - since)
                continue
            reasons = []
            if not alive:
                reasons.append(f"锁PID {lock_pid} 已死")
            if not fresh:
                reasons.append(f"假死(>{STALE_SEC}s 无新快照)")
            uptime = (child_start and (now - child_start)) or 0
            fast = uptime > 0 and uptime < FAST_DEATH_SEC
            msg = f"重启采集器: {', '.join(reasons)} (uptime={uptime:.0f}s{' FAST' if fast else ''})"
            slog(msg)
            wlog(msg)
            if lock_pid and pid_alive(lock_pid):
                kill_tree(lock_pid)
            kill_all_msedge()  # 清孤儿, 防 OOM 螺旋
            try:
                os.remove(LOCK)
            except Exception:
                pass
            child = start_child()
            child_start = time.time()
            last_restart = time.time()
            slog(f"新采集器 PID={child.pid}")
            time.sleep(10)
        except Exception as e:
            slog(f"loop err: {e}")
            time.sleep(CHECK_SEC)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
