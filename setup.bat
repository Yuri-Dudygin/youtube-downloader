@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Please install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist "venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv venv
) else (
    echo Virtual environment already exists.
)

echo Activating...
call venv\Scripts\activate.bat

echo Installing dependencies...
pip install --upgrade pip
pip install --upgrade yt-dlp yt-dlp-get-pot bgutil-ytdlp-pot-provider pytubefix imageio-ffmpeg nodejs-wheel-binaries

echo.
echo Setup complete! Use run.bat to download videos or run_subs.bat to download subtitles.
pause
