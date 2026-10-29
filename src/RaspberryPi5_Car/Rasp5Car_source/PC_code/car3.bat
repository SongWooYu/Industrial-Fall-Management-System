@echo off
chcp 65001 >nul
cd /d "%~dp0"

py car_tk_clientV3.py

if errorlevel 1 (
    echo.
    echo 프로그램 실행 중 오류가 발생했습니다.
    pause
)
