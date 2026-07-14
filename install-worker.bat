@echo off
rem Create a self-contained worker environment (bs_env: Pillow, numpy, Google libs)
rem next to the app. Most people should use install.bat instead; this is here for
rem re-running just the worker step (e.g. after deleting bs_env).
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
echo [2/2] Creating worker environment (bs_env) ...
%PYEXE% -m venv bs_env || (echo Could not create venv & pause & exit /b 1)
call bs_env\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-worker.txt
call bs_env\Scripts\deactivate.bat

echo.
echo   Writing config.json ...
if not exist config.json (
    copy /y config.example.json config.json >nul
)

set "WORKER_PY=%~dp0bs_env\Scripts\python.exe"
set "CFGPY=%TEMP%\bs_write_config_%RANDOM%.py"
if exist "%CFGPY%" del /q "%CFGPY%" >nul 2>nul
echo import json, sys, os> "%CFGPY%"
echo path = sys.argv[1]>> "%CFGPY%"
echo pairs = sys.argv[2:]>> "%CFGPY%"
echo updates = {}>> "%CFGPY%"
echo i = 0 >> "%CFGPY%"
echo while i ^< len(pairs):>> "%CFGPY%"
echo     updates[pairs[i]] = pairs[i + 1]>> "%CFGPY%"
echo     i += 2 >> "%CFGPY%"
echo with open(path, "r", encoding="utf-8") as f:>> "%CFGPY%"
echo     cfg = json.load(f)>> "%CFGPY%"
echo def is_placeholder(key, value):>> "%CFGPY%"
echo     if not isinstance(value, str) or value.strip() == "":>> "%CFGPY%"
echo         return True>> "%CFGPY%"
echo     v = value.strip()>> "%CFGPY%"
echo     if v.startswith("/path/to"):>> "%CFGPY%"
echo         return True>> "%CFGPY%"
echo     for frag in ("worker_env", "transnet_env"):>> "%CFGPY%"
echo         if frag in v and not os.path.exists(v):>> "%CFGPY%"
echo             return True>> "%CFGPY%"
echo     return False>> "%CFGPY%"
echo changed = False>> "%CFGPY%"
echo for key, value in updates.items():>> "%CFGPY%"
echo     if value == "":>> "%CFGPY%"
echo         continue>> "%CFGPY%"
echo     current = cfg.get(key, "")>> "%CFGPY%"
echo     if is_placeholder(key, current):>> "%CFGPY%"
echo         cfg[key] = value>> "%CFGPY%"
echo         changed = True>> "%CFGPY%"
echo if changed:>> "%CFGPY%"
echo     with open(path, "w", encoding="utf-8") as f:>> "%CFGPY%"
echo         json.dump(cfg, f, indent=2)>> "%CFGPY%"
echo         f.write("\n")>> "%CFGPY%"
echo     print("config.json updated")>> "%CFGPY%"
echo else:>> "%CFGPY%"
echo     print("config.json already configured, no changes needed")>> "%CFGPY%"

bs_env\Scripts\python.exe "%CFGPY%" "%~dp0config.json" worker_python "%WORKER_PY%"
del /q "%CFGPY%" >nul 2>nul

echo.
echo Worker Python is:  %WORKER_PY%
echo config.json has been updated (if worker_python was still a placeholder).
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
echo    3. Close this window, open a NEW terminal, and re-run install-worker.bat.
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
echo    2. Close this window, open a NEW terminal, and re-run install-worker.bat.
echo ============================================================================
pause
exit /b 1
