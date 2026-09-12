@echo off
REM 哨响双AI黑板协作 - 23:01 正式执行轮 (干净日志)
set HERE=%~dp0
set LOG=D:\Architecture\logs\sandbox_collab_2301.log
echo ===== 哨响黑板协作 23:01 正式轮 开始 ===== > %LOG%
echo [1] 清空沙箱黑板(正式轮从干净状态开始) >> %LOG%
if exist "%HERE%journal.jsonl" del "%HERE%journal.jsonl"
echo [2] S1-S3 端到端测试 >> %LOG%
cd /d %HERE%
"D:\Architecture\.venv\Scripts\python.exe" run_sandbox_test.py >> %LOG% 2>&1
echo [3] 正式回放轮: 22条真实异常 全自动处理 >> %LOG%
"D:\Architecture\.venv\Scripts\python.exe" mock_observer.py --journal "%HERE%journal.jsonl" >> %LOG% 2>&1
"D:\Architecture\.venv\Scripts\python.exe" mock_executor.py --journal "%HERE%journal.jsonl" >> %LOG% 2>&1
"D:\Architecture\.venv\Scripts\python.exe" collab.py --journal "%HERE%journal.jsonl" verify >> %LOG% 2>&1
echo [4] 黑板终态 >> %LOG%
"D:\Architecture\.venv\Scripts\python.exe" collab.py --journal "%HERE%journal.jsonl" stats >> %LOG% 2>&1
echo ===== 完成 ===== >> %LOG%
