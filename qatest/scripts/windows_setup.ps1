$ErrorActionPreference = "Stop"

function Get-PythonCommand {
  if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Exe = "python"; Args = @() } }
  if (Get-Command py -ErrorAction SilentlyContinue) { return @{ Exe = "py"; Args = @("-3") } }
  throw "Python not found. Install Python 3.12+ and ensure it is in PATH."
}

$py = Get-PythonCommand

if (!(Test-Path ".venv")) {
  & $py.Exe @($py.Args) -m venv .venv
}

$pipArgs = @("--disable-pip-version-check", "--no-input")
if (-not $env:QA_ALLOW_PIP_CONFIG) { $pipArgs += "--isolated" }

if ($env:QA_UPGRADE_PIP) {
  try {
    .\.venv\Scripts\python.exe -m pip install @pipArgs -U pip
  } catch {
    Write-Host "Warning: pip upgrade failed, continue with current pip."
  }
} else {
  Write-Host "Skipping pip upgrade (set QA_UPGRADE_PIP=1 to enable)."
}

$wheelhouse = Join-Path (Get-Location) "wheelhouse"
if ((Test-Path $wheelhouse) -and (-not $env:QA_NO_WHEELHOUSE)) {
  .\.venv\Scripts\python.exe -m pip install @pipArgs --no-index --find-links $wheelhouse -r requirements.txt
} else {
  .\.venv\Scripts\python.exe -m pip install @pipArgs -r requirements.txt
}

if (-not $env:PLAYWRIGHT_BROWSERS_PATH) {
  $env:PLAYWRIGHT_BROWSERS_PATH = (Join-Path (Get-Location) ".playwright")
}
.\.venv\Scripts\python.exe -m playwright install chromium

Write-Host "Setup OK. Next: .\\.venv\\Scripts\\python.exe manage.py migrate"
