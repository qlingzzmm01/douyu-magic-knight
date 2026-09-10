@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先跑打包好的 exe；没有就用系统 Python 3.14 跑源码
rem （tkinter 仅存在于系统 Python 3.14；托管 Python 是 embeddable 版，没有 tkinter）
if exist "魔法骑士自动挂机\魔法骑士挂机助手.exe" (
    start "" "%~dp0魔法骑士自动挂机\魔法骑士挂机助手.exe"
    exit /b
)

set PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe
if not exist "%PY%" set PY=python
"%PY%" knight_gui.py
if errorlevel 1 pause
