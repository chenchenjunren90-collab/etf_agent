@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

echo ============================================================
echo   ETF 投资智能体 - 对话前端（完整版）
echo   本机: http://127.0.0.1:8766
echo   功能: 投资建议收集 / 比赛 JSON / 新闻解读 / 边界守卫
echo   关闭本窗口即停止服务
echo ============================================================
echo.

py -3 -c "from agent_kb import load_knowledge_base; load_knowledge_base()" 2>nul
py -3 agent_server.py --no-browser
pause
