@echo off
REM Simple helper to run the CapriQuant backend locally (no service)
cd /d "C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend"

echo Starting CapriQuant backend on http://127.0.0.1:8001 ...
echo (Press Ctrl+C to stop)
echo.

python -m uvicorn main:app --reload --port 8001
pause
