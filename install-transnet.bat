@echo off
rem ============================================================================
rem Advanced: separate detection env (GPU/CUDA users)
rem
rem The default install.bat installs AI features (detection + OCR) straight into
rem bs_env, which is right for almost everyone. Use THIS script only if you want
rem a specific CUDA torch wheel for GPU detection, kept in its own venv so it does
rem not affect bs_env. Install the matching torch wheel from https://pytorch.org
rem into transnet_env AFTER it is created (or edit requirements-transnet.txt first).
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo [1/2] Checking for a working Python interpreter ...

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
echo [2/2] Creating TransNetV2 environment (transnet_env) ...
%PYEXE% -m venv transnet_env || (echo Could not create venv & pause & exit /b 1)
call transnet_env\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-transnet.txt
call transnet_env\Scripts\deactivate.bat

echo.
echo ============================================================================
echo  Done. TransNetV2 Python is:  %~dp0transnet_env\Scripts\python.exe
echo  Open Breakdown Studio - Settings - "TransNetV2 Python" and set it to that path.
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
echo    3. Close this window, open a NEW terminal, and re-run install-transnet.bat.
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
echo    2. Close this window, open a NEW terminal, and re-run install-transnet.bat.
echo ============================================================================
pause
exit /b 1
