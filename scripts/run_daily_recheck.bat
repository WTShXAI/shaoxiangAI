@echo off
REM 哨响AI 每日复盘复核定时任务 (ShaoxiangAI_DailyRecheck)
REM 00:00 触发: 赛前KNN单结论 vs 赛后赛果 增量复核 + WS3 最强信号重算 + 赛前波胆验证
REM 2026-08-28: python.exe → pythonw.exe (计划任务静默运行, 不再弹 cmd 窗口)
cd /d D:\Architecture
"D:\Architecture\.venv\Scripts\pythonw.exe" "D:\Architecture\scripts\recheck_analysis.py" --apply >> "D:\Architecture\logs\daily_recheck.log" 2>&1
