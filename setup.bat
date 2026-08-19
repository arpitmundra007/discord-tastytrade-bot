@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   Discord to Tastytrade Bot - First-Time Setup
echo ============================================
echo.

REM ---------- Step 1: find or install Python ----------
where python >nul 2>nul
if errorlevel 1 goto :no_python
goto :check_real_python

:no_python
echo Python was not found on this computer.
where winget >nul 2>nul
if errorlevel 1 (
    echo winget isn't available either, so this can't be installed automatically.
    echo.
    echo Please install Python yourself first:
    echo   1. Go to https://www.python.org/downloads/
    echo   2. Download and run the installer
    echo   3. IMPORTANT: check "Add python.exe to PATH" during install
    echo   4. Run this setup.bat again afterward
    echo.
    pause
    exit /b 1
)

echo Attempting to install Python 3.12 automatically via winget...
echo ^(this may take a few minutes and might show its own progress window^)
echo.
winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo.
    echo Automatic install didn't complete successfully.
    echo Please install Python manually from https://www.python.org/downloads/
    echo ^(check "Add python.exe to PATH" during install^), then run setup.bat again.
    echo.
    pause
    exit /b 1
)

echo.
echo Python was installed. Windows needs a FRESH window to pick up the
echo updated PATH before "python" will work - this window can't continue
echo automatically past this point.
echo.
echo Please CLOSE this window, then double-click setup.bat again to continue.
echo.
pause
exit /b 0

:check_real_python
REM "python" being found doesn't guarantee it's a real install - Windows
REM sometimes intercepts it with a Microsoft Store stub instead.
python --version 2>&1 | findstr /B "Python" >nul
if errorlevel 1 (
    echo.
    echo "python" was found but doesn't look like a real Python install.
    echo This usually means Windows' "App execution alias" for python.exe is
    echo intercepting the command instead of a real install ^(or opening the
    echo Microsoft Store^) - a common gotcha, not something wrong with this
    echo project.
    echo.
    echo Fix: Settings -^> Apps -^> Advanced app settings -^> App execution
    echo      aliases -^> turn OFF the python.exe / python3.exe entries there.
    echo Then run setup.bat again.
    echo.
    pause
    exit /b 1
)

echo Found:
python --version
echo.

REM ---------- Step 2: create the virtual environment ----------
if not exist venv\Scripts\activate.bat (
    echo Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo.
        echo Failed to create the virtual environment - see the error above.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists, skipping creation.
)

call venv\Scripts\activate.bat

REM ---------- Step 3: install dependencies ----------
echo.
echo Installing dependencies - this can take a few minutes the first time...
echo.
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed - see the error above.
    pause
    exit /b 1
)

REM ---------- Step 4: verify everything actually works ----------
echo.
echo Verifying everything installed correctly...
echo.
python check_setup.py
if errorlevel 1 (
    echo.
    echo Setup ran, but the environment check found a problem - see above
    echo for what to fix. Run setup.bat again once it's addressed.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Setup complete!
echo ============================================
echo.
set /p LAUNCH="Start the bot now? (Y/N): "
if /i "%LAUNCH%"=="Y" (
    call start.bat
) else (
    echo.
    echo You can start it any time by double-clicking start.bat.
    echo.
    pause
)
