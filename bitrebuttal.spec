# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: Bit Rebuttal executables.

- Console EXE from packaging/pyi_entry.py on all platforms (named BitRebuttal
  on Linux, keeping the existing single-artifact behaviour).
- Windows and macOS additionally build a windowed EXE (console=False) from
  packaging/pyi_entry_gui.py named BitRebuttal; on macOS it is wrapped in a
  BitRebuttal.app bundle.
"""

import sys

# Windows exes take the .ico; the macOS app bundle takes the .icns (EXE icon
# is a Windows-resource concept - pass None elsewhere).
icon_file = 'packaging/icon.ico' if sys.platform == 'win32' else None

datas = [('bitrebuttal/static', 'bitrebuttal/static')]
hiddenimports = [
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
]


def _analysis(entry_script):
    return Analysis(
        [entry_script],
        pathex=[],
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        runtime_hooks=[],
        excludes=[],
    )


if sys.platform == 'linux':
    a = _analysis('packaging/pyi_entry.py')
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='BitRebuttal',
        debug=False,
        strip=False,
        upx=False,
        console=True,
    )
else:
    # Console CLI (all non-Linux platforms).
    a_cli = _analysis('packaging/pyi_entry.py')
    pyz_cli = PYZ(a_cli.pure)
    exe_cli = EXE(
        pyz_cli,
        a_cli.scripts,
        a_cli.binaries,
        a_cli.datas,
        [],
        name='bitrebuttal-cli',
        debug=False,
        strip=False,
        upx=False,
        console=True,
        icon=icon_file,
    )

    # Windowed native shell (Windows + macOS).
    a_gui = _analysis('packaging/pyi_entry_gui.py')
    pyz_gui = PYZ(a_gui.pure)
    exe_gui = EXE(
        pyz_gui,
        a_gui.scripts,
        a_gui.binaries,
        a_gui.datas,
        [],
        name='BitRebuttal',
        debug=False,
        strip=False,
        upx=False,
        console=False,
        icon=icon_file,
    )

    if sys.platform == 'darwin':
        app = BUNDLE(
            exe_gui,
            name='BitRebuttal.app',
            icon='packaging/icon.icns',
            bundle_identifier='com.bitrebuttal.app',
            info_plist={'NSHighResolutionCapable': True},
        )
