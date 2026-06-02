@echo off
REM Run the CapriQuant UI/Visualizer (Streamlit)
REM Backend is on 8001, so the dashboard connects to http://127.0.0.1:8001
cd /d "C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend"

REM Run on default Streamlit port (8501) or specify another if you want
python -m streamlit run ui\dashboard.py --server.address 127.0.0.1

pause
