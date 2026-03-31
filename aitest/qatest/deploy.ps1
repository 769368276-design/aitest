$ErrorActionPreference = "Stop"
$TargetHost = "8.130.85.131"
$TargetUser = "root"
$IdentityFile = "$env:USERPROFILE\.ssh\id_rsa"
$RemoteDir = "/opt/qatest"

Write-Host "=== 自动化部署脚本 ===" -ForegroundColor Cyan

# 0. 检查密钥文件
if (-not (Test-Path $IdentityFile)) {
    Write-Host "错误: 找不到 SSH 密钥文件: $IdentityFile" -ForegroundColor Red
    exit 1
}

# 1. 打包代码
Write-Host "[1/4] 正在打包本地代码..." -ForegroundColor Green
tar -czf project.tar.gz --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=media --exclude=staticfiles --exclude="*.tar.gz" .

if ($LASTEXITCODE -ne 0) {
    Write-Host "错误: 打包失败。请确认已安装 tar 工具。" -ForegroundColor Red
    exit 1
}

# 2. 上传压缩包
Write-Host "[2/4] 正在上传代码到服务器..." -ForegroundColor Green
# 使用 ${变量} 语法避免变量名解析错误
scp -o StrictHostKeyChecking=no -i "$IdentityFile" project.tar.gz "${TargetUser}@${TargetHost}:/tmp/project.tar.gz"

if ($LASTEXITCODE -ne 0) {
    Write-Host "错误: 上传失败。请检查 SSH 连接或密钥。" -ForegroundColor Red
    exit 1
}

# 3. 解压并执行部署
Write-Host "[3/4] 远程部署中..." -ForegroundColor Green
$RemoteCmd = "mkdir -p $RemoteDir && tar -xzf /tmp/project.tar.gz -C $RemoteDir && rm /tmp/project.tar.gz && bash $RemoteDir/deploy/setup_server.sh"

ssh -o StrictHostKeyChecking=no -i "$IdentityFile" "${TargetUser}@${TargetHost}" "$RemoteCmd"

if ($LASTEXITCODE -ne 0) {
    Write-Host "错误: 远程部署执行出错。" -ForegroundColor Red
    exit 1
}

# 4. 清理
Remove-Item project.tar.gz -ErrorAction SilentlyContinue
Write-Host "[4/4] 部署完成！" -ForegroundColor Cyan
