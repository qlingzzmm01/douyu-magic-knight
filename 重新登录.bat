@echo off
chcp 65001 >nul
cd /d %~dp0
echo 打开斗鱼登录（如遇未登录提示，扫码后重跑挂机）
"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" probe\login3.py
pause
