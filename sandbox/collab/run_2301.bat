@echo off
REM Shaoxiang dual-AI blackboard - formal replay run (ASCII-only for GBK console)
cd /d "%~dp0"
set LOG=D:\Architecture\logs\sandbox_collab_2301.log
set PY=D:\Architecture\.venv\Scripts\python.exe
echo ===== formal run start %date% %time% ===== > "%LOG%"
echo [1] clear journal >> "%LOG%"
if exist journal.jsonl del journal.jsonl
echo [2] S1-S3 e2e >> "%LOG%"
%PY% run_sandbox_test.py >> "%LOG%" 2>&1
echo [3] formal replay 22 real anomalies >> "%LOG%"
%PY% mock_observer.py --journal journal.jsonl >> "%LOG%" 2>&1
%PY% mock_executor.py --journal journal.jsonl >> "%LOG%" 2>&1
%PY% collab.py --journal journal.jsonl verify >> "%LOG%" 2>&1
echo [4] final stats >> "%LOG%"
%PY% collab.py --journal journal.jsonl stats >> "%LOG%" 2>&1
echo ===== done %date% %time% ===== >> "%LOG%"
