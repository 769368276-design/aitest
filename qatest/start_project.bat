@echo off
setlocal
cd /d "%~dp0"
echo Starting Project...
echo Project dir: %cd%

if not exist ".venv\Scripts\python.exe" (
  echo Creating venv .venv ...
  python -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
  echo Venv failed: missing .venv\Scripts\python.exe
  exit /b 1
)

echo Installing dependencies (requirements.txt) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-input -r requirements.txt
if errorlevel 1 exit /b 1

set "PLAYWRIGHT_BROWSERS_PATH=%cd%\.playwright"
echo Installing Playwright browsers to: %PLAYWRIGHT_BROWSERS_PATH%
".venv\Scripts\python.exe" -m playwright install chromium chromium-headless-shell ffmpeg
if errorlevel 1 exit /b 1

echo Running Migrations...
".venv\Scripts\python.exe" manage.py migrate
if errorlevel 1 exit /b 1
echo Starting Server...
".venv\Scripts\python.exe" manage.py runserver 0.0.0.0:8003
pause
