@echo off
title Biz Linkedin Scraper - Public Cloudflare Live
color 0a

echo ========================================================
echo         Biz Linkedin Scraper (Public Live Mode)
echo ========================================================
echo.

:: Start Flask app in background
start /B python app.py

echo Starting Cloudflare Tunnel...
echo Generating your live public HTTPS link...
echo.

:: Start Cloudflare tunnel
.\cloudflared.exe tunnel --url http://127.0.0.1:5000
pause
