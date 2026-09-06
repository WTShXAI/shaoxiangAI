#!/usr/bin/env bash
# =============================================================================
#  stop_bridge.sh  --  bridge_service "shim + worker" 双杀停机脚本
#  归属: 哨响AI 稳定化下一代足球系统 / REQ-01 停机铁律 (system_design.md 1.1 事故①-5)
#  适用: Git Bash / WSL / Linux / macOS (Windows 原生 cmd 请用 stop_bridge.bat)
# -----------------------------------------------------------------------------
#  ★★★ WHY 必须"双杀" ★★★
#  bridge_service 的启动链路是两级进程:
#
#      <root>/.venv/Scripts/python.exe (或 .venv/bin/python)   <-- (1) shim 垫片
#            |  re-dispatch (venv launcher 转发到 base interpreter)
#            v
#      系统 Python312 python.exe                                <-- (2) 真正的 worker
#                                                                   (uvicorn 监听 :9000)
#
#  只杀 worker(2):  shim(1) 空转 -> 仍持 GQ.db 文件锁/-wal 句柄 -> 下次启动
#                   "database is locked"; 且守护配置下会把 worker 再拉起来 -> 杀不死。
#  只杀 shim(1):    worker(2) 继续监听 :9000 -> 端口占用, 新实例起不来。
#  => 顺序固定: 先 shim (切断 re-dispatch/重启路径), 再 worker, 最后按端口兜底。
# -----------------------------------------------------------------------------
#  用法:
#    scripts/stop_bridge.sh              精确停机(推荐)
#    scripts/stop_bridge.sh --dry-run    只打印将要杀的 PID
#    scripts/stop_bridge.sh --all        追加 pkill -f python (核弹级, 慎用)
#  退出码: 0 = 端口已释放; 1 = 仍有残留
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BRIDGE_PORT="${BRIDGE_PORT:-9000}"
HEALTH_PORT="${HEALTH_PORT:-9001}"
MATCH="bridge_service"
DRYRUN=0
KILL_ALL=0
GRACE_SECONDS="${GRACE_SECONDS:-3}"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRYRUN=1 ;;
    --all)     KILL_ALL=1 ;;
    -h|--help)
      echo "Usage: stop_bridge.sh [--dry-run] [--all]"
      echo "  --dry-run   only print PIDs, do not kill"
      echo "  --all       also run: pkill -9 -f python  (DANGEROUS)"
      exit 0
      ;;
    *) echo "[stop_bridge] unknown arg: $arg (ignored)" ;;
  esac
done

echo "[stop_bridge] project root : ${PROJECT_ROOT}"
echo "[stop_bridge] ports        : ${BRIDGE_PORT} (bridge), ${HEALTH_PORT} (health)"
[ "${DRYRUN}" -eq 1 ] && echo "[stop_bridge] *** DRY RUN - nothing will be killed ***"
echo

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

IS_WINDOWS=0
case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
esac

# win_pids_by_pattern <pattern>
# ⚠ Git Bash 的 pgrep 只能看到 MSYS 自己的进程, **看不到原生 Win32 进程**
#    (bridge_service 的 shim/worker 都是原生进程)。因此在 Windows 上必须借
#    PowerShell 的 Win32_Process 来找 PID, 否则 shim 永远"匹配不到"而漏杀。
win_pids_by_pattern() {
  local pattern="$1"
  command -v powershell >/dev/null 2>&1 || return 0
  # 把 /d/Architecture 风格路径转回 D:\Architecture 风格再匹配
  local win_pattern
  win_pattern="$(printf '%s' "${pattern}" | sed -E 's#^/([a-zA-Z])/#\1:/#')"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "
    \$p = '${win_pattern}'.Replace('/','\\')
    try {
      Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object {
          (\$_.ExecutablePath -and \$_.ExecutablePath -like \"*\$p*\") -or
          (\$_.CommandLine    -and \$_.CommandLine    -like \"*\$p*\")
        } | ForEach-Object { \$_.ProcessId }
    } catch { }
  " 2>/dev/null | tr -d '\r' | tr '\n' ' '
}

# kill_pattern <label> <pkill-pattern>
# 先 SIGTERM 给一次优雅退出的机会 (让 finally 里的 conn.close()/checkpoint 跑完),
# 等 GRACE_SECONDS 后对残留发 SIGKILL —— 直接 -9 会留下 -wal/-shm 脏尾巴。
kill_pattern() {
  local label="$1" pattern="$2" pids
  pids="$(pgrep -f "${pattern}" 2>/dev/null | tr '\n' ' ' || true)"
  if [ -z "${pids// /}" ] && [ "${IS_WINDOWS}" -eq 1 ]; then
    pids="$(win_pids_by_pattern "${pattern}")"
    [ -n "${pids// /}" ] && label="${label}/win32"
  fi
  # 排除自身与父 shell, 免得脚本把自己杀了
  local filtered=""
  for p in ${pids}; do
    [ "${p}" = "$$" ] && continue
    [ "${p}" = "${PPID}" ] && continue
    filtered="${filtered}${p} "
  done
  if [ -z "${filtered// /}" ]; then
    echo "      - no process matched (${label})"
    return 0
  fi
  echo "      - ${label}: PIDs ${filtered}"
  if [ "${DRYRUN}" -eq 1 ]; then
    return 0
  fi
  for p in ${filtered}; do kill_one "${p}"; done
  sleep "${GRACE_SECONDS}"
  for p in ${filtered}; do
    if kill -0 "${p}" 2>/dev/null; then
      echo "        (still alive, force kill) PID ${p}"
      force_kill_one "${p}"
    fi
  done
}

# kill_one <pid> —— 优雅终止 (Windows 上 kill 对原生进程无效, 退回 taskkill)
kill_one() {
  local pid="$1"
  kill -TERM "${pid}" 2>/dev/null && return 0
  if [ "${IS_WINDOWS}" -eq 1 ] && command -v taskkill >/dev/null 2>&1; then
    taskkill //PID "${pid}" //T >/dev/null 2>&1 || taskkill /PID "${pid}" /T >/dev/null 2>&1 || true
  fi
}

# force_kill_one <pid> —— 强杀
force_kill_one() {
  local pid="$1"
  kill -9 "${pid}" 2>/dev/null && return 0
  if [ "${IS_WINDOWS}" -eq 1 ] && command -v taskkill >/dev/null 2>&1; then
    taskkill //F //PID "${pid}" //T >/dev/null 2>&1 || taskkill /F /PID "${pid}" /T >/dev/null 2>&1 || true
  fi
}

# pids_on_port <port> —— 依次尝试 lsof / ss / netstat, 兼容 Git Bash 与 Linux
pids_on_port() {
  local port="$1" out=""
  if command -v lsof >/dev/null 2>&1; then
    out="$(lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null || true)"
  fi
  if [ -z "${out}" ] && command -v ss >/dev/null 2>&1; then
    out="$(ss -lptn "sport = :${port}" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 || true)"
  fi
  if [ -z "${out}" ] && command -v netstat >/dev/null 2>&1; then
    # Git Bash on Windows: netstat -ano 的最后一列是 PID
    out="$(netstat -ano 2>/dev/null | grep -E "[:.]${port}[[:space:]]" | grep -i LISTEN \
           | awk '{print $NF}' | grep -E '^[0-9]+$' || true)"
  fi
  echo "${out}" | tr '\n' ' '
}

kill_by_port() {
  local port="$1" pids
  pids="$(pids_on_port "${port}")"
  if [ -z "${pids// /}" ]; then
    echo "      - nothing listening on ${port}"
    return 0
  fi
  echo "      - port ${port}: PIDs ${pids}"
  [ "${DRYRUN}" -eq 1 ] && return 0
  for p in ${pids}; do kill_one "${p}"; done
  sleep "${GRACE_SECONDS}"
  for p in ${pids}; do
    if kill -0 "${p}" 2>/dev/null; then force_kill_one "${p}"; fi
  done
}

# ---------------------------------------------------------------------------
# STEP 1/4: shim 垫片 (.venv 下的 python) —— 必须最先杀
# ---------------------------------------------------------------------------
echo "[1/4] killing .venv shim processes ..."
kill_pattern "venv-shim(Scripts)" "${PROJECT_ROOT}/.venv/Scripts/python"
kill_pattern "venv-shim(bin)"     "${PROJECT_ROOT}/.venv/bin/python"

# ---------------------------------------------------------------------------
# STEP 2/4: 系统 Python worker (命令行含 bridge_service)
# ---------------------------------------------------------------------------
echo "[2/4] killing python workers whose command line contains '${MATCH}' ..."
kill_pattern "bridge-worker" "${MATCH}"

# ---------------------------------------------------------------------------
# STEP 3/4: 按端口兜底
# ---------------------------------------------------------------------------
echo "[3/4] killing leftovers still LISTENING on ${BRIDGE_PORT} / ${HEALTH_PORT} ..."
kill_by_port "${BRIDGE_PORT}"
kill_by_port "${HEALTH_PORT}"

if [ "${KILL_ALL}" -eq 1 ]; then
  echo "      [--all] pkill -9 -f python   (DANGEROUS: kills every python process)"
  if [ "${DRYRUN}" -eq 0 ]; then
    pkill -9 -f python 2>/dev/null || true
  fi
fi

# ---------------------------------------------------------------------------
# STEP 4/4: 清理 pid 文件 + 校验
# ---------------------------------------------------------------------------
echo "[4/4] cleanup and verify ..."
if [ "${DRYRUN}" -eq 0 ] && [ -f "${PROJECT_ROOT}/bridge.pid" ]; then
  rm -f "${PROJECT_ROOT}/bridge.pid" || true
fi

remaining=""
for port in "${BRIDGE_PORT}" "${HEALTH_PORT}"; do
  left="$(pids_on_port "${port}")"
  [ -n "${left// /}" ] && remaining="${remaining}${port}:${left} "
done

echo
if [ "${DRYRUN}" -eq 1 ]; then
  # dry-run 下什么都没杀, 端口当然还占着 —— 不能报 FAIL 误导运维
  echo "[stop_bridge] RESULT: DRY-RUN (nothing killed). would-be leftovers: ${remaining:-none}"
  exit 0
fi
if [ -n "${remaining// /}" ]; then
  echo "[stop_bridge] RESULT: FAIL - still listening -> ${remaining}"
  echo "              retry with:  scripts/stop_bridge.sh --all"
  exit 1
fi
echo "[stop_bridge] RESULT: OK - shim + worker stopped, ports released."
exit 0
