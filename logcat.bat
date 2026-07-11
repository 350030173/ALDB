@echo off
chcp 65001 >nul
title Logcat Viewer
color 0A

echo ==========================================
echo   Logcat Viewer Launcher
echo ==========================================
echo.

REM Set Python path (auto-detect or manual)
set "PYTHON_PATH=python"


:found_python
echo [INFO] Using Python: %PYTHON_PATH%
echo.

REM Get script directory
set "SCRIPT_DIR=%~dp0"
set "SERVER_PY=%SCRIPT_DIR%logcat_server.py"
set "VIEWER_HTML=%SCRIPT_DIR%logcat_terminal.html"

REM Check if files exist
if not exist "%SERVER_PY%" (
    echo [ERROR] logcat_server.py not found
    echo [ERROR] Please ensure logcat_server.py is in the same directory
    pause
    exit /b 1
)

if not exist "%VIEWER_HTML%" (
    echo [WARN] logcat_terminal.html not found, trying logcat_viewer.html
    set "VIEWER_HTML=%SCRIPT_DIR%logcat_viewer.html"
    if not exist "%VIEWER_HTML%" (
        echo [ERROR] No HTML file found
        pause
        exit /b 1
    )
)

echo [INFO] Server: %SERVER_PY%
echo [INFO] Browser: %VIEWER_HTML%
echo.

REM Start Python server (background)
echo [INFO] Starting server...
start "Logcat Server" /min cmd /c "cd /d "%SCRIPT_DIR%" && "%PYTHON_PATH%" "%SERVER_PY%""

REM Wait for server to start
timeout /t 2 /nobreak >nul

echo [INFO] Server started
echo.

REM Open browser
echo [INFO] Opening browser...
start "" "%VIEWER_HTML%"

echo.
echo ==========================================
echo   Logcat Viewer Started
echo ==========================================
echo.
echo Server running at: ws://localhost:8765
echo Browser opened
echo.
echo Press any key to close this window (server keeps running)
pause >nul
