@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

where python >nul 2>nul
if %errorlevel%==0 (
  python server\app.py --open
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3 server\app.py --open
  ) else (
    echo 没有找到 python，请先安装 Python 3.10 或更高版本。
    pause
    exit /b 1
  )
)

if errorlevel 1 pause
