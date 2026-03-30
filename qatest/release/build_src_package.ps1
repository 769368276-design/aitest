$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function New-SrcZip {
    param(
        [string]$ProjectRoot,
        [string]$OutDir
    )

    if (!(Test-Path $ProjectRoot)) { throw "ProjectRoot not found: $ProjectRoot" }
    if (!(Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

    $date = Get-Date -Format "yyyyMMdd-HHmmss"
    $zipPath = Join-Path $OutDir ("qatest-src-" + $date + ".zip")

    $tmp = Join-Path $OutDir ("_src_tmp_" + $date)
    Get-ChildItem -Path $OutDir -Directory -Filter "_src_tmp_*" -ErrorAction SilentlyContinue | ForEach-Object {
        try { Remove-Item -Recurse -Force $_.FullName } catch {}
    }
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    New-Item -ItemType Directory -Path $tmp | Out-Null

    try {
        $excludeDir = @(
            ".git",
            ".venv",
            "dist",
            "backup",
            "media",
            "staticfiles",
            ".pytest_cache",
            ".mypy_cache",
            "wheelhouse",
            ".playwright",
            "_pkg_smoketest"
        )

        $xd = @()
        foreach ($x in $excludeDir) { $xd += "/XD"; $xd += (Join-Path $ProjectRoot $x) }

        Write-Host "=== Build source ZIP ==="
        Write-Host "Copying files..."
        & robocopy $ProjectRoot $tmp /MIR /MT:16 /R:1 /W:1 /NFL /NDL /NP /NJH /NJS @xd | Out-Host

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

        Write-Host "Creating ZIP: $zipPath"
        if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
        Compress-Archive -Path (Join-Path $tmp "*") -DestinationPath $zipPath
        return $zipPath
    } finally {
        try { if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp } } catch {}
    }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
$outDir = Resolve-Path (Join-Path $scriptDir "..\\dist") -ErrorAction SilentlyContinue
if (-not $outDir) {
    $outDir = Join-Path $projectRoot "dist"
}

$zip = New-SrcZip -ProjectRoot $projectRoot -OutDir $outDir
Write-Host "Done: $zip"

