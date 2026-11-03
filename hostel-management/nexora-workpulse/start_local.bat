@echo off
REM NEXORA WORKPULSE - one-click local start for Windows. Double-click this file.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Python was not found. Install Python 3.11 or newer from python.org and tick "Add Python to PATH".
  pause & exit /b 1
)
call ".venv\Scripts\activate.bat"
echo Installing requirements (first time takes a minute)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 ( echo Install failed. Check your internet connection. & pause & exit /b 1 )
python app.py
pause
