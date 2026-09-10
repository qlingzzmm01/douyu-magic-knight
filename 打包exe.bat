@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 打包需要系统 Python 3.14（自带 tkinter）；托管 Python 是 embeddable 版，无 tkinter

set PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe
if not exist "%PY%" set PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe
if not exist "%PY%" set PY=python

"%PY%" -m pip install --quiet pyinstaller
"%PY%" -m PyInstaller --noconfirm --distpath "魔法骑士自动挂机" --workpath "_build_tmp" knight_gui.spec
if exist "_build_tmp" rd /s /q "_build_tmp"

echo.
echo 产物：魔法骑士自动挂机\魔法骑士挂机助手.exe
pause
