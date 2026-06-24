param(
    [string]$ProjectRoot = "D:\Architecture v4.0"
)

function ExitWith($msg){ Write-Host $msg -ForegroundColor Red; exit 1 }

Write-Host "1) 检查 Python..."
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "Python 未发现。" -ForegroundColor Yellow
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "尝试通过 winget 安装 Python 3..."
        winget install --id=Python.Python.3 -e --accept-package-agreements --accept-source-agreements
        Write-Host "安装完成后请关闭并重新打开 PowerShell，然后重新运行本脚本。" -ForegroundColor Yellow
        exit 0
    } else {
        ExitWith "请手动从 https://python.org 安装 Python（建议 3.11/3.13），并勾选 'Add to PATH'，然后重启 shell 再运行此脚本。"
    }
}

Write-Host "`n2) 检查 NVIDIA 驱动/CUDA (nvidia-smi)..."
$nvsmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($nvsmi) {
    & nvidia-smi
} else {
    Write-Host "未找到 nvidia-smi — 若为无GPU或未装驱动，请确认。" -ForegroundColor Yellow
}

Write-Host "`n3) 创建并激活虚拟环境..."
$venvPath = Join-Path $ProjectRoot ".venv"
if (-not (Test-Path $ProjectRoot)) {
    Write-Host "项目目录不存在，创建: $ProjectRoot"
    New-Item -ItemType Directory -Path $ProjectRoot -Force | Out-Null
}
if (-not (Test-Path $venvPath)) {
    python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { ExitWith "创建 venv 失败" }
} else {
    Write-Host "已存在虚拟环境，跳过创建。"
}

$venvPy = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $venvPy)) { ExitWith "无法找到虚拟环境的 python: $venvPy" }

# 激活当前进程的 venv
& $venvPy -m pip install --upgrade pip
Write-Host "虚拟环境已准备： $venvPy"

Write-Host "`n4) 安装 PyTorch (CUDA 11.8) 与 HF 依赖..."
& $venvPy -m pip install --upgrade pip setuptools wheel -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --timeout 120
if ($LASTEXITCODE -ne 0) { ExitWith "升级 pip 失败" }

Write-Host "安装 torch/cu118（可能较大，耐心等待）..."
& $venvPy -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
if ($LASTEXITCODE -ne 0) { ExitWith "安装 torch 失败，请检查网络与 CUDA 兼容性" }

Write-Host "安装 Hugging Face 及其他依赖..."
& $venvPy -m pip install transformers accelerate tokenizers sentencepiece safetensors peft bitsandbytes -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --timeout 120
if ($LASTEXITCODE -ne 0) { ExitWith "安装 HF 依赖失败" }

Write-Host "`n5) 验证安装与 GPU..."
& $venvPy -c "import sys,torch; print('python', sys.version.split()[0]); print('torch', torch.__version__); print('cuda_available', torch.cuda.is_available()); print('cuda_version', torch.version.cuda)"
if ($LASTEXITCODE -ne 0) { Write-Host "上一步验证脚本执行出错（退出码 $LASTEXITCODE）" -ForegroundColor Yellow }

Write-Host "`n6) 完成：已安装依赖并检查 GPU。"
Write-Host "如需我生成 run_glm52_example.py 示例文件，请告知，我会单独创建。"
Write-Host "`n完成：请激活虚拟环境并运行示例："
Write-Host "PowerShell 激活：`n    & `"$venvPath\\Scripts\\Activate.ps1`""
Write-Host "运行示例：`n    python $samplePath"
Write-Host "`n若希望自动安装 CUDA 11.8 驱动/Toolkit，请使用 NVIDIA 官方安装器（脚本不自动改系统驱动）" -ForegroundColor Yellow
