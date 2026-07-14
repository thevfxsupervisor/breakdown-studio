@echo off
rem ============================================================================
rem  Breakdown Studio launcher (Windows)
rem  Finds a GUI-capable Python (NOT the Windows Store "python" stub) and runs
rem  the app with pythonw (no console window).
rem ============================================================================
setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

set "PYW="

rem 1) Best source: the worker_python in config.json -> derive its pythonw.exe.
if exist "config.json" (
  for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try{$p=(Get-Content -Raw 'config.json' ^| ConvertFrom-Json).worker_python; if($p){($p -replace 'python\.exe$','pythonw.exe')}}catch{}"`) do set "PYW=%%P"
)
if defined PYW if not exist "!PYW!" set "PYW="

rem 2) Known ShotGrid Python (ships tkinter).
if not defined PYW if exist "C:\Program Files\Shotgun\Python3\pythonw.exe" set "PYW=C:\Program Files\Shotgun\Python3\pythonw.exe"

rem 3) A real pythonw on PATH (the Store stub is named python/python3, never pythonw).
if not defined PYW for %%C in (pythonw.exe) do if not "%%~$PATH:C"=="" set "PYW=%%~$PATH:C"

if not defined PYW (
  echo.
  echo  Could not find a GUI-capable Python.
  echo  Fix: set "worker_python" in config.json to your python.exe, or install
  echo  Python 3 from https://www.python.org ^(tick "Add to PATH"^).
  echo  Then run install.bat. For error details run "Breakdown Studio (debug).bat".
  echo.
  pause
  exit /b 1
)

start "" "!PYW!" "breakdown_studio.py"
exit /b 0
