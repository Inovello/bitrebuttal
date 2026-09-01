# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: Bit Rebuttal executables.

- Console EXE from packaging/pyi_entry.py on all platforms (named BitRebuttal
  on Linux, keeping the existing single-artifact behaviour).
- Windows and macOS additionally build a windowed EXE (console=False) from
  packaging/pyi_entry_gui.py named BitRebuttal; on macOS it is wrapped in a
  BitRebuttal.app bundle.
"""

import os
import sys

# Windows exes take the .ico; the macOS app bundle takes the .icns (EXE icon
# is a Windows-resource concept - pass None elsewhere).
icon_file = 'packaging/icon.ico' if sys.platform == 'win32' else None

datas = [('bitrebuttal/static', 'bitrebuttal/static')]

# Vendored aria2c (release.yml drops it into vendor/ before building) so users
# install nothing themselves. Optional: a build without vendor/ still works and
# falls back to PATH lookup at runtime (engine.resolve_aria2c).
binaries = []
_vendor_aria2 = os.path.join('vendor', 'aria2c.exe' if sys.platform == 'win32'
                             else 'aria2c')
if os.path.isfile(_vendor_aria2):
    binaries.append((_vendor_aria2, '.'))
    # aria2 is GPLv2 (with the OpenSSL linking exception): its license rides
    # along in every bundle that carries the binary.
    datas += [('packaging/aria2-COPYING', '.'),
              ('packaging/aria2-LICENSE.OpenSSL', '.')]
if sys.platform == 'darwin' and os.path.isdir('vendor'):
    # dylibbundler parks aria2c's non-system dylibs next to it (release.yml).
    binaries += [(os.path.join('vendor', f), '.')
                 for f in sorted(os.listdir('vendor')) if f.endswith('.dylib')]
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
        binaries=binaries,
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

    # Windowed native shell. Windows stays ONEFILE (one downloadable .exe);
    # macOS is ONEDIR wrapped in the .app - a onefile binary inside a bundle
    # re-extracts ~40MB on every launch and gets re-scanned by XProtect each
    # time, which made v1.1.0 take ages to open (and .apps ship zipped anyway,
    # so onedir costs the user nothing).
    a_gui = _analysis('packaging/pyi_entry_gui.py')
    pyz_gui = PYZ(a_gui.pure)

    if sys.platform == 'darwin':
        exe_gui = EXE(
            pyz_gui,
            a_gui.scripts,
            [],
            exclude_binaries=True,
            name='BitRebuttal',
            debug=False,
            strip=False,
            upx=False,
            console=False,
        )
        coll_gui = COLLECT(
            exe_gui,
            a_gui.binaries,
            a_gui.datas,
            strip=False,
            upx=False,
            name='BitRebuttal',
        )
        app = BUNDLE(
            coll_gui,
            name='BitRebuttal.app',
            icon='packaging/icon.icns',
            bundle_identifier='com.bitrebuttal.app',
            info_plist={'NSHighResolutionCapable': True},
        )
    else:
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
