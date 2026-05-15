from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installer.iss"


def _icon_lines():
    lines = []
    for raw in INSTALLER.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("Name:") and "IconFilename:" in line:
            lines.append(line)
    return lines


def test_app_shortcuts_use_executable_embedded_icon():
    app_shortcuts = [
        line
        for line in _icon_lines()
        if r"{#MyAppExeName}" in line and "(Help)" not in line
    ]

    assert app_shortcuts
    for line in app_shortcuts:
        assert r'IconFilename: "{app}\{#MyAppExeName}"' in line


def test_app_shortcuts_do_not_reference_missing_assets_folder():
    for line in _icon_lines():
        assert r"{app}\assets\bot_icon_new.ico" not in line
