@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

echo ============================================================
echo   ETF 投资智能体 - 对话前端
echo   地址: http://127.0.0.1:8766
echo   请先运行每日预测以更新知识库（start_auto.bat）
echo   关闭本窗口即停止服务
echo ============================================================
echo.

py -3 -c "from agent_kb import load_knowledge_base; load_knowledge_base()" 2>nul
py -3 agent_server.py
pause
