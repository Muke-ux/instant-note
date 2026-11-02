# 即时备忘 - 注册开机计划任务（需要管理员权限）
$ErrorActionPreference = "Stop"

$dir = $PSScriptRoot
$exe = Join-Path $dir "python\pythonw.exe"
$py  = Join-Path $dir "app\main.py"
$task = "InstantNote"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "   即时备忘 - 设置开机自启（计划任务）" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host ("  程序目录: " + $dir)
Write-Host ""

if (-not (Test-Path $exe)) {
    Write-Host "[错误] 找不到 python\pythonw.exe" -ForegroundColor Red
    Write-Host "       请把这个文件放在「即时备忘-绿色版」文件夹里再运行。"
    Read-Host "按回车退出"
    exit 1
}
if (-not (Test-Path $py)) {
    Write-Host "[错误] 找不到 app\main.py" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[错误] 没有管理员权限。" -ForegroundColor Red
    Write-Host "       右键这个 .ps1 文件 -> 使用 PowerShell 运行之前，先以管理员身份打开 PowerShell。"
    Write-Host ""
    Write-Host "       更简单的办法：右键「设置开机自启(管理员).bat」，选「以管理员身份运行」。"
    Read-Host "按回车退出"
    exit 1
}

Write-Host "[1/4] 删除旧任务..." -NoNewline
try { schtasks /Delete /TN $task /F 2>$null | Out-Null } catch {}
Write-Host " 完成"

Write-Host "[2/4] 注册计划任务（登录时触发，延迟 3 秒）..." -NoNewline
$arg = '"' + $py + '" --autostart'
$action = New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT3S"
$set = New-ScheduledTaskSettingsSet -Priority 1 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$set.ExecutionTimeLimit = [TimeSpan]::Zero
$set.StartWhenAvailable = $true
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$prin = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $set -Principal $prin -Force | Out-Null
    Write-Host " 完成" -ForegroundColor Green
} catch {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ("       原因: " + $_.Exception.Message)
    Read-Host "按回车退出"
    exit 1
}

Write-Host "[3/4] 清掉旧注册表启动项..." -NoNewline
try {
    Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name InstantNote -ErrorAction Stop
    Write-Host " 已删除"
} catch {
    Write-Host " 本来就没有"
}

Write-Host "[4/4] 验证..." -NoNewline
$t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
if ($t) {
    Write-Host " OK" -ForegroundColor Green
    Write-Host ""
    Write-Host ("   状态   : " + $t.State)
    Write-Host ("   触发器 : 登录时（延迟 3 秒）")
    Write-Host ("   优先级 : " + $t.Settings.Priority)
    Write-Host ("   执行   : " + $t.Actions[0].Execute)
    Write-Host ("   参数   : " + $t.Actions[0].Arguments)
} else {
    Write-Host " 查不到，注册可能没成功" -ForegroundColor Red
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "   完成。下次开机会比以前早很多就弹出来。" -ForegroundColor Green
Write-Host "   想取消：右键「取消开机自启.bat」以管理员身份运行。"
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""
Read-Host "按回车退出"
