@echo off
REM ============================================================
REM CapriQuant Backend - Install as Windows Service (NSSM)
REM Run this as Administrator!
REM ============================================================

echo.
echo === CapriQuant Backend Service Installer ===
echo.
echo This will register the backend as a Windows Service that:
echo   - Starts automatically with Windows
echo   - Runs only on weekdays (Mon-Fri) - see run_as_windows_service.py
echo   - Restarts automatically if it crashes
echo   - Listens on port 8001 (for MT5 EA and UI)
echo.

set "SERVICE_NAME=CapriQuantBackend"
set "NSSM_PATH=C:\Tools\nssm\nssm.exe"
set "BAT_PATH=%~dp0start_capriquant.bat"
set "APP_DIR=%~dp0.."

echo Current settings:
echo   Service Name : %SERVICE_NAME%
echo   NSSM         : %NSSM_PATH%
echo   Wrapper BAT  : %BAT_PATH%
echo   App Dir      : %APP_DIR%
echo.

echo If these paths are wrong, EDIT THIS FILE before running!
echo.

pause

echo.
echo [1/5] Installing service with NSSM...
"%NSSM_PATH%" install %SERVICE_NAME% "%BAT_PATH%"

if errorlevel 1 (
    echo ERROR: NSSM install failed. Is NSSM installed at %NSSM_PATH% ?
    echo Download from https://nssm.cc/
    pause
    exit /b 1
)

echo.
echo [2/5] Setting AppDirectory...
"%NSSM_PATH%" set %SERVICE_NAME% AppDirectory "%APP_DIR%"

echo.
echo [3/5] Setting auto-start...
"%NSSM_PATH%" set %SERVICE_NAME% Start SERVICE_AUTO_START

echo.
echo [4/5] Configuring logging (optional but recommended)...
mkdir "%APP_DIR%\logs" 2>nul
"%NSSM_PATH%" set %SERVICE_NAME% AppStdout "%APP_DIR%\logs\service.log"
"%NSSM_PATH%" set %SERVICE_NAME% AppStderr "%APP_DIR%\logs\service_error.log"

echo.
echo [5/5] Starting the service...
"%NSSM_PATH%" start %SERVICE_NAME%

echo.
echo === DONE ===
echo.
echo Service '%SERVICE_NAME%' installed and started.
echo.
echo Verify:
echo   - Open http://127.0.0.1:8001 in browser
echo   - Check services.msc for CapriQuantBackend
echo   - Your MT5 EA should now get realtime signals on 8001
echo.
echo To stop:   Double-click python-backend\service\stop_service.bat (as Admin)
echo            or run: nssm stop %SERVICE_NAME%
echo To remove: nssm remove %SERVICE_NAME% confirm
echo.
pause
