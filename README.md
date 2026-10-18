# ClipOrbit Desktop
*One Link. One Hub. Your Media.* - A MUdevelops Project, v1.0.0

Windows 10/11 media downloader built with **Python 3.11.9**, PySide6 and yt-dlp. Everything lives in `main.py`.

## Setup and launch
1. Install Python 3.11.9 (tick "Add python.exe to PATH").
2. Double-click `start.bat` (installs `PySide6` and `yt-dlp` on first run), or run:
   ```
   py -3.11 -m pip install -r requirements.txt
   py -3.11 main.py
   ```

## FFmpeg (recommended)
Needed to merge separate video+audio streams (highest resolutions) and for MP3 conversion.
- `winget install Gyan.FFmpeg`, then restart ClipOrbit, **or**
- download a build from https://ffmpeg.org, then add its `bin` folder to PATH or put `ffmpeg.exe` next to `main.py` / `ClipOrbit.exe`.

Without FFmpeg the app still works but only downloads pre-merged streams (often lower resolution).

## Packaging
```
build.bat
```
Runs `pyinstaller --onefile --windowed --collect-all yt_dlp main.py` and produces `dist\ClipOrbit.exe`.

## Data location
Settings and history: `%APPDATA%\MUdevelops\ClipOrbit Desktop\cliporbit.db`. No telemetry.

## Known limitations
- Platform support depends on yt-dlp; some posts need logins, are private, DRM-protected or region-locked. ClipOrbit does not bypass these.
- Single videos only (playlist links download the first entry).
- Pause/resume is not offered; cancelled or failed jobs can be retried and resume partial files.
- Cancelling during FFmpeg merging/conversion takes effect after that step.
- Keep yt-dlp current (`pip install -U yt-dlp`) as sites change often.
- Use only for content you own or are authorized to download.
