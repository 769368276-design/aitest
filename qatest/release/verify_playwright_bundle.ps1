param(
    [string]$ZipPath,
    [string]$Browser = "chromium",
    [switch]$LaunchCheck
)

$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Get-PythonExe {
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Exe = "python"; Args = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @{ Exe = "py"; Args = @("-3") } }
    throw "python/py not found. Install Python 3.12+ and ensure it is in PATH."
}

if (-not $ZipPath) { throw "ZipPath required" }
$zip = Resolve-Path $ZipPath

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
$tmp = Join-Path $projectRoot "dist\_pw_bundle_verify"

if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
New-Item -ItemType Directory -Path $tmp | Out-Null

Expand-Archive -Path $zip -DestinationPath $tmp
$bundleDir = $tmp
$wheelhouse = Join-Path $bundleDir "wheelhouse"
if (!(Test-Path $wheelhouse)) {
    $maybeBundle = Get-ChildItem -Path $tmp -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($maybeBundle) {
        $bundleDir = $maybeBundle.FullName
        $wheelhouse = Join-Path $bundleDir "wheelhouse"
    }
}
if (!(Test-Path $wheelhouse)) { throw "Missing wheelhouse in bundle" }

$pw = Join-Path $bundleDir ".playwright"
if (!(Test-Path $pw)) { throw "Missing .playwright in bundle" }
$browserDir = Get-ChildItem -Path $pw -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like ($Browser + "-*") } | Select-Object -First 1
if (-not $browserDir) { throw ("Missing " + $Browser + " browser dir in bundle") }

$py = Get-PythonExe
$venvDir = Join-Path $tmp "venv"
& $py.Exe @($py.Args) -m venv $venvDir
$venvPy = Join-Path $venvDir "Scripts\python.exe"
if (!(Test-Path $venvPy)) { throw "Temp venv failed" }

& $venvPy -m pip install --disable-pip-version-check --no-input --no-index --find-links $wheelhouse playwright
if ($LASTEXITCODE -ne 0) { throw "pip install playwright from wheelhouse failed" }

& $venvPy -c "import playwright;print('playwright_import_ok')"
if ($LASTEXITCODE -ne 0) { throw "playwright import failed" }

if ($LaunchCheck) {
    $env:PLAYWRIGHT_BROWSERS_PATH = $pw
    & $venvPy -c "from playwright.sync_api import sync_playwright;p=sync_playwright().start();b=p.chromium.launch(headless=True);b.close();p.stop();print('launch_ok')" | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "playwright launch failed with bundled browsers" }
}

Write-Host "OK: wheelhouse offline install + import + browsers present"
