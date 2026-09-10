# -*- mode: python ; coding: utf-8 -*-
"""魔法骑士挂机助手 · PyInstaller 打包配置（单文件 exe，含 Playwright driver）"""
import os
from PyInstaller.utils.hooks import collect_submodules
import playwright

PW_DIR = os.path.dirname(playwright.__file__)
DRIVER = os.path.join(PW_DIR, "driver")          # node.exe + package/cli.js（约 100MB）

datas = [(DRIVER, os.path.join("playwright", "driver"))]
hiddenimports = ["playwright", "playwright.sync_api", "playwright.async_api", "greenlet"]
hiddenimports += collect_submodules("playwright")

a = Analysis(
    ["knight_gui.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pandas", "PIL", "scipy", "pytest",
              "IPython", "notebook", "tkinter.test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="魔法骑士挂机助手",
    debug=False,
    strip=False,
    upx=False,
    console=False,                 # 纯 GUI，不弹黑色控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
