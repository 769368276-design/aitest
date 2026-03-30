param(
    [string]$OutDir,
    [string]$PlaywrightVersion,
    [string]$Browser = "chromium",
    [switch]$SkipWheelhouse,
    [switch]$SkipBrowserDownload,
    [string]$BrowsersSourcePath,
    [string]$PlaywrightDownloadHost
)

$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Get-PythonExe {
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Exe = "python"; Args = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @{ Exe = "py"; Args = @("-3") } }
    throw "python/py not found. Install Python 3.12+ and ensure it is in PATH."
}

function Get-DetectedPlaywrightVersion {
    param([string]$ProjectRoot)

    $candidates = @(
        (Join-Path $ProjectRoot "_pkg_smoketest\wheelhouse\playwright-*.whl"),
        (Join-Path $ProjectRoot "_pkg_smoketest\_pkg_smoketest\wheelhouse\playwright-*.whl")
    )
    foreach ($p in $candidates) {
        $f = Get-ChildItem -Path $p -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) {
            $m = [regex]::Match($f.Name, '^playwright-([0-9]+\.[0-9]+\.[0-9]+)[^\\]*\.whl$')
            if ($m.Success) { return $m.Groups[1].Value }
        }
    }
    return $null
}

function New-TempVenv {
    param([hashtable]$Py)

    $venvDir = Join-Path $env:TEMP ("qatest_pwbundle_venv_" + [Guid]::NewGuid().ToString("N"))
    & $Py.Exe @($Py.Args) -m venv $venvDir
    $venvPy = Join-Path $venvDir "Scripts\python.exe"
    if (!(Test-Path $venvPy)) { throw "Temp venv failed: missing $venvPy" }
    return @{ Dir = $venvDir; Py = $venvPy }
}

function Test-HasPlaywrightBrowser {
    param(
        [string]$BrowsersPath,
        [string]$Browser
    )
    try {
        if (!(Test-Path $BrowsersPath)) { return $false }
        $d = Get-ChildItem -Path $BrowsersPath -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like ($Browser + "-*") } | Select-Object -First 1
        return $d -ne $null
    } catch {
        return $false
    }
}

function Ensure-PlaywrightBrowserDownload {
    param(
        [string]$VenvPython,
        [string]$BrowsersPath,
        [string]$Browser,
        [string]$PreferredHost
    )

    if (Test-HasPlaywrightBrowser -BrowsersPath $BrowsersPath -Browser $Browser) { return }

    if (Test-Path $BrowsersPath) {
        try { Remove-Item -Recurse -Force $BrowsersPath } catch {}
    }

    $origDownloadHost = $env:PLAYWRIGHT_DOWNLOAD_HOST
    $origBrowsersPath = $env:PLAYWRIGHT_BROWSERS_PATH
    try {
        $env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersPath
        $hosts = @()

        if (-not [string]::IsNullOrWhiteSpace($PreferredHost)) { $hosts += $PreferredHost }
        if (-not [string]::IsNullOrWhiteSpace($origDownloadHost)) { $hosts += $origDownloadHost }
        $hosts += @(
            "https://npmmirror.com/mirrors/playwright",
            "https://playwright.azureedge.net",
            "https://cdn.playwright.dev",
            "https://playwright-akamai.azureedge.net",
            "https://playwright-verizon.azureedge.net"
        )

        foreach ($h in $hosts) {
            if ([string]::IsNullOrWhiteSpace($h)) { continue }
            Write-Host "Trying PLAYWRIGHT_DOWNLOAD_HOST=$h"
            $env:PLAYWRIGHT_DOWNLOAD_HOST = $h
            & $VenvPython -m playwright install $Browser
            if ($LASTEXITCODE -eq 0 -and (Test-HasPlaywrightBrowser -BrowsersPath $BrowsersPath -Browser $Browser)) { return }
            Start-Sleep -Seconds 2
        }

        throw "Playwright browser download failed. Try setting -PlaywrightDownloadHost to an internal mirror, or generate the .playwright folder on a machine with working access and then copy it."
    } finally {
        try { $env:PLAYWRIGHT_DOWNLOAD_HOST = $origDownloadHost } catch {}
        try { $env:PLAYWRIGHT_BROWSERS_PATH = $origBrowsersPath } catch {}
    }
}

function Copy-PlaywrightBrowsersFromPath {
    param(
        [string]$SourcePath,
        [string]$DestPath,
        [string[]]$BrowserNames
    )

    if ([string]::IsNullOrWhiteSpace($SourcePath)) { return $false }
    if (!(Test-Path $SourcePath)) { return $false }

    if (-not $BrowserNames -or $BrowserNames.Count -eq 0) { return $false }

    if (!(Test-Path $DestPath)) { New-Item -ItemType Directory -Path $DestPath | Out-Null }

    $copiedAny = $false
    foreach ($n in $BrowserNames) {
        $dirs = @()
        try {
            $dirs = Get-ChildItem -Path $SourcePath -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like ($n + "-*") }
        } catch {
            $dirs = @()
        }
        foreach ($d in $dirs) {
            $dst = Join-Path $DestPath $d.Name
            Write-Host ("Copying browser dir: " + $d.FullName + " -> " + $dst)
            if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
            & robocopy $d.FullName $dst /MIR /R:1 /W:1 /NFL /NDL /NP /NJH /NJS | Out-Null
            $copiedAny = $true
        }
    }
    return $copiedAny
}

Write-Host "=== Build Playwright offline bundle (Windows) ==="

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")

if (-not $OutDir) {
    $OutDir = Resolve-Path (Join-Path $projectRoot "dist") -ErrorAction SilentlyContinue
    if (-not $OutDir) { $OutDir = Join-Path $projectRoot "dist" }
}
if (!(Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

$ver = $PlaywrightVersion
if ([string]::IsNullOrWhiteSpace($ver)) {
    $ver = Get-DetectedPlaywrightVersion -ProjectRoot $projectRoot
}
if ([string]::IsNullOrWhiteSpace($ver)) { $ver = "1.58.0" }

$date = Get-Date -Format "yyyyMMdd_HHmmss"
$bundleRoot = Join-Path $OutDir ("playwright_offline_bundle_windows_" + $ver + "_" + $date)
$zipPath = Join-Path $OutDir ("playwright_offline_bundle_windows_" + $ver + "_" + $date + ".zip")

if (Test-Path $bundleRoot) { Remove-Item -Recurse -Force $bundleRoot }
New-Item -ItemType Directory -Path $bundleRoot | Out-Null

$wheelhouse = Join-Path $bundleRoot "wheelhouse"
$browsersPath = Join-Path $bundleRoot ".playwright"
New-Item -ItemType Directory -Path $wheelhouse | Out-Null
New-Item -ItemType Directory -Path $browsersPath | Out-Null

$py = Get-PythonExe
$venv = $null

try {
    if (-not $SkipWheelhouse) {
        Write-Host "Downloading wheels: playwright==$ver"
        $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
        $env:PIP_NO_INPUT = "1"
        & $py.Exe @($py.Args) -m pip download --only-binary=:all: -d $wheelhouse ("playwright==" + $ver)
        if ($LASTEXITCODE -ne 0) { throw "pip download failed for playwright==$ver" }
    } else {
        Write-Host "Skip wheelhouse download."
    }

    $venv = New-TempVenv -Py $py
    Write-Host "Installing playwright into temp venv (offline from wheelhouse) ..."
    & $venv.Py -m pip install --disable-pip-version-check --no-input --no-index --find-links $wheelhouse ("playwright==" + $ver)
    if ($LASTEXITCODE -ne 0) { throw "pip install failed from wheelhouse for playwright==$ver" }

    if (-not $SkipBrowserDownload) {
        if ([string]::IsNullOrWhiteSpace($BrowsersSourcePath)) {
            $projectPw = Join-Path $projectRoot ".playwright"
            $msPw = Join-Path $env:LOCALAPPDATA "ms-playwright"
            if (Test-HasPlaywrightBrowser -BrowsersPath $projectPw -Browser $Browser) {
                $BrowsersSourcePath = $projectPw
            } elseif (Test-Path $msPw) {
                $BrowsersSourcePath = $msPw
            }
        }

        $browserNames = @($Browser)
        if ($Browser -eq "chromium") {
            $browserNames = @("chromium", "chromium_headless_shell", "ffmpeg")
        } else {
            $browserNames = @($Browser, "ffmpeg")
        }

        $copied = Copy-PlaywrightBrowsersFromPath -SourcePath $BrowsersSourcePath -DestPath $browsersPath -BrowserNames $browserNames
        if ($copied) {
            Write-Host ("Browser copied from cache: " + $BrowsersSourcePath)
            if ($Browser -eq "chromium") {
                $hasShell = Test-HasPlaywrightBrowser -BrowsersPath $browsersPath -Browser "chromium_headless_shell"
                if (-not $hasShell) {
                    Write-Host "Warning: missing chromium_headless_shell-* in bundled browsers. Headless launch may fail; download browsers on a machine with working Playwright CDN access and re-build."
                }
            }
        } else {
            Write-Host "Downloading Playwright browser: $Browser"
            Ensure-PlaywrightBrowserDownload -VenvPython $venv.Py -BrowsersPath $browsersPath -Browser $Browser -PreferredHost $PlaywrightDownloadHost
        }
    } else {
        Write-Host "Skip browser download."
    }

    $readme = @()
    $readme += "Playwright Offline Bundle (Windows)"
    $readme += ""
    $readme += ("Playwright version: " + $ver)
    $readme += ("Browser: " + $Browser)
    if (-not [string]::IsNullOrWhiteSpace($BrowsersSourcePath)) { $readme += ("Browsers source: " + $BrowsersSourcePath) }
    $readme += ""
    $readme += "Target machine steps:"
    $readme += "1) Copy this bundle folder to the target machine."
    $readme += "2) Copy .playwright/ into the project root (or set PLAYWRIGHT_BROWSERS_PATH to this .playwright path)."
    $readme += "3) Install Python package offline: python -m pip install --no-index --find-links .\\wheelhouse playwright==$ver"
    $readme += "4) (Optional) Self-check: python -c ""from playwright.sync_api import sync_playwright;p=sync_playwright().start();b=p.chromium.launch(headless=True);b.close();p.stop();print('ok')"""
    Set-Content -Path (Join-Path $bundleRoot "README.txt") -Value ($readme -join "`r`n") -Encoding UTF8

    Copy-Item -Force (Join-Path $projectRoot "release\install_playwright_offline.ps1") (Join-Path $bundleRoot "install_playwright_offline.ps1")

    Write-Host "Creating ZIP: $zipPath"
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
    Compress-Archive -Path (Join-Path $bundleRoot "*") -DestinationPath $zipPath

    Write-Host "Done: $zipPath"
} finally {
    try { if ($venv -and (Test-Path $venv.Dir)) { Remove-Item -Recurse -Force $venv.Dir } } catch {}
    try { if (Test-Path $bundleRoot) { Remove-Item -Recurse -Force $bundleRoot } } catch {}
}
