param(
    [string]$BundleRoot,
    [string]$ProjectRoot,
    [switch]$BrowsersOnly
)

$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Get-PythonExe {
    param([string]$ProjectRoot)

    $venvPy = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { return @{ Exe = $venvPy; Args = @() } }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Exe = "python"; Args = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @{ Exe = "py"; Args = @("-3") } }
    throw "python/py not found. Install Python 3.12+ and ensure it is in PATH."
}

function Get-PlaywrightVersionFromWheelhouse {
    param([string]$Wheelhouse)

    $f = Get-ChildItem -Path $Wheelhouse -File -Filter "playwright-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $f) { return $null }
    $m = [regex]::Match($f.Name, '^playwright-([0-9]+\.[0-9]+\.[0-9]+)[^\\]*\.whl$')
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}

if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).Path }
$ProjectRoot = (Resolve-Path $ProjectRoot).Path

if (-not $BundleRoot) { $BundleRoot = (Get-Location).Path }
$BundleRoot = (Resolve-Path $BundleRoot).Path

$wheelhouse = Join-Path $BundleRoot "wheelhouse"
$bundlePw = Join-Path $BundleRoot ".playwright"
if (!(Test-Path $wheelhouse)) { throw "Missing wheelhouse: $wheelhouse" }
if (!(Test-Path $bundlePw)) { throw "Missing .playwright: $bundlePw" }

$dstPw = Join-Path $ProjectRoot ".playwright"
Write-Host ("Copying browsers: " + $bundlePw + " -> " + $dstPw)
if (Test-Path $dstPw) {
    try { Remove-Item -Recurse -Force $dstPw } catch {}
}
Copy-Item -Recurse -Force $bundlePw $dstPw

$env:PLAYWRIGHT_BROWSERS_PATH = $dstPw

$py = Get-PythonExe -ProjectRoot $ProjectRoot

if (-not $BrowsersOnly) {
    $ver = Get-PlaywrightVersionFromWheelhouse -Wheelhouse $wheelhouse
    $importOk = $true
    try {
        & $py.Exe @($py.Args) -c "import playwright;print(getattr(playwright,'__version__',''))" | Out-Null
        if ($LASTEXITCODE -ne 0) { $importOk = $false }
    } catch { $importOk = $false }

    if (-not $importOk) {
        if ($ver) {
            Write-Host ("Installing playwright==" + $ver + " offline from wheelhouse ...")
            & $py.Exe @($py.Args) -m pip install --disable-pip-version-check --no-input --no-index --find-links $wheelhouse ("playwright==" + $ver)
        } else {
            Write-Host "Installing playwright offline from wheelhouse ..."
            & $py.Exe @($py.Args) -m pip install --disable-pip-version-check --no-input --no-index --find-links $wheelhouse playwright
        }
        if ($LASTEXITCODE -ne 0) { throw "pip install playwright failed." }
    } else {
        Write-Host "Playwright python package already installed, skip."
    }
}

Write-Host "Playwright self-check (launch chromium headless) ..."
& $py.Exe @($py.Args) -c "from playwright.sync_api import sync_playwright;p=sync_playwright().start();b=p.chromium.launch(headless=True);b.close();p.stop();print('ok')" | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Playwright self-check failed." }

Write-Host "Done."

