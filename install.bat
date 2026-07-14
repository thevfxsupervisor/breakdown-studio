@echo off
rem ============================================================================
rem Breakdown Studio - one-shot installer (Windows)
rem   1) validates the Python interpreter (guards against the Microsoft Store stub)
rem   2) creates the worker environment (bs_env): Pillow, numpy, Google libs
rem   3) asks ONE plain-language question about AI features (shot detection, OCR)
rem      and installs them into the SAME bs_env if wanted
rem   4) checks ffmpeg and Tkinter (offers to fetch ffmpeg if missing; non-fatal)
rem   5) writes config.json for you and creates the desktop shortcut
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo [1/5] Checking for a working Python interpreter ...

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
echo [2/5] Creating worker environment (bs_env) ...
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
echo [3/5] AI features ...
set "AI_INSTALLED="
set /p AIQ=Install AI features (shot detection + burn-in OCR)? ~2 GB download, recommended. [Y/n]:
if /I "%AIQ%"=="n" (
    echo   Skipped. These stages will be unavailable until you install AI features:
    echo     - Detect shots
    echo     - Slate OCR
    echo     - VFX-note OCR
    echo     - Boundary QC
    echo   Re-run install.bat later and answer Y to add them, or run install-transnet.bat
    echo   for the advanced ^(separate GPU/CUDA environment^) path.
) else (
    echo   Installing AI features into bs_env, this can take a while ...
    call bs_env\Scripts\activate.bat
    python -m pip install -r requirements-ai.txt
    call bs_env\Scripts\deactivate.bat
    set "AI_INSTALLED=1"
)

echo.
echo [4/5] Checking for ffmpeg ...
set "FFMPEG_PATH="
set "FFPROBE_PATH="

for /f "delims=" %%F in ('where ffmpeg 2^>nul') do if not defined FFMPEG_PATH set "FFMPEG_PATH=%%F"
for /f "delims=" %%F in ('where ffprobe 2^>nul') do if not defined FFPROBE_PATH set "FFPROBE_PATH=%%F"

if not defined FFMPEG_PATH (
    if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
        set "FFMPEG_PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"
        if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffprobe.exe" set "FFPROBE_PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links\ffprobe.exe"
    )
)
if not defined FFMPEG_PATH (
    if exist "C:\ProgramData\chocolatey\bin\ffmpeg.exe" (
        set "FFMPEG_PATH=C:\ProgramData\chocolatey\bin\ffmpeg.exe"
        if exist "C:\ProgramData\chocolatey\bin\ffprobe.exe" set "FFPROBE_PATH=C:\ProgramData\chocolatey\bin\ffprobe.exe"
    )
)

if defined FFMPEG_PATH (
    echo   OK: ffmpeg found at !FFMPEG_PATH!
) else (
    echo   ffmpeg was not found on PATH or in the usual install spots.
    set /p GETFF=Download ffmpeg now into tools\ffmpeg\, about 80 MB? [y/N]:
    if /I "!GETFF!"=="y" (
        echo   Downloading ffmpeg-release-essentials.zip from gyan.dev ...
        if not exist tools\ffmpeg mkdir tools\ffmpeg
        powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'tools\ffmpeg\ffmpeg.zip' } catch { exit 1 }"
        if errorlevel 1 (
            echo   Download failed. Get ffmpeg manually from https://www.gyan.dev/ffmpeg/builds/ and set it in Settings.
        ) else (
            powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path 'tools\ffmpeg\ffmpeg.zip' -DestinationPath 'tools\ffmpeg' -Force"
            del /q "tools\ffmpeg\ffmpeg.zip" >nul 2>nul
            for /f "delims=" %%D in ('dir /b /ad "tools\ffmpeg\ffmpeg-*" 2^>nul') do set "FFDIR=%%D"
            if defined FFDIR (
                if exist "tools\ffmpeg\!FFDIR!\bin\ffmpeg.exe" set "FFMPEG_PATH=%~dp0tools\ffmpeg\!FFDIR!\bin\ffmpeg.exe"
                if exist "tools\ffmpeg\!FFDIR!\bin\ffprobe.exe" set "FFPROBE_PATH=%~dp0tools\ffmpeg\!FFDIR!\bin\ffprobe.exe"
            )
            if defined FFMPEG_PATH (
                echo   OK: ffmpeg downloaded to !FFMPEG_PATH!
            ) else (
                echo   Could not locate ffmpeg.exe after extraction. Get it manually from
                echo   https://www.gyan.dev/ffmpeg/builds/ and set it in Settings.
            )
        )
    ) else (
        echo   Skipped. Get ffmpeg from https://www.gyan.dev/ffmpeg/builds/ and set it in
        echo   Settings, or re-run install.bat later to try the download again.
    )
)

echo.
echo [5/5] Creating desktop-style shortcut (Breakdown Studio.lnk) ...
powershell -NoProfile -ExecutionPolicy Bypass -File "_make_shortcut.ps1"

echo.
echo   Writing config.json ...
if not exist config.json (
    copy /y config.example.json config.json >nul
)

set "WORKER_PY=%~dp0bs_env\Scripts\python.exe"
set "TRANSNET_PY="
if defined AI_INSTALLED set "TRANSNET_PY=%WORKER_PY%"

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

bs_env\Scripts\python.exe "%CFGPY%" "%~dp0config.json" worker_python "%WORKER_PY%" transnet_python "%TRANSNET_PY%" ffmpeg "%FFMPEG_PATH%" ffprobe "%FFPROBE_PATH%"
del /q "%CFGPY%" >nul 2>nul

echo.
echo ============================================================================
echo  Done. Settings are pre-filled (config.json written).
echo  Double-click "Breakdown Studio.lnk", or run: python3 breakdown_studio.py
echo  Google OAuth client secret still needs to be set in Settings (see README).
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
