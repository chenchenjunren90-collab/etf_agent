# 注册 Windows 计划任务：每个工作日 09:25 自动运行 ETF 每日预测
# 用法：右键 -> 用 PowerShell 运行，或在管理员 PowerShell 中执行

$taskName = "ETF_Agent_DailyJob"
$batFile  = (Resolve-Path (Join-Path $PSScriptRoot "start_auto.bat")).Path

# 先删旧任务（避免重复）
schtasks /Delete /TN $taskName /F 2>$null

schtasks /Create `
  /TN $taskName `
  /TR "`"$batFile`"" `
  /SC WEEKLY `
  /D MON,TUE,WED,THU,FRI `
  /ST 09:25 `
  /F

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "=== 计划任务已注册 ===" -ForegroundColor Green
    Write-Host "  名称: $taskName"
    Write-Host "  脚本: $batFile"
    Write-Host "  时间: 每个工作日 09:25"
    Write-Host ""
    Write-Host "查看任务: schtasks /Query /TN $taskName /FO LIST /V"
    Write-Host "手动运行: schtasks /Run  /TN $taskName"
    Write-Host "删除任务: schtasks /Delete /TN $taskName /F"
} else {
    Write-Host ""
    Write-Host "[错误] 注册失败，退出码 $LASTEXITCODE" -ForegroundColor Red
    Write-Host "请以管理员身份运行此脚本。"
}
