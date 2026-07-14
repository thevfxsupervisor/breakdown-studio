@echo off
rem ============================================================================
rem  Breakdown Studio - DEBUG launcher: runs with python.exe so a console window
rem  stays open and shows any startup error. Use this if the normal launcher
rem  "does nothing".
rem ============================================================================
setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

set "PY="
if exist "config.json" (
  for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try{(Get-Content -Raw 'config.json' ^| ConvertFrom-Json).worker_python}catch{}"`) do set "PY=%%P"
)
if defined PY if not exist "!PY!" set "PY="
if not defined PY if exist "C:\Program Files\Shotgun\Python3\python.exe" set "PY=C:\Program Files\Shotgun\Python3\python.exe"
if not defined PY for %%C in (python.exe) do if not "%%~$PATH:C"=="" set "PY=%%~$PATH:C"

if not defined PY (
  echo Could not find python.exe. Set "worker_python" in config.json or install Python 3.
  pause & exit /b 1
)

echo Using interpreter: "!PY!"
echo Launching Breakdown Studio... (close this window to quit)
"!PY!" "breakdown_studio.py"
echo.
echo [app exited with code %errorlevel%]
pause
