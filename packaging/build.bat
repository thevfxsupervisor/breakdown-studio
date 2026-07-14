@echo off
setlocal enabledelayedexpansion
rem build.bat - build the BreakdownStudio.exe one-folder distribution with PyInstaller.
rem
rem Run from anywhere; paths below are relative to this script's own location, so
rem "packaging\build.bat" works whether you're sitting in packaging\ or the app root.
rem
rem Base Python: set BS_BUILD_PYTHON to any Python 3.10+ interpreter before running this
rem script. If unset, it looks for "python" on PATH. A studio machine without a normal
rem Python install (e.g. only a vendor-bundled interpreter missing ensurepip) can still
rem build: this script creates the venv with --without-pip and bootstraps pip itself via
rem get-pip.py, so it does not require ensurepip to be present.
rem
rem The venv is created in packaging\build_env (gitignored). If your app folder lives on a
rem network drive (Dropbox, NAS, etc.), PyInstaller's analysis can be slow/unreliable there;
rem this script builds in a local temp directory and copies the result back automatically.

set "PKG_DIR=%~dp0"
set "APP_DIR=%PKG_DIR%.."
set "BUILD_ENV=%PKG_DIR%build_env"

if "%BS_BUILD_PYTHON%"=="" set "BS_BUILD_PYTHON=python"

echo === Breakdown Studio packaged build ===
echo App dir:    %APP_DIR%
echo Build env:  %BUILD_ENV%
echo Base python: %BS_BUILD_PYTHON%

if not exist "%BUILD_ENV%\Scripts\python.exe" (
    echo.
    echo [1/5] Creating build venv, bootstrapping pip separately ...
    "%BS_BUILD_PYTHON%" -m venv "%BUILD_ENV%" --without-pip
    if errorlevel 1 (
        echo ERROR: could not create venv with "%BS_BUILD_PYTHON%". Set BS_BUILD_PYTHON to a
        echo working Python 3.10+ interpreter and try again.
        exit /b 1
    )

    if not exist "%PKG_DIR%get-pip.py" (
        echo   downloading get-pip.py...
        powershell -NoProfile -Command ^
            "Invoke-WebRequest -Uri https://bootstrap.pypa.io/get-pip.py -OutFile '%PKG_DIR%get-pip.py'"
        if errorlevel 1 (
            echo ERROR: could not download get-pip.py. If this machine has no internet access,
            echo copy get-pip.py into packaging\ manually from a machine that does, or point
            echo BS_BUILD_PYTHON at an interpreter that already has pip.
            exit /b 1
        )
    )
    "%BUILD_ENV%\Scripts\python.exe" "%PKG_DIR%get-pip.py"
) else (
    echo.
    echo [1/5] Build venv already exists, reusing it.
)

echo.
echo [2/5] Installing build + runtime dependencies...
"%BUILD_ENV%\Scripts\python.exe" -m pip install --quiet --no-warn-script-location ^
    pyinstaller PySide6 Pillow numpy ^
    google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2
if errorlevel 1 (
    echo ERROR: pip install failed.
    exit /b 1
)

rem Build off the network drive if APP_DIR is not on a local fixed disk: PyInstaller's file
rem scanning is slow and occasionally flaky over SMB/Dropbox. %TEMP% is always local.
set "LOCAL_BUILD=%TEMP%\bs_pyinstaller_build"
echo.
echo [3/5] Building (workpath/distpath staged locally at %LOCAL_BUILD%)...
if exist "%LOCAL_BUILD%" rmdir /s /q "%LOCAL_BUILD%"
mkdir "%LOCAL_BUILD%"

"%BUILD_ENV%\Scripts\python.exe" -m PyInstaller "%PKG_DIR%breakdown_studio.spec" ^
    --distpath "%LOCAL_BUILD%\dist" ^
    --workpath "%LOCAL_BUILD%\work" ^
    --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    exit /b 1
)

echo.
echo [4/5] Copying result back to %APP_DIR%\dist ...
if exist "%APP_DIR%\dist\BreakdownStudio" rmdir /s /q "%APP_DIR%\dist\BreakdownStudio"
if not exist "%APP_DIR%\dist" mkdir "%APP_DIR%\dist"
robocopy "%LOCAL_BUILD%\dist\BreakdownStudio" "%APP_DIR%\dist\BreakdownStudio" /E /NFL /NDL /NJH /NJS
rem robocopy exit codes 0-7 are success; >=8 is a real error
if errorlevel 8 (
    echo ERROR: robocopy failed copying the build back.
    exit /b 1
)

echo.
echo [5/5] Done. Output: %APP_DIR%\dist\BreakdownStudio\BreakdownStudio.exe
exit /b 0
