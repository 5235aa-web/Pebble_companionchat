# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the companion backend.

Two things shape this file.

The audio.cpp runtime is a plain Win32 program, not a Python package: it has to
travel as binaries plus its model-spec directory, and only the server build is
included. The CLI and the GGUF converter are debug tools that would add 150 MB
for nothing.

Attachment parsing imports Pillow / python-docx / openpyxl / pypdf inside
functions so a missing library degrades one file type instead of the whole app.
PyInstaller cannot see those imports, so they are listed as hidden imports.
"""

import os

_AUDIO = 'backend/audio-cpp'

# Only the server binary and the MSVC runtime it links against. audiocpp_cli.exe
# and audiocpp_gguf.exe are developer tools; the zip is the release archive they
# came from; the wav/log files are test residue.
_AUDIO_BINARIES = [
    (_AUDIO + '/audiocpp_server.exe', 'audio-cpp'),
    (_AUDIO + '/msvcp140.dll', 'audio-cpp'),
    (_AUDIO + '/msvcp140_1.dll', 'audio-cpp'),
    (_AUDIO + '/msvcp140_2.dll', 'audio-cpp'),
    (_AUDIO + '/msvcp140_atomic_wait.dll', 'audio-cpp'),
    (_AUDIO + '/msvcp140_codecvt_ids.dll', 'audio-cpp'),
    (_AUDIO + '/vcruntime140.dll', 'audio-cpp'),
    (_AUDIO + '/vcruntime140_1.dll', 'audio-cpp'),
    (_AUDIO + '/vcruntime140_threads.dll', 'audio-cpp'),
    (_AUDIO + '/vcomp140.dll', 'audio-cpp'),
]

# model_specs is read by audiocpp_server at startup to resolve model families;
# without it the server refuses to load any engine.
_AUDIO_DATAS = [
    (_AUDIO + '/model_specs', 'audio-cpp/model_specs'),
    (_AUDIO + '/LICENSE', 'audio-cpp'),
]

# External worker scripts & headless See-through inference sources for optional
# Live2D decomposition (~1.2 MB). PyTorch itself stays outside the bundle in the
# user's isolated venv (configured via ~/.pebblecompanion/inochi2d-venv or UI).
_INOCHI_DATAS = [
    ('backend/Inochi2D/run_decompose.py', 'Inochi2D'),
    ('backend/Inochi2D/puppet_builder.py', 'Inochi2D'),
    ('backend/Inochi2D/expression_extractor.py', 'Inochi2D'),
    ('backend/Inochi2D/requirements.txt', 'Inochi2D'),
]
if os.path.isdir('backend/Inochi2D/see-through'):
    _INOCHI_DATAS += [
        ('backend/Inochi2D/see-through/LICENSE', 'Inochi2D/see-through'),
        ('backend/Inochi2D/see-through/common', 'Inochi2D/see-through/common'),
        ('backend/Inochi2D/see-through/inference/scripts', 'Inochi2D/see-through/inference/scripts'),
    ]

a = Analysis(
    ['run_backend.py'],
    pathex=[],
    binaries=_AUDIO_BINARIES,
    datas=_AUDIO_DATAS + _INOCHI_DATAS,
    hiddenimports=[
        # uvicorn picks its event loop and protocol backend at runtime, so none
        # of these appear in an import graph.
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # Deferred imports inside attachment_parser: each one guards a single
        # file type, so they never show up at module import time.
        'PIL',
        'PIL.Image',
        'docx',
        'openpyxl',
        'pypdf',
        # token_engine falls back to tiktoken, and to tokenizers when a vendor
        # tokenizer file exists. tiktoken discovers encodings dynamically from
        # the tiktoken_ext namespace package, so openai_public must be listed.
        'tiktoken',
        'tiktoken_ext',
        'tiktoken_ext.openai_public',
        'tokenizers',
        # ComfyUI progress arrives over a WebSocket. websockets resolves these
        # submodules at runtime, so the static import scan does not see them.
        'websockets',
        'websockets.asyncio',
        'websockets.asyncio.client',
        'websockets.legacy',
        'websockets.speedups',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # torch and kokoro went with the Kokoro engine; soundfile, scipy, openai and
    # aiohttp only ever appeared in audio-cpp/tools scripts or a superseded LLM
    # path. onnxruntime arrived through smart_turn and turn_detector, both since
    # removed with the semantic turn-taking models. Explicitly excluding numpy
    # (which openpyxl and tiktoken try to import optionally) plus ML embedding
    # stacks keeps heavy runtimes out of the bundle for good.
    excludes=[
        'torch',
        'kokoro',
        'soundfile',
        'scipy',
        'openai',
        'aiohttp',
        'matplotlib',
        'IPython',
        'pytest',
        'onnxruntime',
        'numpy',
        'transformers',
        'sentence_transformers',
        'chromadb',
        'sklearn',
    ],
    noarchive=False,
    optimize=0,
)

# Packages with compiled extensions are collected as whole trees, tests and
# headers included. Nothing imports those at runtime, so strip them.
def _unwanted(entry: str) -> bool:
    n = entry.replace('\\', '/').lower()
    return (
        '/tests/' in n
        or n.startswith('tests/')
        or '/__pycache__/' in n
        or 'inochi2d/see-through/common/assets/' in n
        or n.endswith(('.pyi', '.pyx', '.pxd', '.ipynb'))
    )


a.datas = [d for d in a.datas if not _unwanted(d[0])]
a.binaries = [b for b in a.binaries if not _unwanted(b[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='pebble-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['audiocpp_server.exe', 'vcruntime140.dll', 'msvcp140.dll'],
    runtime_tmpdir=None,
    # console=True keeps a window that shows backend logs. The shell spawns this
    # with CREATE_NO_WINDOW, so nothing appears on screen in the packaged build,
    # but running the exe by hand still prints uvicorn output for debugging.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
