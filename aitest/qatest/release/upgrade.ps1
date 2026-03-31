$ErrorActionPreference = "Stop"

function Get-PythonCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Exe = "python"; Args = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @{ Exe = "py"; Args = @("-3") } }
    throw "未检测到 Python。请先安装 Python 3.12+，并确保命令行可用。"
}

function Ensure-Venv {
    param($Py)
    if (Test-Path ".\.venv\Scripts\python.exe") { return }
    Write-Host "未检测到 .venv，创建虚拟环境 ..."
    & $Py.Exe @($Py.Args) -m venv .venv
}

function Backup-File {
    param([string]$Path)
    if (!(Test-Path $Path)) { return }
    if (!(Test-Path ".\backup")) { New-Item -ItemType Directory -Path ".\backup" | Out-Null }
    $ts = Get-Date -Format "yyyyMMdd_HHmmss"
    $name = Split-Path -Leaf $Path
    Copy-Item $Path ".\backup\$name.$ts.bak" -Force
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

function Django-Upgrade {
    Write-Host "执行数据库迁移（同步新增字段）..."
    .\.venv\Scripts\python.exe manage.py migrate
    Write-Host "收集静态文件 ..."
    .\.venv\Scripts\python.exe manage.py collectstatic --noinput
}

Write-Host "=== QA Platform 升级脚本 ==="
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location ..

Backup-File ".\db.sqlite3"
Backup-File ".\.env"

$py = Get-PythonCommand
Ensure-Venv -Py $py
Pip-Install
Django-Upgrade

Write-Host ""
Write-Host "升级完成。"
Write-Host "如需启动服务："
Write-Host "  .\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8003"
