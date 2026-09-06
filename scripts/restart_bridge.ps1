# 哨响AI bridge_service 重启 (PowerShell)
# 使用: .\scripts\restart_bridge.ps1
$ErrorActionPreference = "Stop"
$root = "D:\Architecture"

Write-Host "[哨响AI] 关闭旧进程..." -ForegroundColor Cyan
Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -like "*bridge*" -or 
    (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -like "*bridge_service*"
} | Stop-Process -Force

# 2. 等端口释放
Write-Host "[哨响AI] 等端口 9000 释放..." -ForegroundColor Cyan
while ($true) {
    Start-Sleep 2
    $port = Get-NetTCPConnection -LocalPort 9000 -ErrorAction SilentlyContinue | Where-Object {$_.State -eq 'Listen'}
    if (-not $port) { break }
    Write-Host "  端口仍被 PID $($port.OwningProcess) 占用..." -ForegroundColor Yellow
}

# 3. 启动
Write-Host "[哨响AI] 启动 bridge_service ..." -ForegroundColor Cyan
$proc = Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
    -ArgumentList "bridge_service.py" `
    -WorkingDirectory $root `
    -WindowStyle Minimized `
    -PassThru

# 4. 健康检查
Write-Host "[哨响AI] 等就绪..." -ForegroundColor Cyan
do {
    Start-Sleep 3
    try { $ok = (Invoke-WebRequest -Uri "http://localhost:9000/health" -TimeoutSec 5 -UseBasicParsing).Content -like "*ok*" } catch { $ok = $false }
} while (-not $ok)
Write-Host "[哨响AI] Bridge 已就绪. PID=$($proc.Id)" -ForegroundColor Green
