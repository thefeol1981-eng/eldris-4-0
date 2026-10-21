@echo off
REM Publish ELDRIS 3.0 to a free public HTTPS address (Cloudflare quick tunnel).
cd /d "%~dp0"
python tools\publish_free.py %*
echo.
pause
