# -*- coding: utf-8 -*-
"""哨响AI Windows 进程守护 (对标 scripts/restart_bridge.py, 扩展为常驻守护).

职责 (合并交付 ②进程守护 / ③健康检查+自动重启 / ④日志轮转):
  1. 用 subprocess.Popen(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) 自举拉起
     bridge_service.py 与 gq/auto_collector.py (本机沙箱外无 nohup;
     PowerShell Start-Process / cmd.exe / Diagnostics.Process 均被拦或 PATH bug,
     唯一可靠 = Popen 自举). 必须用 .venv\\Scripts\\python.exe shim
     (系统 Python312 无 fastapi, venv 注入 site-packages).
  2. 常驻循环: 每 CHECK_INTERVAL 秒检查两个托管进程:
       - bridge:   进程存活 + http://127.0.0.1:PORT/health 200 (含超时=冻结判定)
       - collector: 进程存活 + events.db mtime 新鲜度 (采集器唯一外部信号)
     任一死亡/假死 -> taskkill 按命令行关键字精准清除 -> 重新 Popen 自举.
  3. 子进程 stdout/stderr 经管道泵入 RotatingLogWriter (按大小轮转,
     ROTATE_MAX_BYTES x ROTATE_BACKUPS), 彻底杜绝历史 logs 膨胀 1.2GB 的问题.
  4. 守护自身日志也走 Python RotatingFileHandler (ASCII-only, 规避 GBK 崩溃铁律).

用法:
  python deploy/windows/daemon_guard.py            # 前台常驻 (Task Scheduler 拉起)
  python deploy/windows/daemon_guard.py --run-once # 仅巡检一次并退出 (调试)
"""
import os
import sys
import time
import subprocess
import threading
import logging
import logging.handlers
import urllib.request

# ===================== 配置 (按需修改) =====================
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")  # 2026-08-28: pythonw 无窗口不抢焦点
LOG_DIR = os.path.join(ROOT, "logs")
BRIDGE_PORT = 9000
HEALTH_URL = "http://127.0.0.1:%d/health" % BRIDGE_PORT
HEALTH_TIMEOUT = 8          # /health 超时(秒) -> 视为冻结
CHECK_INTERVAL = 15         # 巡检周期(秒)
FREEZE_RETRIES = 2          # 连续 N 次 health 失败 -> 判冻结重启
COLLECTOR_MAX_MIN = 10      # 采集器 events.db 超过 N 分钟未更新 -> 假死
ROTATE_MAX_BYTES = 20 * 1024 * 1024   # 单日志文件上限 20MB
ROTATE_BACKUPS = 5                    # 保留 5 个备份

MANAGED = {
    "bridge": {
        "cmd": [PY, "bridge_service.py", "--port", str(BRIDGE_PORT)],
        "match": "bridge_service",     # taskkill 命令行关键字 (精准, 不碰 collector)
        "kind": "http",
    },
    "collector": {
        "cmd": [PY, "gq/auto_collector.py", "-i", "60"],   # 前台, 由守护托管 PID
        "match": "auto_collector",
        "kind": "db",
    },
}
# ==========================================================


# ---------- 日志 (守护自身, ASCII-only) ----------
def _setup_logger():
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, "daemon_guard.log")
    logger = logging.getLogger("daemon_guard")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=ROTATE_MAX_BYTES, backupCount=ROTATE_BACKUPS,
        encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    # 同时输出到控制台 (若被 Task Scheduler 捕获)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(sh)
    return logger


LOG = _setup_logger()


# ---------- 子进程日志轮转写入器 (防 1.2GB 膨胀) ----------
class RotatingLogWriter:
    """按大小轮转的二进制日志写入器. 直接写 bytes, 不做解码(防 GBK 崩溃)."""

    def __init__(self, base_path, max_bytes=ROTATE_MAX_BYTES, backups=ROTATE_BACKUPS):
        self.base = base_path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self._f = None
        self._size = 0
        self._open()

    def _open(self):
        self._f = open(self.base, "ab")
        self._size = os.path.getsize(self.base) if os.path.exists(self.base) else 0

    def write(self, data):
        if not data:
            return
        with self._lock:
            if self._f is None:
                self._open()
            self._f.write(data)
            self._size += len(data)
            if self._size >= self.max_bytes:
                self._rotate()

    def _rotate(self):
        try:
            self._f.close()
        except Exception:
            pass
        # base -> base.1 -> ... -> base.N (丢弃最旧)
        for i in range(self.backups - 1, -1, -1):
            src = self.base if i == 0 else ("%s.%d" % (self.base, i))
            dst = "%s.%d" % (self.base, i + 1)
            if os.path.exists(src):
                try:
                    if i + 1 >= self.backups:
                        os.remove(src)
                    else:
                        os.replace(src, dst)
                except Exception:
                    pass
        self._open()

    def close(self):
        with self._lock:
            if self._f:
                try:
                    self._f.close()
                except Exception:
                    pass
                self._f = None


def _pump_reader(stream, writer):
    """线程: 从子进程管道读取并写入轮转日志."""
    try:
        for chunk in iter(lambda: stream.read(4096), b""):
            writer.write(chunk)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


# ---------- 进程管理 ----------
def kill_by_keyword(keyword):
    """按命令行关键字精准杀进程树 (不影响其它托管进程)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match '%s' } | "
             "ForEach-Object { taskkill /PID $_.ProcessId /F /T 2>&1 }" % keyword],
            capture_output=True, timeout=60)
        text = ((out.stdout or b"") + (out.stderr or b"")).decode(
            "utf-8", errors="replace")
        if text.strip():
            LOG.warning("[kill] %s -> %s", keyword, text.strip()[:300])
    except Exception as e:
        LOG.error("[kill] exception for %s: %s", keyword, e)


def start_detached(name, spec):
    """Popen DETACHED 自举拉起托管进程, 返回 Popen (含轮转日志泵线程)."""
    if not os.path.exists(PY):
        LOG.error("[start] %s: .venv python not found: %s (skip)", name, PY)
        return None
    os.makedirs(LOG_DIR, exist_ok=True)
    out_log = os.path.join(LOG_DIR, "%s.out.log" % name)
    err_log = os.path.join(LOG_DIR, "%s.err.log" % name)
    out_w = RotatingLogWriter(out_log)
    err_w = RotatingLogWriter(err_log)
    DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        # 2026-08-28: SW_SHOWMINNOACTIVE(1) - 启动后窗口最小化且不抢焦点,
        # 解决"看门狗 cmd 弹出后不在置顶显示"问题; 日志走 RotatingLogWriter 不影响。
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 1  # SW_SHOWMINNOACTIVE
        p = subprocess.Popen(
            spec["cmd"], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=DETACHED, startupinfo=si)
    except Exception as e:
        LOG.error("[start] %s failed: %s", name, e)
        out_w.close(); err_w.close()
        return None
    t_out = threading.Thread(target=_pump_reader, args=(p.stdout, out_w), daemon=True)
    t_err = threading.Thread(target=_pump_reader, args=(p.stderr, err_w), daemon=True)
    t_out.start(); t_err.start()
    LOG.info("[start] %s launched pid=%d cmd=%s", name, p.pid, " ".join(spec["cmd"]))
    return p


# ---------- 健康检查 ----------
def health_http():
    try:
        r = urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT)
        return r.status == 200
    except Exception:
        return False


def health_db_fresh():
    """采集器存活: events.db (含 -wal/-shm) 在 COLLECTOR_MAX_MIN 分钟内被写过."""
    candidates = ["events.db", "events.db-wal", "events.db-shm"]
    found = None
    for c in candidates:
        p = os.path.join(ROOT, "data", c)
        if os.path.exists(p):
            found = p; break
    if not found:
        return False
    age_min = (time.time() - os.path.getmtime(found)) / 60.0
    return age_min <= COLLECTOR_MAX_MIN


# ---------- 守护状态 ----------
STATE = {name: {"proc": None, "fails": 0} for name in MANAGED}


def ensure_running(name):
    spec = MANAGED[name]
    st = STATE[name]
    proc = st["proc"]
    alive = proc is not None and proc.poll() is None

    healthy = True
    if not alive:
        LOG.warning("[check] %s: process down (restarting)", name)
        healthy = False
    else:
        if spec["kind"] == "http":
            ok = health_http()
            if ok:
                st["fails"] = 0
            else:
                st["fails"] += 1
                LOG.warning("[check] %s: health fail #%d/%d",
                            name, st["fails"], FREEZE_RETRIES)
                if st["fails"] >= FREEZE_RETRIES:
                    LOG.error("[check] %s: FROZEN (health timeout) -> restart", name)
                    healthy = False
        elif spec["kind"] == "db":
            if not health_db_fresh():
                LOG.warning("[check] %s: events.db stale -> restart", name)
                healthy = False

    if not healthy:
        # 精准清除该托管进程 (按关键字), 再自举
        kill_by_keyword(spec["match"])
        time.sleep(2)
        st["proc"] = start_detached(name, spec)
        st["fails"] = 0


def run_once():
    for name in MANAGED:
        ensure_running(name)


def main():
    LOG.info("[guard] starting; root=%s py=%s", ROOT, PY)
    if "--run-once" in sys.argv:
        run_once()
        LOG.info("[guard] --run-once done")
        return
    LOG.info("[guard] entering supervise loop (interval=%ds)", CHECK_INTERVAL)
    while True:
        try:
            run_once()
        except Exception as e:
            LOG.error("[guard] loop exception: %s", e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
