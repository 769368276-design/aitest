$ErrorActionPreference = "Stop"

function Say([string]$msg) { Write-Host ("[qatest] " + $msg) }

function Get-PythonCommand {
  if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Exe = "python"; Args = @() } }
  if (Get-Command py -ErrorAction SilentlyContinue) { return @{ Exe = "py"; Args = @("-3") } }
  throw "Python not found"
}

Set-Location -Path (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -Path (Resolve-Path "..")

$port = if ($env:QA_PORT) { [int]$env:QA_PORT } else { 8003 }
$installBrowser = if ($env:QA_SKIP_PLAYWRIGHT) { $false } else { $true }
$installOnly = if ($env:QA_INSTALL_ONLY) { $true } else { $false }
$wheelhouse = Join-Path (Get-Location) "wheelhouse"
$useWheelhouse = (Test-Path $wheelhouse) -and (-not $env:QA_NO_WHEELHOUSE)
$pipArgs = @("--disable-pip-version-check", "--no-input")
if (-not $env:QA_ALLOW_PIP_CONFIG) { $pipArgs += "--isolated" }

Say "Project dir: $(Get-Location)"

try {
  $py = Get-PythonCommand
  $pyv = & $py.Exe @($py.Args) --version 2>$null
  if (-not $pyv) { throw "python not found" }
  Say "Python: $pyv"
} catch {
  throw "Python not found. Install Python 3.12+ and ensure it is in PATH."
}

if (!(Test-Path ".venv")) {
  Say "Creating venv .venv ..."
  & $py.Exe @($py.Args) -m venv .venv
}

if (!(Test-Path ".venv\Scripts\python.exe")) {
  throw "Venv failed: missing .venv\\Scripts\\python.exe"
}

if ($env:QA_UPGRADE_PIP) {
  try {
    Say "Upgrading pip ..."
    .\.venv\Scripts\python.exe -m pip install @pipArgs -U pip
  } catch {
    Say "Warning: pip upgrade failed, continue with current pip."
  }
} else {
  Say "Skipping pip upgrade (set QA_UPGRADE_PIP=1 to enable)."
}

if ($useWheelhouse) {
  Say "Installing requirements.txt from wheelhouse ..."
  .\.venv\Scripts\python.exe -m pip install @pipArgs --no-index --find-links $wheelhouse -r requirements.txt
} else {
  Say "Installing requirements.txt from network ..."
  .\.venv\Scripts\python.exe -m pip install @pipArgs -r requirements.txt
}

$envTemplate = $null
if (Test-Path ".env.local.example") { $envTemplate = ".env.local.example" }
elseif (Test-Path ".env.example") { $envTemplate = ".env.example" }
if (!(Test-Path ".env") -and $envTemplate) {
  Say "No .env found. Copying $envTemplate -> .env (fill your AI keys later if needed)."
  Copy-Item $envTemplate ".env"
}

if ($installBrowser) {
  if (-not $env:PLAYWRIGHT_BROWSERS_PATH) {
    $env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) ".playwright")
  }
  $pwPath = $env:PLAYWRIGHT_BROWSERS_PATH
  $hasChromium = $false
  try {
    if (Test-Path $pwPath) {
      $hasChromium = (Get-ChildItem -Path $pwPath -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue | Select-Object -First 1) -ne $null
    }
  } catch { $hasChromium = $false }
  if ($hasChromium) {
    Say "Playwright browsers already present in: $pwPath (skip download)"
  } else {
    Say "Installing Playwright browser (chromium) ..."
    .\.venv\Scripts\python.exe -m playwright install chromium
  }
  Say "Playwright self-check (launch chromium headless) ..."
  try {
    .\.venv\Scripts\python.exe -c "from playwright.sync_api import sync_playwright;p=sync_playwright().start();b=p.chromium.launch(headless=True);b.close();p.stop();print('ok')" | Out-Host
  } catch {
    Say "Playwright self-check failed."
    Say "Try these steps:"
    Say "1) Re-install browser: .\\.venv\\Scripts\\python.exe -m playwright install chromium"
    Say "2) Ensure Windows security/AV is not blocking ms-playwright cache"
    throw
  }
} else {
  Say "Skipping Playwright install (QA_SKIP_PLAYWRIGHT=1). AI Test requires browser and will fail."
}

Say "Running migrate ..."
.\.venv\Scripts\python.exe manage.py migrate

if (-not $env:INIT_ADMIN_PASSWORD) {
  $chars = (48..57 + 65..90 + 97..122)
  $env:INIT_ADMIN_PASSWORD = -join ($chars | Get-Random -Count 16 | ForEach-Object { [char]$_ })
  Say "Generated INIT_ADMIN_PASSWORD=$env:INIT_ADMIN_PASSWORD"
}

Say "Running init_data ..."
.\.venv\Scripts\python.exe manage.py init_data

Say "Starting server: http://localhost:$port/"
Say "Change port: set QA_PORT, e.g. `$env:QA_PORT=8010"
Say "Install only: set QA_INSTALL_ONLY=1"
Say "After login: set AI keys in Personal Center (AI Generate/AI Test require keys)."
Say "Chrome 录制插件(QA Recorder)目录: .\\qa_recorder_extension (安装方式见《常见问题处理方案.md》第6节)"

if ($installOnly) {
  Say "QA_INSTALL_ONLY=1, install+init only, not starting server."
  Say "Manual start: .\\.venv\\Scripts\\python.exe manage.py runserver 0.0.0.0:$port"
  exit 0
}

.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:$port
