@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==========================================
echo qatest 一键安装 ^& 启动
echo ==========================================
echo.
echo 1) 将在本目录创建 .venv 并安装依赖
echo 2) 将执行 migrate/init_data 初始化
echo 3) 将启动服务（默认端口 8003）
echo.
echo 如卡住或报错，打开“本地安装说明.md”照着排查。
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows_oneclick.ps1"

echo.
echo ==========================================
echo 结束。若窗口一闪而过，请手动用 PowerShell 执行：
echo powershell -ExecutionPolicy Bypass -File .\scripts\windows_oneclick.ps1
echo ==========================================
pause
