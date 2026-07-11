# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['desktop_app\\app.py'],
    pathex=[],
    binaries=[],
    datas=[('desktop_app/index.html', 'desktop_app'), ('config', 'config'), ('saved_models', 'saved_models')],
    hiddenimports=['uvicorn', 'fastapi', 'pydantic', 'sklearn', 'numpy', 'pandas', 'joblib', 'yaml', 'pipeline', 'pipeline.collectors.daily_collector', 'data_collector', 'lightgbm', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebChannel', 'database'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'matplotlib', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='哨响AI_操盘终端',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
