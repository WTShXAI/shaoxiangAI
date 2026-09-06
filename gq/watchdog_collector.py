"""GQ 采集器看门狗 — 每5分钟由计划任务(ShaoxiangGQ_Watchdog)调用, 挂了自动拉起

【状态 2026-08-27】主采集器已切换为 ws_collector.py (乐鱼 WS 实时推送流). 本看门狗现
监控/自愈 ws_collector 进程(进程名匹配 "ws_collector"); 拉起入口改走 start_collector.py
(其内部已改拉 ws_collector). auto_collector.py 仅作 HTTP 辅助库, 不再被本看门狗拉起.

检测三层:
  1. 进程层: auto_collector.py 进程是否存在 (psutil, 避免 shell 引号转义坑)
  2. 数据层: matches 表 MAX(last_seen) 是否距今 <= 15 分钟
             (进程在但卡死/断采也拉起; 弃用 odds_snapshots.MAX(captured_at) 全表扫 —— 7GB 大库会卡死)
  3. token 探针: 拉一次比赛列表, 有数据=token 有效; 空/异常=token 失效 → 明确告警
             (重启进程无法修复 token 失效, 必须更新 gq/.env 的 GQ_REQUEST_ID)

拉取: 先杀残留 auto_collector 进程 (防双实例) → start_collector.py (DETACHED)。
日志: gq/watchdog.log (只记录异常/重启/token失效, 健康时静默)。
"""
import subprocess, sys, os, sqlite3, time, datetime
from typing import Optional

try:
    import psutil
except ImportError:
    psutil = None

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
LOG = os.path.join(BASE, "watchdog.log")
DB = os.path.join(ROOT, "data", "events.db")
STALE_MAX_SEC = 15 * 60  # 数据 15 分钟未更新视为断采

def log(msg: str):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def collector_pids() -> list:
    """返回所有 auto_collector 相关进程 PID (含其启动链)"""
    pids = []
    if psutil is not None:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if p.info["name"] and p.info["name"].lower().startswith("python"):
                    cmd = " ".join(p.info["cmdline"] or [])
                    if "ws_collector" in cmd:
                        pids.append(p.info["pid"])
            except Exception:
                continue
    if pids:
        return pids
    # 兜底: 命令行探测 (不常用, 防止 psutil 列表不完整)
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\").CommandLine"],
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stderr=subprocess.DEVNULL)
        lines = out.decode("utf-8", "replace").splitlines()
        for i, ln in enumerate(lines):
            if "auto_collector" in ln:
                # CommandLine 与 ProcessId 不在同一行时无法精确配对, 用 tasklist 宽匹配
                pids.append(-1)
        if -1 in pids:
            pids = [-1]  # 标记"疑似存在, 走强杀"
    except Exception:
        pass
    return pids

def collector_alive() -> bool:
    return len(collector_pids()) > 0

def data_fresh() -> bool:
    """数据层检测: 采集器是否仍在写入 (matches.MAX(last_seen) 距今可接受)

    弃用 SELECT MAX(captured_at) FROM odds_snapshots: 7GB 大库全表扫会卡死看门狗自身。
    matches 表仅数千~万行, MAX(last_seen) 是稳定且极快的"采集器心跳"信号 —— 每轮
    collect_round 都会 upsert_match 刷新 last_seen, 故进程活着且 token 有效时该值持续推进。
    """
    try:
        c = sqlite3.connect(DB, timeout=30)
        c.execute("PRAGMA busy_timeout=30000")
        ts = c.execute("SELECT MAX(last_seen) FROM matches").fetchone()[0]
        c.close()
        if ts is None:
            log("DB 无任何比赛记录(matches 为空, 视为未初始化)")
            return False
        age = time.time() - ts
        if age > STALE_MAX_SEC:
            log(f"数据层断采: 最新采集距今 {age/60:.1f} 分钟 (> {STALE_MAX_SEC//60} 分钟)")
            return False
        return True
    except Exception as e:
        log(f"DB 读取失败(按断采处理): {e}")
        return False


def token_alive() -> Optional[bool]:
    """token 存活探针: 拉一次比赛列表, 有数据=token 有效, 空/异常=失效或未知。

    复用 auto_collector.fetch_match_list (懒导入; 导入或网络失败时返回 None=未知,
    绝不阻断看门狗核心的进程/数据检测)。token 失效时重启进程无效, 必须更新 gq/.env。
    """
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from gq.auto_collector import fetch_match_list
        items = fetch_match_list()
        return len(items) > 0
    except Exception as e:
        log(f"token 探针异常(按未知处理, 不告警): {e}")
        return None

def kill_residual():
    """杀掉残留的 ws_collector 进程, 防双实例"""
    killed = 0
    if psutil is not None:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if p.info["name"] and p.info["name"].lower().startswith("python"):
                    cmd = " ".join(p.info["cmdline"] or [])
                    if "ws_collector" in cmd:
                        p.kill()
                        killed += 1
            except Exception:
                continue
    if killed:
        time.sleep(2)  # 等进程退出
    return killed

def restart():
    """清残留 → 拉起采集器 (DETACHED)"""
    killed = kill_residual()
    if killed:
        log(f"已清理残留采集器 {killed} 个 (防双实例)")
    py = sys.executable
    launcher = os.path.join(BASE, "start_collector.py")
    try:
        proc = subprocess.Popen(
            [py, launcher],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                           if hasattr(subprocess, "DETACHED_PROCESS") else 0),
            cwd=BASE,
        )
        log(f"已拉起采集器 (start_collector pid={proc.pid})")
    except Exception as e:
        log(f"拉取失败: {e}")

def main():
    alive = collector_alive()
    fresh = data_fresh()
    tok = token_alive()  # None=未知(不告警); False=失效; True=有效
    # 健康判定: 进程在 + 数据新鲜 + token 未明确失效 → 静默退出
    if alive and fresh and (tok is None or tok):
        return
    if not alive:
        log("进程层死亡 — 触发拉取")
    if alive and not fresh:
        log("数据层断采(进程在但无新数据) — 触发拉取")
    if tok is False:
        log("[ALERT] 乐鱼 token(requestid) 失效 — 重启进程无效, 请更新 gq/.env 的 "
            "GQ_REQUEST_ID 为最新登录 URL 的 token= 值; 采集器每30s自动 pickup 新 token, 无需重启")
    restart()

if __name__ == "__main__":
    main()
