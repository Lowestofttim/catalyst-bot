; ============================================================
; CATalyst — Inno Setup installer script
; ============================================================
; Build the PyInstaller bundle first (python build.py), then open
; this file in Inno Setup Compiler (ISCC.exe) to produce the
; distributable .exe installer.
;
; Inno Setup download: https://jrsoftware.org/isinfo.php
;
; After Inno produces Output\Catalyst-Setup-X.Y.Z.exe,
; SIGN that installer with the same code-signing cert used for
; Catalyst.exe and splash.exe (see docs\PUBLIC_RELEASE_CHECKLIST.md). Users run
; the installer first, so SmartScreen checks its signature.
; ============================================================

#define MyAppName        "CATalyst"
#define MyAppVersion     "1.0.0"
#define MyAppPublisher   "MonkeyZoo"
#define MyAppURL         "https://github.com/catalystxch/catalyst-bot"
#define MyAppExeName     "Catalyst.exe"
#define MySourceDir      "dist\Catalyst"

[Setup]
; A fresh GUID per product. DO NOT re-use across unrelated products.
; Generate your own in Inno Setup: Tools -> Generate GUID.
AppId={{B7F7C8A3-5E1A-4D9B-9F43-CC51A3B9D2E7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyAppVersion}

; Install to Program Files by default.  All per-user data
; (bot.db, .env, logs, crash.log, backups) is written to
; %APPDATA%\Catalyst\ by user_paths.py, so Program Files
; can stay read-only as Microsoft intends.
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; Allow both admin (all users) and non-admin (current user) installs.
; On non-admin installs, {autopf} becomes %LOCALAPPDATA%\Programs.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

OutputBaseFilename=Catalyst-Setup-{#MyAppVersion}
OutputDir=Output
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern

; Uninstaller
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

; Require Windows 10 or later (WebView2 needs it)
MinVersion=10.0.17763

; Let silent upgrades replace the running app cleanly. CATalyst launches the
; installer only after the user confirms the update and the bot is stopped.
CloseApplications=yes
RestartApplications=no

; Show license during install (optional — create LICENSE.txt first)
; LicenseFile=LICENSE.txt

; Icons for Add/Remove Programs and shortcuts
SetupIconFile=assets\bot_icon_new.ico

; Wizard branding — MonkeyZoo logo on left panel, app icon top-right.
; Inno Setup 6+ accepts PNG directly and auto-scales.
WizardImageFile=assets\MonkeyZoo_Logo.png
WizardSmallImageFile=assets\bot_icon_new.png

; Run the app after install (optional checkbox on the final page)
; See [Run] section below

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startmenuicon"; Description: "Create a &Start Menu shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Pack the entire PyInstaller output folder. The wildcard with
; recursesubdirs picks up _internal\ and every bundled asset.
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Explicitly include the .env.example so users can see a template
; (user_paths.py seeds .env from this on first launch if needed).
; Harmless if already matched by the wildcard above.
Source: "{#MySourceDir}\.env.example"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist

[Icons]
; Start Menu group entry
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startmenuicon; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName} (Help)"; Filename: "{#MyAppURL}"; Tasks: startmenuicon
Name: "{autoprograms}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"; Tasks: startmenuicon

; Desktop shortcut (opt-in via task checkbox)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; IconFilename: "{app}\{#MyAppExeName}"

[Run]
; Offer to launch the app after install finishes.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
; Silent in-app upgrades pass /CATALYST_RELAUNCH=1 so the updated app reopens.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait skipifnotsilent; Check: ShouldAutoRelaunch

[UninstallDelete]
; The uninstaller removes the install dir, but per-user data in
; %APPDATA%\Catalyst\ is deliberately left behind so a
; reinstall picks up the user's existing wallet settings and
; trade history. Users who want a clean wipe can delete
; %APPDATA%\Catalyst\ manually.

[Code]
function ShouldAutoRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:CATALYST_RELAUNCH|0}') = '1';
end;

// Friendly message on the install-complete page reminding users
// where their config and database live.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Nothing automated yet — reserved for future steps
    // (e.g. registering URL handlers or creating Start Menu tiles).
  end;
end;
