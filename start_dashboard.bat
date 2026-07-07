@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

echo ============================================================
echo   ETF 智能体 - 本地展示面板
echo   地址: http://127.0.0.1:8765
echo   关闭本窗口即停止服务
echo ============================================================
echo.

py -3 dashboard_server.py
pause
