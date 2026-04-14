@echo off
echo ========================================
echo    PLUXO Local Development Setup
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed!
    echo Please install Python from https://python.org
    pause
    exit /b 1
)

echo [1/3] Installing Python dependencies...
pip install flask flask-cors gunicorn python-dotenv >nul 2>&1

echo [2/3] Starting API Server (port 5000) — same as Railway (main.py)...
start "PLUXO API" cmd /k "python main.py"

echo [3/3] Starting Web Server (port 8080)...
timeout /t 2 >nul
start "PLUXO Web" cmd /k "python -m http.server 8080"

echo.
echo ========================================
echo    All services started!
echo ========================================
echo.
echo API Server:  http://localhost:5000
echo Website:     http://localhost:8080/index%%20(27).html
echo.
echo Test API: http://localhost:5000/health
echo.
echo Keep both command windows open!
echo.
pause
