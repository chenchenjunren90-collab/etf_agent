@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set CAPITAL=500000
set ETF_AGENT_ALLOW_NETWORK=1
set ETF_AGENT_STRICT_DATA=1
set SCORE_GATE_MODE=dynamic

cd /d "%~dp0"

echo ============================================================
echo   ETF 智能体 - 每日新闻驱动预测
echo   1. 更新 ETF 行情（先更新，再算昨日盈亏）
echo   2. 复盘上一日预测开盘到收盘收益
echo   3. 抓取 09:30 前新闻并按严格规则筛选
echo   4. 输出比赛格式 [{symbol, symbol_name, volume}]
echo ============================================================
echo.

py -3 daily_job.py %*
set ERR=%ERRORLEVEL%

echo.
if exist data\daily_output\last_pnl_report.txt (
    echo --- 上一日收益报告 ---
    type data\daily_output\last_pnl_report.txt
)

echo.
if %ERR% neq 0 (
    echo [失败] 退出码 %ERR%
) else (
    echo [完成] 输出目录: data\daily_output
    echo 正在打开仪表盘（截图模式）用于参赛提交...
    start "" /min py -3 dashboard_server.py --no-browser
    timeout /t 2 >nul
    start "" "http://127.0.0.1:8765/?screenshot=1"
)
exit /b %ERR%
