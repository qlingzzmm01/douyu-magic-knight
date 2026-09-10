@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   Douyu Magic Knight Auto Bot
echo   Ctrl+C 停止
echo ============================================
"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" bot.py %*
pause
