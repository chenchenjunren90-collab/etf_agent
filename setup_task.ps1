# 注册 Windows 计划任务：自动运行 ETF 每日预测 + 拉起浏览器展示提交预览
# 用法：右键 -> 用 PowerShell 运行，或在管理员 PowerShell 中执行
#
# 时间设定说明：
#   比赛（智投未来 · 驼灵大赛复赛）当前已知的每日提交截止约为 08:10（上海时区）。
#   为留出网络抓取/大模型调用的缓冲，主任务提前到 07:40 启动；
#   若主任务因偶发网络问题失败，07:55 的备份任务会自动重跑一次
#   （daily_job.py 对同一天是幂等的：已成功生成的预测不会被重复覆盖，
#   见 daily_run_guard.has_daily_run，重跑只会原样打印已有结果，安全无副作用）。
#
#   **请务必以平台通知页 (tuoling-competition-semifinal-notice.html /
#   investment-daily-submit.html) 实际显示的截止时间为准**，如与 08:10 不同，
#   请修改下方 $PrimaryTime / $RetryTime 后重新运行本脚本。

$PrimaryTime = "07:40"
$RetryTime   = "07:55"

$taskName      = "ETF_Agent_DailyJob"
$retryTaskName = "ETF_Agent_DailyJob_Retry"
$batFile       = (Resolve-Path (Join-Path $PSScriptRoot "start_auto.bat")).Path

# 先删旧任务（避免重复）
schtasks /Delete /TN $taskName /F 2>$null
schtasks /Delete /TN $retryTaskName /F 2>$null

schtasks /Create `
  /TN $taskName `
  /TR "`"$batFile`"" `
  /SC WEEKLY `
  /D MON,TUE,WED,THU,FRI `
  /ST $PrimaryTime `
  /F

$primaryOk = ($LASTEXITCODE -eq 0)

schtasks /Create `
  /TN $retryTaskName `
  /TR "`"$batFile`"" `
  /SC WEEKLY `
  /D MON,TUE,WED,THU,FRI `
  /ST $RetryTime `
  /F

$retryOk = ($LASTEXITCODE -eq 0)

if ($primaryOk -and $retryOk) {
    Write-Host ""
    Write-Host "=== 计划任务已注册 ===" -ForegroundColor Green
    Write-Host "  主任务: $taskName  ($PrimaryTime，工作日)"
    Write-Host "  备份任务: $retryTaskName  ($RetryTime，工作日，主任务失败时的安全重试)"
    Write-Host "  脚本: $batFile"
    Write-Host ""
    Write-Host "生成完成后会自动打开浏览器到 http://127.0.0.1:8765/?screenshot=1"
    Write-Host "（截图模式：只显示提交预览卡片，方便直接截图上传）"
    Write-Host ""
    Write-Host "查看任务: schtasks /Query /TN $taskName /FO LIST /V"
    Write-Host "手动运行: schtasks /Run  /TN $taskName"
    Write-Host "删除任务: schtasks /Delete /TN $taskName /F ; schtasks /Delete /TN $retryTaskName /F"
} else {
    Write-Host ""
    Write-Host "[错误] 注册失败，退出码 $LASTEXITCODE" -ForegroundColor Red
    Write-Host "请以管理员身份运行此脚本。"
}
