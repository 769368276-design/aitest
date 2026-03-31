$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function New-ReleaseZip {
    param(
        [string]$ProjectRoot,
        [string]$OutDir
    )

    if (!(Test-Path $ProjectRoot)) { throw "ProjectRoot 不存在：$ProjectRoot" }
    if (!(Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

    $date = Get-Date -Format "yyyyMMdd_HHmmss"
    $zipPath = Join-Path $OutDir ("qa_platform_release_" + $date + ".zip")

    $tmp = Join-Path $OutDir ("_release_tmp_" + $date)
    Get-ChildItem -Path $OutDir -Directory -Filter "_release_tmp_*" -ErrorAction SilentlyContinue | ForEach-Object {
        try { Remove-Item -Recurse -Force $_.FullName } catch {}
    }
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    New-Item -ItemType Directory -Path $tmp | Out-Null

    $exclude = @(
        ".venv",
        "db.sqlite3",
        "staticfiles",
        "media",
        "__pycache__",
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".DS_Store",
        "backup",
        "dist"
    )

    $gitOk = $false
    try {
        if (Test-Path (Join-Path $ProjectRoot ".git")) {
            $null = & git -C $ProjectRoot rev-parse --is-inside-work-tree 2>$null
            if ($LASTEXITCODE -eq 0) { $gitOk = $true }
        }
    } catch { $gitOk = $false }
    if ($gitOk) {
        try {
            $dirty = & git -C $ProjectRoot status --porcelain 2>$null
            if ($dirty) { $gitOk = $false }
        } catch { $gitOk = $false }
    }

    if ($gitOk) {
        Write-Host "使用 git archive 生成 ZIP：$zipPath"
        if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
        & git -C $ProjectRoot archive --format=zip --output $zipPath HEAD
        if ($LASTEXITCODE -ne 0) { throw "git archive 失败" }
        return $zipPath
    }

    try {
        Write-Host "复制项目文件（fallback）..."
        $xd = @()
        foreach ($x in $exclude) { $xd += "/XD"; $xd += (Join-Path $ProjectRoot $x) }
        & robocopy $ProjectRoot $tmp /MIR /R:1 /W:1 /NFL /NDL /NP /NJH /NJS @xd | Out-Null

        Write-Host "打包 ZIP：$zipPath"
        if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
        Compress-Archive -Path (Join-Path $tmp "*") -DestinationPath $zipPath
        return $zipPath
    } finally {
        try { if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp } } catch {}
    }
}

Write-Host "=== 生成发布包 ==="
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptDir "..")
$outDir = Resolve-Path (Join-Path $scriptDir "..\\dist") -ErrorAction SilentlyContinue
if (-not $outDir) {
    $outDir = Join-Path $projectRoot "dist"
}

$zip = New-ReleaseZip -ProjectRoot $projectRoot -OutDir $outDir
Write-Host "完成：$zip"
