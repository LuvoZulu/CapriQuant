@echo off
REM Simple .bat wrapper for NSSM or Task Scheduler
REM Adjust the path below to where your python-backend lives on the EC2 / machine

cd /d "C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend"

REM Weekday guard is inside the python launcher, but you can also add one here if desired.
python service\run_as_windows_service.py
pause
