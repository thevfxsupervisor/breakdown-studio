@echo off
rem Create the TransNetV2 detection venv. This is large (torch). For GPU, install the matching
rem CUDA torch wheel from https://pytorch.org BEFORE running this, or edit the line below.
cd /d "%~dp0"
python -m venv transnet_env || (echo Could not create venv & pause & exit /b 1)
call transnet_env\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-transnet.txt
echo.
echo TransNetV2 Python is:  %~dp0transnet_env\Scripts\python.exe
echo Put that path in Breakdown Studio -> Settings -> "TransNetV2 Python".
pause
