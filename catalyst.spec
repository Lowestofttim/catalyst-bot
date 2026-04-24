# build_windows.spec
#
# PyInstaller spec file for CATalyst — Windows (onedir bundle)
#
# Usage:
#   pyinstaller build_windows.spec
#
# Or via the helper script:
#   python build.py
#
# Output: dist/Catalyst/Catalyst.exe  (plus all supporting files)
#
# Design notes:
#   - onedir mode: all files sit in dist/Catalyst/.  Simpler to debug
#     than onefile, no extraction delay on launch, and easier for users to
#     place their .env file alongside the exe.
#   - splash.exe is bundled as a data file so the Splash P2P node works
#     out of the box.  SplashNode.find_binary() searches sys._MEIPASS (the
#     bundle root) automatically because it looks in the same dir as the
#     running script.
#   - .env is never bundled — it lives alongside the exe and holds secrets.
#   - PyWebView on Windows requires edgechromium (WebView2) which ships with
#     Windows 11 and is auto-installed on Windows 10 via Windows Update.
#     The winforms backend is included as a fallback.

import os
import sys

block_cipher = None

# ---------------------------------------------------------------------------
# Resolve the project root (where this spec file lives) and the
# source directory.  Application modules live under src/catalyst/; that
# directory is added to pathex so PyInstaller finds flat imports like
# `import api_server` without the modules sitting at the project root.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(SPEC))  # noqa: F821  (SPEC is injected by PyInstaller)
_SRC = os.path.join(_HERE, 'src', 'catalyst')

# ---------------------------------------------------------------------------
# Data files — everything the running app reads from the filesystem
# ---------------------------------------------------------------------------

# All HTML files served by Flask
_html_files = [
    (os.path.join(_HERE, 'bot_gui.html'), '.'),
    (os.path.join(_HERE, 'splash.html'), '.'),
]

# Brand / UI assets (now in assets/ subfolder)
_assets_dir = os.path.join(_HERE, 'assets')
_image_files = [
    (os.path.join(_assets_dir, f), 'assets')
    for f in ('bot_icon_new.png', 'bot_icon_new.ico', 'favicon.ico',
              'dexie_logo_official.png', 'dexie_logo_official.ico',
              'sage_logo_official.png', 'tibetswap_logo_official.png',
              'MonkeyZoo_Logo.png', 'monkeyzoo-logo-1.gif',
              'spacescan-logo-192.webp')
    if os.path.isfile(os.path.join(_assets_dir, f))
]
# Also check root for backward compat
_image_files += [
    (os.path.join(_HERE, f), '.')
    for f in ('bot_icon_new.png', 'bot_icon_new.ico')
    if os.path.isfile(os.path.join(_HERE, f))
    and not any(os.path.basename(s) == f for s, _ in _image_files)
]

# Splash P2P binary (optional — bundled when present, platform-specific)
_splash_name = 'splash.exe' if sys.platform == 'win32' else 'splash'
_splash_path = os.path.join(_HERE, _splash_name)
_splash_files = [(_splash_path, '.')] if os.path.isfile(_splash_path) else []

# Coin prep worker — launched as a subprocess, needs the .py file in the bundle
_worker_path = os.path.join(_SRC, 'coin_prep_worker.py')
_worker_files = [(_worker_path, '.')] if os.path.isfile(_worker_path) else []

_datas = _html_files + _image_files + _splash_files + _worker_files

# ---------------------------------------------------------------------------
# Hidden imports
#
# PyInstaller's static analysis misses dynamic imports and modules that are
# only referenced as strings (e.g. pywebview platform backends).
# ---------------------------------------------------------------------------
_hiddenimports = [
    # Flask and its ecosystem
    'flask',
    'flask.templating',
    'jinja2',
    'jinja2.ext',
    'werkzeug',
    'werkzeug.serving',
    'werkzeug.middleware.proxy_fix',

    # PyWebView — platform backends (runtime picks best available)
    'webview',
    'webview.guilib',
    *(['webview.platforms.winforms', 'webview.platforms.mshtml',
       'webview.platforms.edgechromium'] if sys.platform == 'win32' else []),
    *(['webview.platforms.cocoa'] if sys.platform == 'darwin' else []),
    *(['webview.platforms.gtk'] if sys.platform == 'linux' else []),

    # System tray
    'pystray',
    *(['pystray._win32'] if sys.platform == 'win32' else []),
    *(['pystray._darwin'] if sys.platform == 'darwin' else []),
    *(['pystray._xorg'] if sys.platform == 'linux' else []),

    # Notifications
    'plyer',
    'plyer.utils',
    *(['plyer.platforms.win.notification'] if sys.platform == 'win32' else []),
    *(['plyer.platforms.macosx.notification'] if sys.platform == 'darwin' else []),
    *(['plyer.platforms.linux.notification'] if sys.platform == 'linux' else []),

    # Pillow (required by pystray for icon generation)
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'PIL.ImageFont',
    'PIL._imaging',

    # HTTP / networking
    'requests',
    'requests.adapters',
    'requests.auth',
    'urllib3',
    'urllib3.util',
    'urllib3.util.retry',
    'certifi',

    # Config / env
    'dotenv',
    'dotenv.main',

    # Data / serialisation
    'decimal',
    'json',
    'sqlite3',

    # Bot modules (all referenced by string or lazy import)
    'api_server',
    'app_bridge',
    'tray_manager',
    'notification_manager',
    'bot_loop',
    'database',
    'config',
    'price_engine',
    'offer_manager',
    'fill_tracker',
    'risk_manager',
    'coin_manager',
    'market_intel',
    'sniper',
    'boost_manager',
    'coinset_client',
    'splash_manager',
    'splash_node',
    'splash_receive',
    'splash_setup',
    'dexie_manager',
    'wallet',
    'wallet_chia',
    'wallet_sage',
    'mock_wallet',
    'coin_prep_worker',
    'super_log',
    'super_log_hooks',
    'startup_test',
    'chia_node',
    'sage_node',
    'runtime_monitor',
    'spacescan',
    'tx_fees',
    'reservation_manager',
    'market_data_collector',
    'mempool_watcher',
    'win_subprocess',
    'event_taxonomy',
    'offer_lifecycle',
    'user_secrets',
    'reaction_strategy',
]

# ---------------------------------------------------------------------------
# Binaries — compiled extensions that Analysis may not find automatically
# ---------------------------------------------------------------------------
_binaries = []

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    ['desktop_app.py'],
    pathex=[_HERE, _SRC],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Test files — no need in the bundle
        'pytest',
        'playwright',
        'unittest',
        # Heavy scientific stack (not used)
        'numpy',
        'pandas',
        'matplotlib',
        'scipy',
        'IPython',
        'notebook',
        # Dev tools
        'black',
        'mypy',
        'pylint',
        'flake8',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# PYZ archive (compiled .pyc files)
# ---------------------------------------------------------------------------
pyz = PYZ(  # noqa: F821  (injected by PyInstaller)
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)

# ---------------------------------------------------------------------------
# EXE
# ---------------------------------------------------------------------------
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir: binaries go into COLLECT, not the exe
    name='Catalyst',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                # Compress where possible (reduces size)
    console=False,           # No console window — desktop_app.py manages this
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(_assets_dir, 'bot_icon_new.ico') if os.path.isfile(os.path.join(_assets_dir, 'bot_icon_new.ico')) else None,
    # Embed Windows PE version metadata so Task Manager, file Properties, and
    # SmartScreen show the correct product name / description / company.
    version=os.path.join(_HERE, 'version_info.txt') if os.path.isfile(os.path.join(_HERE, 'version_info.txt')) else None,
)

# ---------------------------------------------------------------------------
# COLLECT — assemble the final onedir bundle
# ---------------------------------------------------------------------------
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Catalyst',
)

# ---------------------------------------------------------------------------
# macOS .app bundle (only produced on macOS builds)
#
# On macOS users expect a double-clickable .app, not a bare folder.
# PyInstaller's BUNDLE() wraps the COLLECT output into a proper
# macOS application bundle with the correct Info.plist and icon.
#
# To generate the .icns icon from the PNG:
#   mkdir icon.iconset
#   sips -z 1024 1024 assets/bot_icon_new.png --out icon.iconset/icon_512x512@2x.png
#   iconutil -c icns icon.iconset -o assets/bot_icon_new.icns
# ---------------------------------------------------------------------------
if sys.platform == 'darwin':
    _icns_path = os.path.join(_assets_dir, 'bot_icon_new.icns')
    app = BUNDLE(  # noqa: F821
        coll,
        name='CATalyst.app',
        icon=_icns_path if os.path.isfile(_icns_path) else None,
        bundle_identifier='com.monkeyzoo.catalyst',
        info_plist={
            'CFBundleName': 'CATalyst',
            'CFBundleDisplayName': 'CATalyst',
            'CFBundleVersion': '1.0.0',
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': True,
        },
    )
