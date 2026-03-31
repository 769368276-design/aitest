$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

function Say([string]$msg) { Write-Host ("[qatest-offline] " + $msg) }
function AssertOk([string]$action) {
  if ($LASTEXITCODE -ne 0) { throw ("Command failed (" + $LASTEXITCODE + "): " + $action) }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "docker not found. Install Docker Desktop and ensure docker is in PATH."
}

Say "Project dir: $ProjectRoot"

$null = docker info 2>$null
if ($LASTEXITCODE -ne 0) {
  throw "Docker daemon is not running. Start Docker Desktop and wait for it to be Running, then retry."
}

$ts = Get-Date -Format "yyyyMMdd_HHmm"
$packagesDir = Join-Path $ProjectRoot "packages"
New-Item -ItemType Directory -Force $packagesDir | Out-Null

$skipUpload = $false
if ($env:QA_SKIP_UPLOAD) {
  $v = $env:QA_SKIP_UPLOAD.ToLower()
  if ($v -eq "1" -or $v -eq "true" -or $v -eq "yes") { $skipUpload = $true }
}

$server = if ($env:QA_SERVER) { $env:QA_SERVER } else { "root@8.130.85.131" }
$identityFile = if ($env:QA_IDENTITY_FILE) { (Resolve-Path $env:QA_IDENTITY_FILE).Path } else { "" }

if (-not $identityFile) {
  $skipUpload = $true
  Say "QA_IDENTITY_FILE not set. Will only build/export locally (skip upload/deploy)."
}

$imagesTar = Join-Path $packagesDir ("qatest_images_" + $ts + ".tar")
$codeZip = Join-Path $packagesDir ("qatest_clean_" + $ts + ".zip")

$include = @(
  "ai_assistant","autotest","bugs","core","deploy","projects","qa_platform","qa_recorder_extension","requirements","scripts","static","testcases","users",
  "manage.py","requirements.txt","Dockerfile","docker-compose.prod.yml",".env.example",".dockerignore",".gitignore",
  "README.md","RUN_ON_NEW_PC.md","start_project.bat","安装点我.bat","安装点我.command","部署指南傻瓜版.md","部署指南傻瓜版-macOS.md","常见问题处理方案.md"
)

Say "Packaging clean source zip..."
$missing = @()
$existing = @()
foreach ($p in $include) {
  if ($p -and (Test-Path $p)) { $existing += $p } else { $missing += $p }
}
if ($missing.Count -gt 0) {
  Say ("Skipping missing paths: " + ($missing -join ", "))
}
tar -a -c -f $codeZip @existing
AssertOk "tar package"
Say "Clean zip: $codeZip"

$mirrorRegistry = if ($env:QA_DOCKER_MIRROR) { $env:QA_DOCKER_MIRROR } else { "docker.m.daocloud.io" }
$officialPyTag = "python:3.12-slim"
$pyMirrorImage = ($mirrorRegistry + "/library/python:3.12-slim")
if (-not $env:PY_BASE_IMAGE) {
  $env:PY_BASE_IMAGE = $pyMirrorImage
}
if ($env:PY_BASE_IMAGE -eq $officialPyTag) {
  Say "Pulling Python base image via mirror..."
  docker pull $pyMirrorImage
  AssertOk "docker pull python"
  docker tag $pyMirrorImage $officialPyTag
  AssertOk "docker tag python"
}

Say "Building images (web/worker) locally..."
$env:COMPOSE_DOCKER_CLI_BUILD = "1"
$env:DOCKER_BUILDKIT = "1"
docker compose -f docker-compose.prod.yml build web worker
if ($LASTEXITCODE -ne 0) {
  Say "Retrying build without BuildKit..."
  $env:COMPOSE_DOCKER_CLI_BUILD = "0"
  $env:DOCKER_BUILDKIT = "0"
  docker compose -f docker-compose.prod.yml build web worker
}
AssertOk "docker compose build"

Say "Pulling base images via mirror (optional but recommended)..."
docker pull ($mirrorRegistry + "/library/postgres:16-alpine")
AssertOk "docker pull postgres"
docker tag ($mirrorRegistry + "/library/postgres:16-alpine") postgres:16-alpine
AssertOk "docker tag postgres"
docker pull ($mirrorRegistry + "/library/redis:7-alpine")
AssertOk "docker pull redis"
docker tag ($mirrorRegistry + "/library/redis:7-alpine") redis:7-alpine
AssertOk "docker tag redis"
docker pull ($mirrorRegistry + "/library/nginx:1.27-alpine")
AssertOk "docker pull nginx"
docker tag ($mirrorRegistry + "/library/nginx:1.27-alpine") nginx:1.27-alpine
AssertOk "docker tag nginx"

Say "Exporting images to tar..."
docker save -o $imagesTar qatest-web qatest-worker postgres:16-alpine redis:7-alpine nginx:1.27-alpine
AssertOk "docker save"
if (-not (Test-Path $imagesTar)) { throw "Images tar not found: $imagesTar" }
Say "Images tar: $imagesTar"

if ($skipUpload) {
  Say "Skip upload/deploy. Artifacts are ready:"
  Say " - $imagesTar"
  Say " - $codeZip"
  exit 0
}

Say "Uploading artifacts to server..."
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -i $identityFile $imagesTar $server`:/tmp/qatest_images.tar
AssertOk "scp images"
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -i $identityFile $codeZip $server`:/tmp/qatest_clean.zip
AssertOk "scp code"

Say "Deploying on server (load images, unzip code, start without build/pull)..."
$remote = @'
set -e
cd /
docker compose -f /opt/qatest/docker-compose.prod.yml down --remove-orphans || true
rm -rf /opt/qatest && mkdir -p /opt/qatest
command -v unzip >/dev/null || (dnf install -y unzip || yum install -y unzip || (apt-get update && apt-get install -y unzip))
unzip -oq /tmp/qatest_clean.zip -d /opt/qatest
rm -f /tmp/qatest_clean.zip
docker load -i /tmp/qatest_images.tar
rm -f /tmp/qatest_images.tar
cd /opt/qatest
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
fi
bash deploy/setup_server.sh || true
docker compose -f docker-compose.prod.yml up -d --no-build --pull never
docker compose -f docker-compose.prod.yml ps
'@

ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -i $identityFile $server $remote
AssertOk "ssh deploy"

Say "Done."
