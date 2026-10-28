@echo off
rem vm_bord launcher -- keep messages ASCII: a .bat's own text is decoded with the
rem codepage that was active when cmd started, so Chinese here can turn into mojibake.
rem Line endings must stay CRLF, otherwise cmd mis-parses the script.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

rem Make sure python actually runs: the Microsoft Store alias only opens the Store.
python -c "import sys" >nul 2>nul
if not errorlevel 1 goto use_python
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 goto use_py
goto no_python

:use_python
set "PY=python"
goto check_deps

:use_py
set "PY=py -3"

:check_deps
rem The package directory is pyVmomi: a lowercase "import pyvmomi" fails here.
%PY% -c "import pyVmomi" >nul 2>nul
if not errorlevel 1 goto run
echo [vm_bord] dependency pyvmomi is missing, installing...
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto no_deps
%PY% -c "import pyVmomi" >nul 2>nul
if errorlevel 1 goto no_deps

:run
echo [vm_bord] starting dashboard, the browser will open in a few seconds.
echo [vm_bord] closing this window also stops the service.
%PY% -u "server\app.py" --open
if errorlevel 1 pause
exit /b 0

:no_python
echo [vm_bord] no usable Python found. Install Python 3.10+ and tick
echo           "Add python.exe to PATH" during setup.
pause
exit /b 1

:no_deps
echo [vm_bord] dependency install failed. Run this manually:
echo           pip install -r requirements.txt
pause
exit /b 1
