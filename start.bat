@echo off
REM Double-click this file (or run "start.bat" in cmd) to launch the bot.
REM Combines venv activation + environment check + app start into one step.

cd /d "%~dp0"

if not exist venv\Scripts\activate.bat (
    echo.
    echo No virtual environment found in this folder.
    echo Run the one-time setup first - see README.md "Setup guide", step 1.
    echo.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo Checking environment...
python check_setup.py
if errorlevel 1 (
    echo.
    echo Environment check failed - see above for what to fix.
    echo Once fixed, just run start.bat again.
    echo.
    pause
    exit /b 1
)

echo.
echo Starting the bot - dashboard will be at http://localhost:8000
echo Close this window (or Ctrl+C) to stop it.
echo.
python run.py

REM Keep the window open if run.py exits unexpectedly, so any error is visible
echo.
echo The app stopped. If that was unexpected, scroll up for the error.
pause
