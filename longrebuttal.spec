# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: Long Rebuttal as a single executable."""

a = Analysis(
    ['packaging/pyi_entry.py'],
    pathex=[],
    datas=[('longrebuttal/static', 'longrebuttal/static')],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LongRebuttal',
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
