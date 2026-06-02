"""
CapriQuant Backend Windows Service / Weekday Runner

Preferred way (simple & reliable):
    Use NSSM (https://nssm.cc/) to register this script or a .bat that calls it.

    Example NSSM command (run as Administrator):
        nssm install CapriQuantBackend "C:\path\to\python.exe" "C:\path\to\this\file.py"

    Or point NSSM at a small .bat:
        @echo off
        cd /d "C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend"
        python -m uvicorn main:app --host 0.0.0.0 --port 8000

The script below adds:
- Weekday-only guard (Mon-Fri only). On weekends it sleeps or exits.
- Graceful handling so the service doesn't look "crashed".
- You can also run it directly for testing: python run_as_windows_service.py

If you prefer a pure Python Windows Service (no NSSM), see the commented Service class at the bottom.
You will need: pip install pywin32
"""

import os
import sys
import time
import datetime
import subprocess
from pathlib import Path

# =============================================================================
# CONFIG - ADJUST TO YOUR MACHINE
# =============================================================================
BACKEND_DIR = Path(__file__).resolve().parent.parent   # python-backend/
PYTHON_EXE = sys.executable
UVICORN_CMD = [PYTHON_EXE, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

WEEKDAY_ONLY = True          # Set False if you want 24/7 even on weekends
WEEKEND_SLEEP_HOURS = 8      # On weekend, sleep this long between checks
CHECK_INTERVAL_SEC = 30      # How often the weekday guard wakes up


def is_weekday() -> bool:
    # Monday=0 ... Friday=4
    return datetime.datetime.now().weekday() < 5


def run_backend_forever():
    """Start uvicorn and restart it if it dies (simple watchdog)."""
    print("[CapriQuantService] Starting backend...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND_DIR)

    while True:
        if WEEKDAY_ONLY and not is_weekday():
            print("[CapriQuantService] Weekend detected - sleeping...")
            time.sleep(WEEKEND_SLEEP_HOURS * 3600)
            continue

        try:
            print(f"[CapriQuantService] Launching uvicorn: {' '.join(UVICORN_CMD)}")
            proc = subprocess.Popen(UVICORN_CMD, cwd=str(BACKEND_DIR), env=env)
            proc.wait()
            print(f"[CapriQuantService] uvicorn exited with code {proc.returncode}. Restarting in 5s...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("[CapriQuantService] Keyboard interrupt - shutting down.")
            break
        except Exception as e:
            print(f"[CapriQuantService] Error: {e}. Restarting in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    print("=" * 60)
    print("CapriQuant Backend Service Launcher")
    print(f"Weekdays only: {WEEKDAY_ONLY}")
    print(f"Backend dir : {BACKEND_DIR}")
    print("=" * 60)
    run_backend_forever()


# =============================================================================
# OPTIONAL: Pure Python Windows Service (requires pywin32)
# =============================================================================
#
# To use this instead of / in addition to NSSM:
#
# 1. pip install pywin32
# 2. Run once as admin to register:
#       python run_as_windows_service.py install
# 3. Start / stop via services.msc or:
#       python run_as_windows_service.py start
#       python run_as_windows_service.py stop
#
# Uncomment the block below if you want this.
#
# import servicemanager
# import win32event
# import win32service
# import win32serviceutil
#
# class CapriQuantService(win32serviceutil.ServiceFramework):
#     _svc_name_ = "CapriQuantBackend"
#     _svc_display_name_ = "CapriQuant Backend (Structure Engine)"
#     _svc_description_ = "Real-time SMC structure analysis + signal engine. Weekdays only."
#
#     def __init__(self, args):
#         win32serviceutil.ServiceFramework.__init__(self, args)
#         self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
#         self.process = None
#
#     def SvcStop(self):
#         self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
#         win32event.SetEvent(self.hWaitStop)
#         if self.process:
#             self.process.terminate()
#
#     def SvcDoRun(self):
#         servicemanager.LogMsg(
#             servicemanager.EVENTLOG_INFORMATION_TYPE,
#             servicemanager.PYS_SERVICE_STARTED,
#             (self._svc_name_, "")
#         )
#         self.main()
#
#     def main(self):
#         # Re-use the same weekday logic
#         run_backend_forever()
#
#
# if __name__ == '__main__':
#     if len(sys.argv) > 1 and sys.argv[1] in ['install', 'remove', 'start', 'stop']:
#         win32serviceutil.HandleCommandLine(CapriQuantService)
#     else:
#         run_backend_forever()
