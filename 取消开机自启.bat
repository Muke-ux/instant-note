@echo off
chcp 65001 >nul 2>&1
setlocal
set "TASK=InstantNote"

echo ================================================
echo   即时备忘 - 取消开机自启
echo ================================================
echo.

net session >nul 2>&1
if errorlevel 1 (
  echo 需要管理员权限，正在申请...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo [1/2] 删除计划任务...
schtasks /Delete /TN "%TASK%" /F >nul 2>&1
if errorlevel 1 (
  echo        没有找到计划任务，跳过
) else (
  echo        已删除
)

echo [2/2] 清掉注册表启动项...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v InstantNote /f >nul 2>&1
if errorlevel 1 (
  echo        没有找到注册表项，跳过
) else (
  echo        已删除
)

echo.
echo ================================================
echo   完成。以后开机不会再自动启动了。
echo   想恢复，运行「设置开机自启(管理员).bat」。
echo ================================================
echo.
pause
