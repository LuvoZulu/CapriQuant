@echo off
REM CapriQuant Backend Service Wrapper
REM This is meant to be run by NSSM as a Windows Service (auto-start on boot).
REM Weekday-only logic + uvicorn watchdog lives inside the Python script.

cd /d "%~dp0.."

python "%~dp0run_as_windows_service.py"
