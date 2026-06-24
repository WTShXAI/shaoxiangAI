<#
.SYNOPSIS
  创建本地 Python 虚拟环境并自动安装依赖。
.DESCRIPTION
  本脚本会查找 Python 3.10+，创建 .venv 并安装 requirements.txt 中列出的依赖。
.PARAMETER Recreate
  若已存在 .venv，则删除后重新创建。
.PARAMETER SkipBackend
  若要跳过单独安装 backend/requirements.txt，请启用此选项。
#>
param(
    [switch]$Recreate,
    [switch]$SkipBackend
)

function Get-PythonPath {
    $candidates = @('python', 'python3', 'py')
    foreach ($name in $candidates) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }

        try {
            $versionText = & $cmd.Source --version 2>&1
        } catch {
            continue
        }

        if ($versionText -match 'Python\s+([0-9]+)\.([0-9]+)') {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 10)) {
                return $cmd.Source
            }
        }
    }
    return $null
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $repoRoot

Write-Host "[setup_env] Repository root: $repoRoot"

$pythonPath = Get-PythonPath
if (-not $pythonPath) {
    Write-Error "未找到 Python 3.10+ 可执行文件。请先安装 Python，并确保 'python' 或 'python3' 可用。"
    exit 1
}

Write-Host "[setup_env] Using Python: $pythonPath"

$venvPath = Join-Path $repoRoot '.venv'
if ($Recreate -and (Test-Path $venvPath)) {
    Write-Host "[setup_env] Recreating virtual environment..."
    Remove-Item -Recurse -Force $venvPath
}

if (-not (Test-Path $venvPath)) {
    Write-Host "[setup_env] Creating virtual environment at $venvPath"
    & $pythonPath -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        Write-Error "虚拟环境创建失败。请检查 Python 安装。"
        exit 1
    }
}

$venvPython = Join-Path $venvPath 'Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Error "无法找到虚拟环境中的 Python: $venvPython"
    exit 1
}

Write-Host "[setup_env] Upgrading pip, setuptools and wheel"
& $venvPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip 升级失败。"
    exit 1
}

$requirementsFile = $null
if (Test-Path (Join-Path $repoRoot 'requirements.txt')) {
    $requirementsFile = 'requirements.txt'
} elseif (Test-Path (Join-Path $repoRoot 'backend\requirements.txt')) {
    $requirementsFile = 'backend\requirements.txt'
}

if (-not $requirementsFile) {
    Write-Warning "未找到 requirements.txt 或 backend\requirements.txt。请手动检查依赖文件。"
} else {
    Write-Host "[setup_env] Installing dependencies from $requirementsFile"
    & $venvPython -m pip install -r $requirementsFile
    if ($LASTEXITCODE -ne 0) {
        Write-Error "依赖安装失败。请检查 $requirementsFile。"
        exit 1
    }
}

if (-not $SkipBackend -and (Test-Path (Join-Path $repoRoot 'backend\requirements.txt')) -and $requirementsFile -ne 'backend\requirements.txt') {
    Write-Host "[setup_env] Installing backend dependencies from backend\requirements.txt"
    & $venvPython -m pip install -r backend\requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Error "backend 依赖安装失败。"
        exit 1
    }
}

Write-Host "[setup_env] 完成。使用以下命令激活虚拟环境："
Write-Host "    .\.venv\Scripts\Activate.ps1"
Write-Host "然后可运行： python main.py backend --dev"