@echo off
rem Windows: double-click to start Image Optimizer.
rem Works from any location. First run (or after moving the folder) sets up Python + Pillow once.
cd /d "%~dp0"
".venv\Scripts\python.exe" -c "import PIL" >nul 2>&1
if errorlevel 1 (
  echo Setting up ^(one time only, needs internet^)...
  if exist ".venv" rmdir /s /q ".venv"
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" ( echo Python 3.9+ is required: https://www.python.org/downloads/ & pause & exit /b 1 )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q || ( echo Setup failed. & pause & exit /b 1 )
)
".venv\Scripts\python.exe" app.py
pause
