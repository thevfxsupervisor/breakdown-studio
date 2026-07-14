@echo off
rem ============================================================================
rem Breakdown Studio - one-shot installer (Windows)
rem   1) validates the Python interpreter (guards against the Microsoft Store stub)
rem   2) creates the worker venv (Pillow, numpy, Google libs)
rem   3) optionally creates the TransNetV2 venv (large; torch)
rem   4) checks ffmpeg and Tkinter (warnings only, non-fatal)
rem   5) prints the paths to paste into Settings
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo [1/4] Checking for a working Python interpreter ...

set "PYEXE="

rem --- Prefer the "py" launcher (py.exe), which is never the Store stub. ---
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; print(sys.version)" >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py -3"
    )
)

rem --- Fall back to "python" on PATH, but verify it is real, not the Store stub. ---
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%P in ('where python 2^>nul') do (
            if not defined PYPATH set "PYPATH=%%P"
        )
        echo !PYPATH! | findstr /I "WindowsApps" >nul
        if not errorlevel 1 (
            goto :store_stub
        )
        python -c "import sys; print(sys.version)" >nul 2>nul
        if not errorlevel 1 (
            set "PYEXE=python"
        )
    )
)

if not defined PYEXE goto :no_python

for /f "usebackq delims=" %%V in (`%PYEXE% -c "import sys; print(sys.version.split()[0])" 2^>nul`) do set "PYVER=%%V"
echo   Using Python !PYVER! ( %PYEXE% )

echo.
echo [2/4] Creating worker environment (bs_env) ...
%PYEXE% -m venv bs_env || (echo Could not create bs_env & pause & exit /b 1)
call bs_env\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-worker.txt
call bs_env\Scripts\deactivate.bat

echo.
echo   Checking Tkinter in the new environment (needed for the desktop GUI) ...
bs_env\Scripts\python.exe -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo   WARNING: Tkinter is not available in this Python.
    echo            The Tkinter GUI ^(breakdown_studio.py^) will not run.
    echo            Install a python.org build ^(its installers include Tkinter^), or
    echo            use the Qt GUI instead: breakdown_studio_qt.py ^(pip install PySide6^).
) else (
    echo   OK: Tkinter is available.
)

echo.
echo   Checking for ffmpeg on PATH ...
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo   WARNING: ffmpeg was not found on PATH.
    echo            Frames, cuts, and reference clips will not work until it is available.
    echo            Download it from https://ffmpeg.org, or point Settings - ffmpeg / ffprobe
    echo            at the full path to your ffmpeg.exe / ffprobe.exe.
) else (
    echo   OK: ffmpeg found on PATH.
)

echo.
set /p DOTN=Install the TransNetV2 detection env now? It is large (torch). [y/N]:
if /I "%DOTN%"=="y" (
  echo [3/4] Creating TransNetV2 environment (transnet_env) ...
  %PYEXE% -m venv transnet_env
  call transnet_env\Scripts\activate.bat
  python -m pip install --upgrade pip
  python -m pip install -r requirements-transnet.txt
  call transnet_env\Scripts\deactivate.bat
) else (
  echo Skipped TransNetV2 env. Run install-transnet.bat later if you want local detection.
)

echo.
echo [4/4] Creating desktop-style shortcut (Breakdown Studio.lnk) ...
powershell -NoProfile -ExecutionPolicy Bypass -File "_make_shortcut.ps1"

echo.
echo ============================================================================
echo  Done. Double-click "Breakdown Studio.lnk" (or "Breakdown Studio.bat"),
echo  open Settings, and set:
echo    Worker Python    = %~dp0bs_env\Scripts\python.exe
echo    TransNetV2 Python = %~dp0transnet_env\Scripts\python.exe   (if installed)
echo    ffmpeg / ffprobe  = your ffmpeg binaries (https://ffmpeg.org)
echo    Google OAuth client secret = your client_secret.json (see README)
echo ============================================================================
pause
exit /b 0

:store_stub
echo.
echo ============================================================================
echo  ERROR: "python" on PATH points into WindowsApps - that is the Microsoft
echo  Store stub, not a real Python. It fails silently (or opens the Store) when
echo  run from a script, so this installer cannot use it.
echo.
echo  Fix:
echo    1. Download and install Python 3.9+ from https://www.python.org/downloads/
echo       (tick "Add python.exe to PATH" during install).
echo    2. Optionally disable the stub: Settings - Apps - Advanced app settings -
echo       App execution aliases - turn OFF "python.exe" / "python3.exe".
echo    3. Close this window, open a NEW terminal, and re-run install.bat.
echo ============================================================================
pause
exit /b 1

:no_python
echo.
echo ============================================================================
echo  ERROR: No working Python 3.9+ interpreter was found ^(checked "py -3" and
echo  "python"^).
echo.
echo  Fix:
echo    1. Download and install Python 3.9+ from https://www.python.org/downloads/
echo       (tick "Add python.exe to PATH" during install).
echo    2. Close this window, open a NEW terminal, and re-run install.bat.
echo ============================================================================
pause
exit /b 1
