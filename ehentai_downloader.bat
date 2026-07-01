@echo off
title E-Hentai Downloader
python -u "%~dp0ehentai_downloader.py" -i --out "%~dp0ehentai_cbz"
echo.
echo Done. If interrupted, re-run to resume.
pause
