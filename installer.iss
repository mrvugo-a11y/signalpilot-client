; Inno Setup script: builds "Output\SignalPilot Setup.exe"
; Build: iscc installer.iss   (after PyInstaller has produced dist\SignalPilot)
#define AppName "SignalPilot"
#ifndef AppVersion
  #define AppVersion "1.3.15"
#endif

[Setup]
AppId={{D72C9711-B6E2-4A42-8CF1-9737549A12EC}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=Output
OutputBaseFilename={#AppName} Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install by default ({autopf} = %LOCALAPPDATA%\Programs), so no UAC is needed.
; /ALLUSERS on the command line (used by the updater when the old copy is in Program Files) upgrades that install in place.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
SetupLogging=yes
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex=Local\SignalPilot-SingleInstance
CloseApplications=yes
UninstallDisplayIcon={app}\{#AppName}.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent
; After an automatic (silent) update the updater helper reopens the app once it has checked the exit code.

; Customer data (login, settings, trades) lives in %APPDATA%\SignalPilot and is kept on uninstall and upgrade.
