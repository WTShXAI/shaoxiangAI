@echo off
REM ============================================
REM 哨响AI 每日维护一键脚本 (2026-09-10)
REM 结算 + HT修复 + minute_at增量修复 + 链路审计
REM 用法: scripts\run_daily_maintenance.bat
REM (可作为计划任务挂载; 本会话为计划任务子会话无法自建定时, 需要时
REM  在普通会话说"每天11点跑哨响维护"即可创建)
REM ============================================
setlocal
set PROJECT_ROOT=D:\Architecture
set PY=%PROJECT_ROOT%\.venv\Scripts\python.exe

echo [%time%] 1/4 每日结算...
cd /d %PROJECT_ROOT%
%PY% scripts\daily_settlement.py

echo [%time%] 2/4 HT 比分修复...
%PY% scripts\repair_ht_scores_wallclock.py --apply

echo [%time%] 3/4 minute_at 增量修复(完赛场)...
%PY% scripts\repair_minute_at_wallclock.py --apply --finished-only

echo [%time%] 4/4 滚球链路一致性审计...
%PY% scripts\audit_chain_consistency.py --limit 40

echo [%time%] 维护完成。
endlocal
