@echo off
REM ============================================================
REM CapriQuant Backend - Stop the Windows Service
REM Run this as Administrator!
REM ============================================================

echo.
echo === Stopping CapriQuant Backend Service ===
echo.

set "SERVICE_NAME=CapriQuantBackend"
set "NSSM_PATH=C:\Tools\nssm\nssm.exe"

echo Attempting to stop service '%SERVICE_NAME%' ...

"%NSSM_PATH%" stop %SERVICE_NAME%

if errorlevel 1 (
    echo.
    echo NSSM stop command returned an error.
    echo Trying alternative methods...
    echo.
    
    sc stop %SERVICE_NAME%
    
    if errorlevel 1 (
        echo.
        echo Could not stop via sc either.
        echo.
        echo Please try manually:
        echo 1. Press Win + R, type services.msc and press Enter
        echo 2. Find "CapriQuantBackend" in the list
        echo 3. Right-click it and select "Stop"
    ) else (
        echo.
        echo Service stop requested via sc.exe
    )
) else (
    echo.
    echo Service stop requested successfully via NSSM.
)

echo.
echo You can verify the status with:
echo   "%NSSM_PATH%" status %SERVICE_NAME%
echo   or
echo   sc query %SERVICE_NAME%
echo.
pause
