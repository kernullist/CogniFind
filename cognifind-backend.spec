# -*- mode: python ; coding: utf-8 -*-

import os
import importlib

block_cipher = None

hidden_imports = [
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'fastapi',
    'pydantic',
    'starlette',
    'starlette.middleware',
    'starlette.middleware.cors',
    'anyio',
    'anyio._backends',
    'anyio._backends._asyncio',
    'h11',
    'httptools',
    'onnxruntime',
    'sqlite_vec',
    'tokenizers',
    'huggingface_hub',
    'requests',
    'certifi',
    'lxml',
    'lxml.etree',
    'lxml._elementpath',
    'numpy',
    'pypdf',
    'docx',
    'openpyxl',
    'watchdog',
    'watchdog.observers',
    'watchdog.events',
    'PySide6.QtCore',
    'PySide6.QtWidgets',
    'PySide6.QtGui',
    'src',
    'src.config',
    'src.database',
    'src.embedding',
    'src.parser',
    'src.watcher',
]

datas = []

try:
    import onnxruntime
    ort_path = os.path.dirname(onnxruntime.__file__)
    datas.append((ort_path, 'onnxruntime'))
except ImportError:
    pass

try:
    import sqlite_vec
    vec_path = os.path.dirname(sqlite_vec.__file__)
    datas.append((vec_path, 'sqlite_vec'))
except ImportError:
    pass

try:
    import tokenizers
    tok_path = os.path.dirname(tokenizers.__file__)
    datas.append((tok_path, 'tokenizers'))
except ImportError:
    pass

a = Analysis(
    ['api.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'scipy', 'PIL', 'tkinter', 'unittest', 'test',
        'torch', 'torchvision', 'torchaudio', 'torchtext',
        'tensorflow', 'keras',
        'sklearn', 'scikit-learn',
        'pandas', 'plotly', 'bokeh',
        'IPython', 'jupyter', 'notebook',
        'pytest', 'pip', 'wheel',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='cognifind-backend',
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
