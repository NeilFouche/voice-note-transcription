; Inno Setup script for Voice Note Transcription.
;
; Produces a single Setup.exe: install wizard, Start Menu (and optional
; Desktop) shortcut, "Voice Note Transcription" entry in Windows'
; Apps & Features with a proper uninstaller. Installs per-user (no admin
; rights / UAC prompt needed), matching a non-technical user who may not
; have admin access on their own machine.
;
; Build with (after the PyInstaller build has produced dist\Voice Note
; Transcription\):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss

#define MyAppName "Voice Note Transcription"
#define MyAppVersion "1.0"
#define MyAppExeName "Voice Note Transcription.exe"

[Setup]
AppId={{B1D9C6C2-8B7C-4C1E-9C36-7B7F8D3E4A11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=VoiceNoteTranscription-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
