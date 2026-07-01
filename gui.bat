@echo off
chcp 65001 >nul
title E-Hentai Downloader (GUI)
python -u "%~dp0gui.py"
if errorlevel 1 (
    echo.
    echo 启动失败，请确保已安装依赖：
    echo pip install requests PyQt5
    pause
)
