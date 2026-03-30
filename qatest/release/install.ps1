$ErrorActionPreference = "Stop"

function Get-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) { return @("py", "-3") }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @("python") }
    throw "未检测到 Python。请先安装 Python 3.12+，并确保命令行可用。"
}

function Ensure-EnvFile {
    if (Test-Path ".\.env") { return }
    if (Test-Path ".\.env.local.example") {
        Copy-Item ".\.env.local.example" ".\.env" -Force
        Write-Host "已创建 .env（从 .env.local.example 复制）。请根据实际情况填写 AI Key 等配置。"
        return
    }
    if (Test-Path ".\.env.example") {
        Copy-Item ".\.env.example" ".\.env" -Force
        Write-Host "已创建 .env（从 .env.example 复制）。请根据实际情况填写 AI Key 等配置。"
        return
    }
    New-Item -ItemType File -Path ".\.env" -Force | Out-Null
    Write-Host "已创建空的 .env。请根据实际情况填写配置。"
}

function Ensure-Venv {
    param([string[]]$Py)
    if (Test-Path ".\.venv\Scripts\python.exe") { return }
    Write-Host "创建虚拟环境 .venv ..."
    & $Py[0] @($Py[1..($Py.Length-1)]) -m venv .venv
}

function Pip-Install {
    if (!(Test-Path ".\requirements.txt")) { throw "缺少 requirements.txt" }
    Write-Host "安装/更新依赖 ..."
    $pipArgs = @("--disable-pip-version-check", "--no-input")
    if (-not $env:QA_ALLOW_PIP_CONFIG) { $pipArgs += "--isolated" }
    if ($env:QA_UPGRADE_PIP) {
        try {
            .\.venv\Scripts\python.exe -m pip install @pipArgs --upgrade pip
        } catch {
            Write-Host "Warning: pip upgrade failed, continue with current pip."
        }
    } else {
        Write-Host "Skipping pip upgrade (set QA_UPGRADE_PIP=1 to enable)."
    }
    if ((Test-Path ".\wheelhouse") -and (-not $env:QA_NO_WHEELHOUSE)) {
        .\.venv\Scripts\python.exe -m pip install @pipArgs --no-index --find-links ".\wheelhouse" -r .\requirements.txt
    } else {
        .\.venv\Scripts\python.exe -m pip install @pipArgs -r .\requirements.txt
    }
}

function Django-Setup {
    Write-Host "执行数据库迁移 ..."
    .\.venv\Scripts\python.exe manage.py migrate
    Write-Host "收集静态文件 ..."
    .\.venv\Scripts\python.exe manage.py collectstatic --noinput
}

Write-Host "=== QA Platform 新装脚本 ==="
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location ..

$py = Get-PythonCommand
Ensure-EnvFile
Ensure-Venv -Py $py
Pip-Install
Django-Setup

Write-Host ""
Write-Host "安装完成。"
Write-Host "如需启动服务："
Write-Host "  .\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8003"
