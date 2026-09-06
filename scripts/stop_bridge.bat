@echo off
setlocal EnableDelayedExpansion
rem ===========================================================================
rem  stop_bridge.bat  --  bridge_service "shim + worker" 双杀停机脚本 (Windows)
rem  归属: 哨响AI 稳定化下一代足球系统 / REQ-01 停机铁律 (system_design.md 1.1 事故①-5)
rem ---------------------------------------------------------------------------
rem  ★★★ WHY 必须"双杀" ★★★
rem  bridge_service 的启动链路是两级进程:
rem
rem      D:\Architecture\.venv\Scripts\python.exe        <-- (1) shim 垫片
rem                |  re-dispatch (venv launcher 转发)
rem                v
rem      %LOCALAPPDATA%\Programs\Python\Python312\python.exe  <-- (2) 真正的 worker
rem                                                               (uvicorn 监听 :9000)
rem
rem  只杀 worker(2) 会留下 shim(1) 空转:
rem    - shim 仍持有 GQ.db 的 sqlite 文件锁 / -wal 句柄 -> 下次启动 "database is locked";
rem    - shim 仍占着 stdout 管道与 .pid 文件 -> 监控误判"服务在跑";
rem    - 部分守护配置下 shim 会把 worker 再拉起来 -> "杀不死"。
rem  只杀 shim(1) 会留下 worker(2) 继续监听 :9000 -> 端口占用, 新实例起不来。
rem  => 必须 shim 先杀(切断 re-dispatch/重启路径), 再杀 worker, 最后按端口兜底。
rem ---------------------------------------------------------------------------
rem  用法:
rem    scripts\stop_bridge.bat              精确停机(推荐): 按 shim 路径 + 命令行 + 端口
rem    scripts\stop_bridge.bat --all        追加 taskkill /F /IM python.exe (核弹级,
rem                                         会连带杀掉本机所有 python 进程, 慎用!)
rem    scripts\stop_bridge.bat --dry-run    只打印将要杀的 PID, 不真杀
rem  退出码: 0 = 端口已释放; 1 = 仍有残留进程/端口未释放
rem ===========================================================================

set "PROJECT_ROOT=%~dp0.."
pushd "%PROJECT_ROOT%" >nul 2>&1
set "PROJECT_ROOT=%CD%"
popd >nul 2>&1

set "BRIDGE_PORT=9000"
set "HEALTH_PORT=9001"
set "SHIM_PY=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "MATCH=bridge_service"
set "DRYRUN=0"
set "KILL_ALL=0"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--dry-run" set "DRYRUN=1"
if /i "%~1"=="--all"     set "KILL_ALL=1"
if /i "%~1"=="--help"    goto usage
if /i "%~1"=="-h"        goto usage
shift
goto parse_args

:usage
echo Usage: stop_bridge.bat [--dry-run] [--all]
echo   --dry-run   only print PIDs, do not kill
echo   --all       also run: taskkill /F /IM python.exe  (DANGEROUS, kills every python)
exit /b 0

:args_done
echo [stop_bridge] project root : %PROJECT_ROOT%
echo [stop_bridge] shim python  : %SHIM_PY%
echo [stop_bridge] ports        : %BRIDGE_PORT% (bridge), %HEALTH_PORT% (health)
if "%DRYRUN%"=="1" echo [stop_bridge] *** DRY RUN - nothing will be killed ***
echo.

rem ---------------------------------------------------------------------------
rem  STEP 1/4: 杀 shim 垫片 (.venv\Scripts\python.exe)
rem  先杀 shim, 切断 re-dispatch / 自动重启路径, 否则 worker 被杀后可能立刻重生。
rem ---------------------------------------------------------------------------
echo [1/4] killing .venv shim processes ...
set "SHIM_FOUND=0"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Get-CimInstance Win32_Process -ErrorAction Stop ^| Where-Object { $_.ExecutablePath -and $_.ExecutablePath -ieq '%SHIM_PY%' } ^| ForEach-Object { $_.ProcessId } } catch { }"`) do (
    set "SHIM_FOUND=1"
    call :kill_pid %%P "venv-shim"
)
if "!SHIM_FOUND!"=="0" echo       - no .venv shim process found

rem ---------------------------------------------------------------------------
rem  STEP 2/4: 杀系统 Python worker (命令行含 bridge_service)
rem  覆盖 Python312 / 任意解释器, 只要命令行里出现 bridge_service 就是我们的 worker。
rem ---------------------------------------------------------------------------
echo [2/4] killing python workers whose command line contains "%MATCH%" ...
set "WORKER_FOUND=0"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Get-CimInstance Win32_Process -ErrorAction Stop ^| Where-Object { $_.CommandLine -and $_.CommandLine -match '%MATCH%' -and $_.Name -match '^(python^|pythonw)' } ^| ForEach-Object { $_.ProcessId } } catch { }"`) do (
    set "WORKER_FOUND=1"
    call :kill_pid %%P "bridge-worker"
)
if "!WORKER_FOUND!"=="0" echo       - no bridge_service worker found

rem ---------------------------------------------------------------------------
rem  STEP 3/4: 按端口兜底 (改过启动方式 / 命令行匹配不到时的最后一道)
rem ---------------------------------------------------------------------------
echo [3/4] killing leftovers still LISTENING on %BRIDGE_PORT% / %HEALTH_PORT% ...
call :kill_by_port %BRIDGE_PORT%
call :kill_by_port %HEALTH_PORT%

rem ---------------------------------------------------------------------------
rem  STEP 3.5 (可选): 核弹级 taskkill /F /IM
rem  ⚠ 会杀掉本机**所有** python 进程 (含采集器/训练/其它项目), 仅在确认无副作用时用。
rem ---------------------------------------------------------------------------
if "%KILL_ALL%"=="1" (
    echo       [--all] taskkill /F /IM python.exe  ^(DANGEROUS^)
    if "%DRYRUN%"=="0" (
        taskkill /F /IM python.exe  >nul 2>&1
        taskkill /F /IM pythonw.exe >nul 2>&1
    )
)

rem ---------------------------------------------------------------------------
rem  STEP 4/4: 清理 pid 文件 + 校验端口是否真的释放
rem ---------------------------------------------------------------------------
echo [4/4] cleanup and verify ...
if "%DRYRUN%"=="0" (
    if exist "%PROJECT_ROOT%\bridge.pid" del /f /q "%PROJECT_ROOT%\bridge.pid" >nul 2>&1
)

set "STILL=0"
for /f "tokens=5" %%A in ('netstat -ano ^| findstr /r /c:":%BRIDGE_PORT% .*LISTENING" 2^>nul') do set "STILL=1"
for /f "tokens=5" %%A in ('netstat -ano ^| findstr /r /c:":%HEALTH_PORT% .*LISTENING" 2^>nul') do set "STILL=1"

if "!STILL!"=="1" (
    echo.
    echo [stop_bridge] RESULT: FAIL - port %BRIDGE_PORT%/%HEALTH_PORT% still LISTENING.
    echo               retry with:  scripts\stop_bridge.bat --all
    exit /b 1
)
echo.
echo [stop_bridge] RESULT: OK - shim + worker stopped, ports released.
exit /b 0

rem ===========================================================================
rem  subroutines
rem ===========================================================================
:kill_pid
rem  %1 = pid, %2 = label
set "_PID=%~1"
set "_LABEL=%~2"
if "%_PID%"=="" goto :eof
if "%_PID%"=="0" goto :eof
echo       - PID %_PID% (%_LABEL%)
if "%DRYRUN%"=="0" taskkill /F /T /PID %_PID% >nul 2>&1
goto :eof

:kill_by_port
rem  %1 = port
set "_PORT=%~1"
set "_HIT=0"
for /f "tokens=5" %%A in ('netstat -ano ^| findstr /r /c:":%_PORT% .*LISTENING" 2^>nul') do (
    set "_HIT=1"
    call :kill_pid %%A "port-%_PORT%"
)
if "!_HIT!"=="0" echo       - nothing listening on %_PORT%
goto :eof
