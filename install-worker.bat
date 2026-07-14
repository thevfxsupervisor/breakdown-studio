@echo off
rem Create a self-contained worker venv (Pillow + numpy) next to the app.
cd /d "%~dp0"
python -m venv bs_env || (echo Could not create venv & pause & exit /b 1)
call bs_env\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-worker.txt
echo.
echo Worker Python is:  %~dp0bs_env\Scripts\python.exe
echo Put that path in Breakdown Studio -> Settings -> "Worker Python".
pause
