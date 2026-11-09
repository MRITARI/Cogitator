; Inno Setup script: per-user install of Cogitator, no admin rights, Windows 11 x64 only.
; Build (after packaging\build_exe.ps1):  ISCC.exe packaging\cogitator.iss /DAppVersion=0.1.0
;
; Resulting layout (what cogitator.selfupdate.detect_layout() recognises as "installer"):
;   %LOCALAPPDATA%\Programs\Cogitator\
;       install.json              written by the [Code] section below
;       app\cogitator.exe ...     the PyInstaller folder; the self-updater swaps this folder
;       app\cogitator-updater.exe
;       setup\Cogitator Setup.exe ...
;       (app.previous\ and staging\ are created later by the updater)
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6E2F7B1A-3C54-4D0B-9A57-C0617A70B001}
AppName=Cogitator
AppVersion={#AppVersion}
AppPublisher=MRITARI
DefaultDirName={localappdata}\Programs\Cogitator
DefaultGroupName=Cogitator
PrivilegesRequired=lowest
MinVersion=10.0.22000
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
DisableDirPage=auto
SetupIconFile=cogitator.ico
UninstallDisplayIcon={app}\app\cogitator.exe
UninstallDisplayName=Cogitator
OutputDir=..\release
OutputBaseFilename=Cogitator-{#AppVersion}-win-x64-installer
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Files]
Source: "..\dist\cogitator\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\dist\Cogitator Setup\*"; DestDir: "{app}\setup"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "cogitator.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Cogitator"; Filename: "{app}\app\cogitator.exe"; IconFilename: "{app}\cogitator.ico"
Name: "{autoprograms}\Cogitator Setup (repair or uninstall)"; Filename: "{app}\setup\Cogitator Setup.exe"; IconFilename: "{app}\cogitator.ico"
Name: "{autodesktop}\Cogitator"; Filename: "{app}\app\cogitator.exe"; IconFilename: "{app}\cogitator.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: checkedonce

[Run]
Filename: "{app}\app\cogitator.exe"; Description: "Start Cogitator"; Flags: postinstall nowait skipifsilent unchecked

[UninstallDelete]
; Created by the self-updater after installation; Inno does not know about them.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\app.previous"
Type: filesandordirs; Name: "{app}\staging"
Type: files; Name: "{app}\install.json"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    // Its presence next to the app\ folder is what makes the Update panel live (detect_layout -> "installer").
    SaveStringToFile(ExpandConstant('{app}\install.json'),
      '{"installer": "inno", "version": "{#AppVersion}"}' + #13#10, False);
end;
