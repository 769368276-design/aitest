@echo off
setlocal

set "TARGET_HOST=8.130.85.131"
set "TARGET_USER=root"
set "IDENTITY_FILE=%IDENTITY_FILE%"
if "%IDENTITY_FILE%"=="" set "IDENTITY_FILE=%USERPROFILE%\.ssh\id_rsa"
if not exist "%IDENTITY_FILE%" (
  if exist "%USERPROFILE%\.ssh\id_ed25519" set "IDENTITY_FILE=%USERPROFILE%\.ssh\id_ed25519"
)
set "REMOTE_DIR=/opt/qatest"

echo === Automated Deploy (Windows CMD) ===

set "SSH_ID_OPT="
if exist "%IDENTITY_FILE%" (
  set "SSH_ID_OPT=-i %IDENTITY_FILE%"
  echo Using SSH key: %IDENTITY_FILE%
) else (
  echo WARN: SSH key not found. Will use password login ^(ssh/scp will prompt for password^).
  echo HINT: If you do not know the root password, reset it in the ECS console first.
)

echo [1/4] Packing source...
if exist project.tar.gz del /f /q project.tar.gz >nul 2>nul
tar -czf project.tar.gz --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=media --exclude=staticfiles --exclude=packages --exclude=*.tar --exclude=*.tar.gz --exclude=*.zip .
if errorlevel 1 (
  echo ERROR: tar failed. Please ensure Windows tar is available.
  exit /b 1
)

echo [2/4] Uploading to server...
if exist "%IDENTITY_FILE%" (
  scp -o StrictHostKeyChecking=no -i "%IDENTITY_FILE%" project.tar.gz "%TARGET_USER%@%TARGET_HOST%:/tmp/project.tar.gz"
) else (
  scp -o StrictHostKeyChecking=no project.tar.gz "%TARGET_USER%@%TARGET_HOST%:/tmp/project.tar.gz"
)
if errorlevel 1 (
  echo ERROR: scp failed. Please check SSH connectivity and key permissions.
  exit /b 1
)

echo [3/4] Running remote deploy...
if exist "%IDENTITY_FILE%" (
  ssh -o StrictHostKeyChecking=no -i "%IDENTITY_FILE%" "%TARGET_USER%@%TARGET_HOST%" "mkdir -p %REMOTE_DIR% && tar -xzf /tmp/project.tar.gz -C %REMOTE_DIR% && rm /tmp/project.tar.gz && bash %REMOTE_DIR%/deploy/setup_server.sh"
) else (
  ssh -o StrictHostKeyChecking=no "%TARGET_USER%@%TARGET_HOST%" "mkdir -p %REMOTE_DIR% && tar -xzf /tmp/project.tar.gz -C %REMOTE_DIR% && rm /tmp/project.tar.gz && bash %REMOTE_DIR%/deploy/setup_server.sh"
)
if errorlevel 1 (
  echo ERROR: remote deploy failed. Please login and check logs:
  echo   ssh %SSH_ID_OPT% %TARGET_USER%@%TARGET_HOST%
  echo   cd %REMOTE_DIR% ^&^& docker compose -f docker-compose.prod.yml ps
  exit /b 1
)

echo [4/4] Cleaning up...
del /f /q project.tar.gz >nul 2>nul

echo DONE: Deployment completed.
echo Open: http://%TARGET_HOST%/
endlocal
