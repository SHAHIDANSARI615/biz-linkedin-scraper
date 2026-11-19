@echo off
title Biz Linkedin Scraper
color 0b

echo ========================================================
echo               Biz Linkedin Scraper
echo ========================================================
echo.

:: Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not added to PATH!
    echo Please install Python 3.10 or newer from https://www.python.org
    pause
    exit /b
)

echo Checking and installing required packages...
pip install -r requirements.txt --quiet

echo.
echo Launching Biz Linkedin Scraper...
echo Opening browser at http://localhost:5000 ...
start http://localhost:5000

python app.py
pause
