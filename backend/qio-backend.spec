# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('src/agent/tool_worker.py', 'agent')]
binaries = []
# agent.interactive.materials：understanding.py 用 importlib.import_module 取它，PyInstaller 的
# 静态分析看不见动态导入（0.2.0 安装包整个漏了这个模块 → 每次理解都 materials=[] + 诚实报错）。
# 与 scripts/build_sidecar.ps1 的 --hidden-import 保持一致；删掉会让 check_frozen_materials.py 失败。
hiddenimports = ['keyring.backends.Windows', 'agent.tool_worker', 'agent.interactive.materials', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on']
tmp_ret = collect_all('tiktoken')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['src/agent/main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='qio-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
