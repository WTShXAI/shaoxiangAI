# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for 哨响AI 操盘手辅助决策终端
================================================
打包为单文件 exe, 内置 bridge_service + 前端 HTML + 所有依赖

构建命令:
    cd D:\Architecture
    pyinstaller desktop_app/app.spec --clean --noconfirm

产物:
    dist/哨响AI_操盘终端.exe
"""

import sys, os
from pathlib import Path

# 绝对路径基础
PROJECT_ROOT = r'D:\Architecture'
DESKTOP_DIR = os.path.join(PROJECT_ROOT, 'desktop_app')

a = Analysis(
    [os.path.join(DESKTOP_DIR, 'app.py')],
    pathex=[PROJECT_ROOT, DESKTOP_DIR],
    binaries=[],
    datas=[
        # 桌面端 HTML
        (os.path.join(DESKTOP_DIR, 'index.html'), 'desktop_app'),
        # 核心模块
        (os.path.join(PROJECT_ROOT, 'bridge_service.py'), '.'),
        # 配置文件
        (os.path.join(PROJECT_ROOT, 'config'), 'config'),
        # saved_models (预测模型权重)
        (os.path.join(PROJECT_ROOT, 'saved_models'), 'saved_models'),
        # 数据目录 — 只打包主库, 排除备份/大文件 (否则exe会膨胀到1.9GB+)
        # 用 Tree 的 excludes 过滤, 或改为只打包 football_data.db
        # 这里用 PyInstaller Tree 的 excludes 机制
        # (os.path.join(PROJECT_ROOT, 'data'), 'data'),  ← 旧: 打包全部(含4.6GB备份)
        # 新: 只打包主库 + api_cache(赛程缓存)
        (os.path.join(PROJECT_ROOT, 'data', 'football_data.db'), 'data'),
        (os.path.join(PROJECT_ROOT, 'data', 'bets.db'), 'data'),
        # pipeline
        (os.path.join(PROJECT_ROOT, 'pipeline'), 'pipeline'),
        # data_collector
        (os.path.join(PROJECT_ROOT, 'data_collector'), 'data_collector'),
        # models
        (os.path.join(PROJECT_ROOT, 'models'), 'models'),
        # bookmaker_sim
        (os.path.join(PROJECT_ROOT, 'bookmaker_sim'), 'bookmaker_sim'),
    ],
    hiddenimports=[
        # FastAPI / uvicorn
        'uvicorn', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.protocols',
        'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan', 'uvicorn.lifespan.on',
        'fastapi', 'starlette', 'pydantic',
        # pipeline
        'pipeline', 'pipeline.engine', 'pipeline.wc_engine', 'pipeline.league_engine',
        'pipeline.score_model', 'pipeline.reverse_odds_engine', 'pipeline.collectors',
        'pipeline.collectors.daily_collector', 'pipeline.collectors.sp_odds_api',
        # data_collector
        'data_collector', 'data_collector.football_data_live',
        # ML
        'sklearn', 'sklearn.ensemble', 'sklearn.linear_model',
        'xgboost', 'lightgbm',
        'numpy', 'pandas', 'scipy', 'joblib', 'sqlite3',
        # config
        'yaml',
        # general
        'logging', 'json', 'threading', 'socket', 'webbrowser',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'test', 'unittest', 'pytest',
        'matplotlib', 'IPython', 'jupyter',
        'torch', 'torchvision', 'torchaudio',
        'tensorflow', 'keras',
        'tkinter',
        # 排除 CUDA/nvidia DLL (torch 残留, 可省数百MB)
        'nvidia', 'cuda', 'cublas', 'cudnn',
        # 排除 notebook/jupyter 相关
        'notebook', 'ipykernel', 'ipywidgets',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# 图标 (如果存在)
icon_path = os.path.join(PROJECT_ROOT, 'browser_extension', 'icon128.png')
if not os.path.exists(icon_path):
    icon_path = None

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
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)
