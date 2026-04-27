"""PyInstaller build orchestrator for the CATalyst desktop app

Wraps the full PyInstaller invocation so a single command produces a
distributable Windows build. Cleans previous `dist/` and `build/`
directories, runs PyInstaller against `catalyst.spec`, copies
`.env.example` next to the generated exe (kept external rather than
bundled so users can edit it), and verifies key assets shipped
correctly.

Key responsibilities:
    - Parse CLI flags and drive the clean / build / verify phases
    - Invoke PyInstaller via catalyst.spec and surface non-zero exits
    - Place .env.example alongside dist/Catalyst/Catalyst.exe
    - Sanity-check that expected output files exist after build

Usage:
    python build.py              # full clean build (default)
    python build.py --no-clean   # skip cleaning for faster iteration
"""

# --- src-layout bootstrap (auto-inserted) ---
import os as _os
import sys as _sys
_sys.path.insert(
    0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "src", "catalyst")
)
# --- end bootstrap ---

import os
import sys
import shutil
import subprocess
import argparse


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_FILE = os.path.join(HERE, 'catalyst.spec')
DIST_DIR = os.path.join(HERE, 'dist')
BUILD_DIR = os.path.join(HERE, 'build')
OUTPUT_DIR = os.path.join(DIST_DIR, 'Catalyst')
ENV_EXAMPLE = os.path.join(HERE, '.env.example')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(cmd, **kwargs):
    """Run a command, printing it first. Raises on non-zero exit."""
    print(f"\n  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"\n  ERROR: command exited with code {result.returncode}")
        sys.exit(result.returncode)
    return result


def _ensure_pyinstaller():
    """Check PyInstaller is importable; install it if not."""
    try:
        import PyInstaller  # noqa: F401
        import PyInstaller.__main__  # noqa: F401
        print("  PyInstaller found.")
    except ImportError:
        print("  PyInstaller not found — installing...")
        _run([sys.executable, '-m', 'pip', 'install', 'pyinstaller', '--break-system-packages'])
        print("  PyInstaller installed.")


def _clean():
    """Remove previous build and dist directories."""
    for path in (BUILD_DIR, DIST_DIR):
        if os.path.isdir(path):
            print(f"  Removing {path} ...")
            shutil.rmtree(path)
    # Remove PyInstaller's spec-generated __pycache__ entries but not the project's own
    pycache = os.path.join(HERE, '__pycache__')
    if os.path.isdir(pycache):
        print(f"  Removing {pycache} ...")
        shutil.rmtree(pycache)


def _build():
    """Run PyInstaller with our spec file."""
    _run([
        sys.executable,
        '-m', 'PyInstaller',
        '--noconfirm',          # Overwrite dist without prompting
        '--log-level', 'WARN',  # Suppress INFO noise; keep warnings/errors
        SPEC_FILE,
    ], cwd=HERE)


def _post_build():
    """Copy supporting files into the output directory."""
    if not os.path.isdir(OUTPUT_DIR):
        print(f"\n  ERROR: Expected output directory not found: {OUTPUT_DIR}")
        sys.exit(1)

    # Copy .env.example so users know what to configure
    if os.path.isfile(ENV_EXAMPLE):
        dest = os.path.join(OUTPUT_DIR, '.env.example')
        shutil.copy2(ENV_EXAMPLE, dest)
        print(f"  Copied .env.example -> {dest}")
    else:
        print("  Warning: .env.example not found — users will need to create .env manually.")

    # Sanity: confirm the executable exists (platform-specific name)
    exe_name = 'Catalyst.exe' if sys.platform == 'win32' else 'Catalyst'
    exe_path = os.path.join(OUTPUT_DIR, exe_name)
    if not os.path.isfile(exe_path):
        print(f"\n  ERROR: Executable not found at expected path: {exe_path}")
        sys.exit(1)

    # Confirm HTML files are bundled (quick sanity check)
    if not os.path.isfile(os.path.join(OUTPUT_DIR, 'bot_gui.html')):
        print("\n  WARNING: bot_gui.html not found in the bundle.")
        print("  The app may fail to load the GUI. Check the .spec datas list.")
    else:
        print("  HTML assets verified in bundle.")


def _print_success():
    exe_name = 'Catalyst.exe' if sys.platform == 'win32' else 'Catalyst'
    exe_path = os.path.join(OUTPUT_DIR, exe_name)
    size_mb = os.path.getsize(exe_path) / (1024 * 1024)
    print(f"""
  =====================================================
  BUILD SUCCESSFUL
  =====================================================

  Executable : {exe_path}
  Size       : {size_mb:.1f} MB
  Platform   : {sys.platform}

  To run:
    1. Copy {OUTPUT_DIR}/ to your target machine
    2. Ensure Sage wallet is running with RPC enabled
    3. Run {exe_name}
    4. The app auto-creates .env on first launch

  =====================================================
""")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Build CATalyst')
    parser.add_argument('--no-clean', action='store_true', help='Skip cleaning dist/ and build/ before building')
    args = parser.parse_args()

    print(f"\n  CATalyst — Build ({sys.platform})")
    print(f"  {'=' * 40}")
    print(f"  Python     : {sys.executable}")
    print(f"  Spec file  : {SPEC_FILE}")
    print(f"  Output dir : {OUTPUT_DIR}")

    if not os.path.isfile(SPEC_FILE):
        print(f"\n  ERROR: Spec file not found: {SPEC_FILE}")
        print("  Make sure catalyst.spec is in the same directory as build.py.")
        sys.exit(1)

    _ensure_pyinstaller()

    if not args.no_clean:
        print("\n  Cleaning previous build...")
        _clean()
    else:
        print("\n  Skipping clean (--no-clean).")

    print("\n  Running PyInstaller...")
    _build()

    print("\n  Post-build checks...")
    _post_build()

    _print_success()


if __name__ == '__main__':
    main()

