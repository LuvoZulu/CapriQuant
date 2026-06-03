@echo off
REM CapriQuant Backend Service Wrapper
REM This is meant to be run by NSSM as a Windows Service (auto-start on boot).
REM Weekday-only logic + uvicorn watchdog lives inside the Python script.

cd /d "C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend"

python service\run_as_windows_service.py
