@echo off
chcp 65001 >nul
cd /d "%~dp0"
title HPoker-Agent
set PYTHONUTF8=1

echo ===================================================
echo   HPoker-Agent 启动脚本
echo ===================================================
echo 正在检查并停止旧程序...

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$killed = [System.Collections.Generic.List[int]]::new();" ^
  "Get-NetTCPConnection -LocalPort 8766 -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -gt 0 } | ForEach-Object {" ^
  "  $pidNum = $_.OwningProcess;" ^
  "  if (-not $killed.Contains($pidNum)) {" ^
  "    Stop-Process -Id $pidNum -Force -ErrorAction SilentlyContinue;" ^
  "    Write-Host ('  - 已终止占用 8766 端口的旧进程 [PID: {0}]' -f $pidNum);" ^
  "    $killed.Add($pidNum);" ^
  "  }" ^
  "};" ^
  "$currDir = [regex]::Escape($PWD.Path);" ^
  "Get-CimInstance Win32_Process | Where-Object {" ^
  "  $_.Name -match '^python' -and ($_.CommandLine -match '(\s|[\\/])app\.py(\s|$)' -or $_.CommandLine -match $currDir)" ^
  "} | ForEach-Object {" ^
  "  $pidNum = $_.ProcessId;" ^
  "  if (-not $killed.Contains($pidNum)) {" ^
  "    Stop-Process -Id $pidNum -Force -ErrorAction SilentlyContinue;" ^
  "    Write-Host ('  - 已终止残留的 app.py 进程 [PID: {0}]' -f $pidNum);" ^
  "    $killed.Add($pidNum);" ^
  "  }" ^
  "};" ^
  "if ($killed.Count -eq 0) { Write-Host '  - 未发现正在运行的旧程序。' } else { Start-Sleep -Milliseconds 500 }"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [错误] 未检测到 Python 环境，请确认已安装 Python 并添加到系统 PATH 环境变量。
    echo.
    pause
    exit /b 1
)

echo.
echo 正在启动服务 (http://127.0.0.1:8766)...
echo ===================================================
python app.py
if errorlevel 1 (
    echo.
    echo ===================================================
    echo [提示] 程序已退出 (退出代码: %errorlevel%)
    echo ===================================================
    pause
)
