param(
    [switch]$NoWheelhouse,
    [switch]$IncludePlaywrightBrowsers,
    [switch]$PreparePlaywrightBrowsers,
    [string]$PlaywrightBrowser = "chromium"
)

$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Get-PythonExe {
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Exe = "python"; Args = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @{ Exe = "py"; Args = @("-3") } }
    throw "python/py not found. Install Python 3.12+ and ensure it is in PATH."
}

function Ensure-PlaywrightBrowsers {
    param(
        [string]$ProjectRoot,
        [string]$Browser
    )

    $pwDir = Join-Path $ProjectRoot ".playwright"
    if (Test-Path $pwDir) {
        $hasBrowser = $false
        try {
            $hasBrowser = (Get-ChildItem -Path $pwDir -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like ($Browser + "-*") } | Select-Object -First 1) -ne $null
        } catch { $hasBrowser = $false }
        if ($hasBrowser) { return }
        try { Remove-Item -Recurse -Force $pwDir } catch {}
    }

    $py = Get-PythonExe
    $venvDir = Join-Path $env:TEMP ("qatest_pwvenv_" + [Guid]::NewGuid().ToString("N"))
    $origDownloadHost = $env:PLAYWRIGHT_DOWNLOAD_HOST
    $origBrowsersPath = $env:PLAYWRIGHT_BROWSERS_PATH

    try {
        Write-Host "Preparing Playwright browsers (target: $pwDir, browser: $Browser) ..."

        & $py.Exe @($py.Args) -m venv $venvDir
        $venvPy = Join-Path $venvDir "Scripts\python.exe"
        if (!(Test-Path $venvPy)) { throw "Temp venv failed: missing $venvPy" }

        $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
        $env:PIP_NO_INPUT = "1"

        & $venvPy -m pip install --disable-pip-version-check --no-input playwright
        if ($LASTEXITCODE -ne 0) { throw "Failed to install playwright into temp venv." }

        $env:PLAYWRIGHT_BROWSERS_PATH = $pwDir
        $hosts = @()
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
            & $venvPy -m playwright install $Browser
            if ($LASTEXITCODE -eq 0 -and (Test-Path $pwDir)) { return }
            Start-Sleep -Seconds 2
        }

        throw "Playwright browser download failed. You can retry with a different PLAYWRIGHT_DOWNLOAD_HOST, or run scripts\windows_oneclick.ps1 on a networked machine to generate .playwright then re-run packaging."
    } finally {
        try { $env:PLAYWRIGHT_DOWNLOAD_HOST = $origDownloadHost } catch {}
        try { $env:PLAYWRIGHT_BROWSERS_PATH = $origBrowsersPath } catch {}
        try { if (Test-Path $venvDir) { Remove-Item -Recurse -Force $venvDir } } catch {}
    }
}

function New-LocalZip {
    param(
        [string]$ProjectRoot,
        [string]$OutDir,
        [bool]$BuildWheelhouse,
        [bool]$IncludePlaywrightBrowsers,
        [bool]$PreparePlaywrightBrowsers,
        [string]$PlaywrightBrowser
    )

    if (!(Test-Path $ProjectRoot)) { throw "ProjectRoot not found: $ProjectRoot" }
    if (!(Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

    $date = Get-Date -Format "yyyyMMdd_HHmmss"
    $zipPath = Join-Path $OutDir ("qatest_local_clean_windows_" + $date + ".zip")

    $tmp = Join-Path $OutDir ("_local_pkg_tmp_" + $date)
    Get-ChildItem -Path $OutDir -Directory -Filter "_local_pkg_tmp_*" -ErrorAction SilentlyContinue | ForEach-Object {
        try { Remove-Item -Recurse -Force $_.FullName } catch {}
    }
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    New-Item -ItemType Directory -Path $tmp | Out-Null

    try {
        if ($IncludePlaywrightBrowsers -and $PreparePlaywrightBrowsers) {
            Ensure-PlaywrightBrowsers -ProjectRoot $ProjectRoot -Browser $PlaywrightBrowser
        }

        $excludeDir = @(
            ".venv",
            ".git",
            ".pytest_cache",
            ".mypy_cache",
            "backup",
            "dist",
            "packages",
            "deploy",
            "_old",
            "media",
            "staticfiles",
            ".github",
            "_pkg_smoketest"
        )

        $xd = @()
        foreach ($x in $excludeDir) { $xd += "/XD"; $xd += (Join-Path $ProjectRoot $x) }

        Write-Host "Copying project files (local-only, excluding deploy/server artifacts)..."
        & robocopy $ProjectRoot $tmp /MIR /R:1 /W:1 /NFL /NDL /NP /NJH /NJS @xd | Out-Host

        $excludeFiles = @(
            "deploy.ps1",
            "deploy.cmd",
            "Dockerfile",
            ".dockerignore",
            "docker-compose.prod.yml",
            "docker-compose.yml",
            "docker-compose.*.yml"
        )
        foreach ($pattern in $excludeFiles) {
            Get-ChildItem -Path $tmp -Recurse -File -Filter $pattern -ErrorAction SilentlyContinue | ForEach-Object {
                try { Remove-Item -Force $_.FullName } catch {}
            }
        }

        Get-ChildItem -Path $tmp -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | ForEach-Object {
            try { Remove-Item -Recurse -Force $_.FullName } catch {}
        }
        Get-ChildItem -Path $tmp -Recurse -File -Include "*.pyc","*.pyo",".DS_Store" -ErrorAction SilentlyContinue | ForEach-Object {
            try { Remove-Item -Force $_.FullName } catch {}
        }
        Get-ChildItem -Path $tmp -Recurse -File -Filter ".env" -ErrorAction SilentlyContinue | ForEach-Object {
            try { Remove-Item -Force $_.FullName } catch {}
        }
        Get-ChildItem -Path $tmp -Recurse -File -Filter "db.sqlite3" -ErrorAction SilentlyContinue | ForEach-Object {
            try { Remove-Item -Force $_.FullName } catch {}
        }

        Get-ChildItem -Path $tmp -Recurse -File -Include "*.zip","*.tar","*.gz","*.tgz","*.tar.gz" -ErrorAction SilentlyContinue | ForEach-Object {
            try { Remove-Item -Force $_.FullName } catch {}
        }

        if ($IncludePlaywrightBrowsers) {
            $src = Join-Path $ProjectRoot ".playwright"
            $dst = Join-Path $tmp ".playwright"
            if (Test-Path $src) {
                Write-Host "Including Playwright browsers dir: .playwright"
                if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
                Copy-Item -Recurse -Force $src $dst
            } else {
                Write-Host "No .playwright found, skip including browsers dir. (Tip: re-run with -PreparePlaywrightBrowsers)"
            }
        }

        if ($BuildWheelhouse) {
            $py = Get-PythonExe
            $wheelhouse = Join-Path $tmp "wheelhouse"
            if (!(Test-Path $wheelhouse)) { New-Item -ItemType Directory -Path $wheelhouse | Out-Null }

            Write-Host "Building wheelhouse (offline dependency cache)..."
            $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
            $env:PIP_NO_INPUT = "1"

            $req = Join-Path $tmp "requirements.txt"
            if (!(Test-Path $req)) { throw "Missing requirements.txt in staging directory" }

            $downloadOk = $false
            try {
                & $py.Exe @($py.Args) -m pip download --only-binary=:all: -d $wheelhouse -r $req
                if ($LASTEXITCODE -eq 0) { $downloadOk = $true }
            } catch { $downloadOk = $false }

            if (-not $downloadOk) {
                Write-Host "Warning: wheel-only download failed, fallback to allowing sdists."
                & $py.Exe @($py.Args) -m pip download -d $wheelhouse -r $req
                if ($LASTEXITCODE -ne 0) { throw "wheelhouse build failed" }
            }
        }

        Write-Host "Creating ZIP: $zipPath"
        if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
        Compress-Archive -Path (Join-Path $tmp "*") -DestinationPath $zipPath
        return $zipPath
    } finally {
        try { if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp } } catch {}
    }
}

Write-Host "=== Build local-only package (Windows) ==="
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
$outDir = Resolve-Path (Join-Path $scriptDir "..\\dist") -ErrorAction SilentlyContinue
if (-not $outDir) {
    $outDir = Join-Path $projectRoot "dist"
}

$buildWheelhouse = -not $NoWheelhouse
$zip = New-LocalZip -ProjectRoot $projectRoot -OutDir $outDir -BuildWheelhouse:$buildWheelhouse -IncludePlaywrightBrowsers:$IncludePlaywrightBrowsers -PreparePlaywrightBrowsers:$PreparePlaywrightBrowsers -PlaywrightBrowser $PlaywrightBrowser
Write-Host "Done: $zip"
