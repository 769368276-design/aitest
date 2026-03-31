param(
    [string]$Browser = "chromium",
    [string]$BrowsersPath,
    [string]$PlaywrightDownloadHost
)

$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")

function Get-PythonCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Exe = "python"; Args = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @{ Exe = "py"; Args = @("-3") } }
    throw "Python not found. Install Python 3.12+ and ensure it is in PATH."
}

function Normalize-PlaywrightDownloadHost {
    param([string]$HostValue)
    if ([string]::IsNullOrWhiteSpace($HostValue)) { return "" }
    $h = [string]$HostValue
    $h = $h.Trim()
    $h = $h.Trim('"')
    $h = $h.Trim("'")
    $h = $h.Trim('`')
    $h = $h.Trim()
    $h = ($h -replace '\s+', '')
    return $h
}

if (-not $BrowsersPath) {
    $BrowsersPath = Join-Path $projectRoot ".playwright"
}

$venvPy = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (!(Test-Path $venvPy)) {
    $basePy = Get-PythonCommand
    & $basePy.Exe @($basePy.Args) -m venv (Join-Path $projectRoot ".venv")
}
if (!(Test-Path $venvPy)) { throw "Venv failed: missing $venvPy" }

$py = $venvPy

$origDownloadHost = $env:PLAYWRIGHT_DOWNLOAD_HOST
$origBrowsersPath = $env:PLAYWRIGHT_BROWSERS_PATH

try {
    $env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersPath
    Write-Host "PLAYWRIGHT_BROWSERS_PATH=$BrowsersPath"

    $targets = @()
    if ($Browser -eq "chromium") {
        $targets = @("chromium", "chromium-headless-shell", "ffmpeg")
    } else {
        $targets = @($Browser, "ffmpeg")
    }

    $hosts = @()
    if (-not [string]::IsNullOrWhiteSpace($PlaywrightDownloadHost)) { $hosts += $PlaywrightDownloadHost }
    if (-not [string]::IsNullOrWhiteSpace($origDownloadHost)) { $hosts += $origDownloadHost }
    $hosts += @(
        "https://npmmirror.com/mirrors/playwright",
        "https://playwright.azureedge.net",
        "https://cdn.playwright.dev",
        "https://playwright-akamai.azureedge.net",
        "https://playwright-verizon.azureedge.net"
    )

    foreach ($h in $hosts) {
        $dlHost = Normalize-PlaywrightDownloadHost -HostValue $h
        if ([string]::IsNullOrWhiteSpace($dlHost)) { continue }
        Write-Host "Trying PLAYWRIGHT_DOWNLOAD_HOST=$dlHost"
        $env:PLAYWRIGHT_DOWNLOAD_HOST = $dlHost
        & $py -m playwright install @targets
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Done."
            exit 0
        }
        Start-Sleep -Seconds 2
    }

    throw "Playwright browsers download failed. Current CDN may be blocked or unavailable from this machine/network."
} finally {
    try { $env:PLAYWRIGHT_DOWNLOAD_HOST = $origDownloadHost } catch {}
    try { $env:PLAYWRIGHT_BROWSERS_PATH = $origBrowsersPath } catch {}
}
