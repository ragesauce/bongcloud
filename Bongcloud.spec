# -*- mode: python ; coding: utf-8 -*-

import os
import shutil

a = Analysis(
    ['review_app.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/sounds', 'assets/sounds')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='Bongcloud',
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

stockfish_src = os.environ.get('STOCKFISH_PATH') or shutil.which('stockfish')
if stockfish_src and os.path.isfile(stockfish_src):
    shutil.copy(stockfish_src, os.path.join(DISTPATH, 'stockfish.exe'))
else:
    print('WARNING: stockfish.exe not found (set STOCKFISH_PATH or add stockfish to PATH); '
          'packaged app will prompt for it at runtime.')
