@echo off
setlocal
cd /d "%~dp0"
set "PY="
py -3.11 -c "import sys;sys.exit(0 if sys.version_info[:3]==(3,11,9) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3.11"
if not defined PY (
    python -c "import sys;sys.exit(0 if sys.version_info[:3]==(3,11,9) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ClipOrbit] Python 3.11.9 was not found. Install it first.
    pause
    exit /b 1
)
%PY% -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [ClipOrbit] Dependency installation failed.
    pause
    exit /b 1
)
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name ClipOrbit --collect-all yt_dlp main.py
if errorlevel 1 (
    echo [ClipOrbit] Build failed. See the messages above.
    pause
    exit /b 1
)
echo.
echo Build finished: dist\ClipOrbit.exe
echo Place ffmpeg.exe next to ClipOrbit.exe or install FFmpeg on PATH.
pause
endlocal
