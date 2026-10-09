# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

sys.path.insert(0, SPECPATH)
from build_support import project_datas

block_cipher = None

hidden_imports = [
    # 标准库动态调用
    'queue',
    'concurrent.futures',
    'concurrent.futures.thread',
    'platform',
    'subprocess',
    'dataclasses',
    'enum',
    # 四大子模块核心入口与工具
    'chaoxing.main',
    'chaoxing.api.base',
    'chaoxing.api.answer',
    'chaoxing.api.answer_check',
    'chaoxing.api.cipher',
    'chaoxing.api.config',
    'chaoxing.api.cookies',
    'chaoxing.api.cxsecret_font',
    'chaoxing.api.decode',
    'chaoxing.api.exceptions',
    'chaoxing.api.font_decoder',
    'chaoxing.api.live',
    'chaoxing.api.live_process',
    'chaoxing.api.logger',
    'chaoxing.api.notification',
    'chaoxing.api.exam',
    'chaoxing.api.exam_runner',
    'chaoxing.api.progress',
    'chaoxing.api.online',
    'welearn.welearn_decompiled',
    'zhs.main',
    'zhs.fucker',
    'zhs.logger',
    'zhs.push',
    'zhs.sign',
    'zhs.utils',
    'zhs.zd_utils',
    'zhs.ObjDict',
    'yuketang.main',
    'yuketang.yuketang_login',
    # 核心业务第三方依赖
    'requests',
    'urllib3',
    'pyaes',
    'Crypto',
    'Crypto.Cipher.AES',
    'bs4',
    'lxml',
    'loguru',
    'fontTools',
    'PIL',
    'PIL.Image',
    'PIL.ImageOps',
    'httpx',
    'openai',
    'tiktoken',
    'ddddocr',
]

hidden_imports += collect_submodules('Crypto')
hidden_imports += collect_submodules('pyaes')
hidden_imports += collect_submodules('loguru')

datas = project_datas(Path(SPECPATH))

datas += collect_data_files('tiktoken')

excludes = [
    'torch',
    'scipy',
    'pandas',
    'IPython',
    'matplotlib',
    'openpyxl',
    'sqlalchemy',
    'jinja2',
    'pytest',
    'setuptools',
]

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name='fuckCourse',
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
