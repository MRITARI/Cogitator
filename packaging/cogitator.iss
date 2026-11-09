; Inno Setup script: per-user install of Cogitator, no admin rights, Windows 11 x64 only.
; Build (after packaging\build_exe.ps1):  ISCC.exe packaging\cogitator.iss /DAppVersion=<version>   (packaging\release.py does this)
;
; Resulting layout (what cogitator.selfupdate.detect_layout() recognises as "installer"):
;   %LOCALAPPDATA%\Programs\Cogitator\
;       install.json              written by the [Code] section below
;       app\cogitator.exe ...     the PyInstaller folder; the self-updater swaps this folder
;       app\cogitator-updater.exe
;       setup\Cogitator Setup.exe ...
;       (app.previous\ and staging\ are created later by the updater)
#ifndef AppVersion
  #error Pass the version: ISCC.exe /DAppVersion=1.2.3 cogitator.iss
#endif

[Setup]
AppId={{6E2F7B1A-3C54-4D0B-9A57-C0617A70B001}
AppName=Cogitator
AppVersion={#AppVersion}
AppPublisher=MRITARI
AppPublisherURL=https://github.com/MRITARI/Cogitator
AppSupportURL=https://github.com/MRITARI/Cogitator/issues
ChangesEnvironment=yes
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
DisableFinishedPage=yes

[Files]
Source: "..\dist\cogitator\*"; DestDir: "{app}\app"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\dist\Cogitator Setup\*"; DestDir: "{app}\setup"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "cogitator.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Cogitator"; Filename: "{app}\app\cogitator.exe"; WorkingDir: "{%USERPROFILE}"; IconFilename: "{app}\cogitator.ico"
Name: "{autoprograms}\Cogitator Setup"; Filename: "{app}\setup\Cogitator Setup.exe"; IconFilename: "{app}\cogitator.ico"
Name: "{autodesktop}\Cogitator"; Filename: "{app}\app\cogitator.exe"; WorkingDir: "{%USERPROFILE}"; IconFilename: "{app}\cogitator.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: checkedonce
Name: "addtopath"; Description: "Add Cogitator to the &PATH (so the cogitator command works in any new terminal)"; Flags: checkedonce

[Run]
; One flow: after the files are in place the wizard opens by itself (not an optional checkbox). Silent installs skip it
; and never touch the game. The Nexus key is typed in the wizard only; nothing is passed on a command line.
Filename: "{app}\setup\Cogitator Setup.exe"; Parameters: "--wizard"; Flags: nowait skipifsilent

[UninstallDelete]
; Created by the self-updater after installation; Inno does not know about them.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\app.previous"
Type: filesandordirs; Name: "{app}\staging"
Type: files; Name: "{app}\install.json"

[Code]
// The user's PATH (HKCU\Environment, no admin). Entries are compared whole, ignoring case and a trailing backslash.
function NormEntry(S: String): String;
begin
  S := Trim(S);
  while (Length(S) > 1) and (S[Length(S)] = '\') do
    Delete(S, Length(S), 1);
  Result := Uppercase(S);
end;

function PathHasEntry(Paths, Dir: String): Boolean;
var
  P: Integer;
  Item, Want: String;
begin
  Result := False;
  Want := NormEntry(Dir);
  while Paths <> '' do
  begin
    P := Pos(';', Paths);
    if P = 0 then
    begin
      Item := Paths;
      Paths := '';
    end else
    begin
      Item := Copy(Paths, 1, P - 1);
      Delete(Paths, 1, P);
    end;
    if NormEntry(Item) = Want then
    begin
      Result := True;
      exit;
    end;
  end;
end;

function PathWithout(Paths, Dir: String): String;
var
  P: Integer;
  Item, Want: String;
begin
  Result := '';
  Want := NormEntry(Dir);
  while Paths <> '' do
  begin
    P := Pos(';', Paths);
    if P = 0 then
    begin
      Item := Paths;
      Paths := '';
    end else
    begin
      Item := Copy(Paths, 1, P - 1);
      Delete(Paths, 1, P);
    end;
    if (Item <> '') and (NormEntry(Item) <> Want) then
    begin
      if Result <> '' then
        Result := Result + ';';
      Result := Result + Item;
    end;
  end;
end;

procedure AddToPath(Dir: String);
var
  Paths: String;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
    Paths := '';
  if PathHasEntry(Paths, Dir) then
    exit;
  if (Paths <> '') and (Paths[Length(Paths)] <> ';') then
    Paths := Paths + ';';
  RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Paths + Dir);
end;

procedure RemoveFromPath(Dir: String);
var
  Paths: String;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
    exit;
  if PathHasEntry(Paths, Dir) then
    RegWriteExpandStringValue(HKCU, 'Environment', 'Path', PathWithout(Paths, Dir));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}\app'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Its presence next to the app\ folder is what makes the Update panel live (detect_layout -> "installer").
    SaveStringToFile(ExpandConstant('{app}\install.json'),
      '{"installer": "inno", "version": "{#AppVersion}"}' + #13#10, False);
    if WizardIsTaskSelected('addtopath') then
      AddToPath(ExpandConstant('{app}pp'));
  end;
end;

function InitializeUninstall(): Boolean;
var
  SetupExe: String;
  Code: Integer;
begin
  Result := True; // the app is removed whatever the answer is
  if UninstallSilent then
    exit; // silent uninstalls never touch the game
  SetupExe := ExpandConstant('{app}\setup\Cogitator Setup.exe');
  if not FileExists(SetupExe) then
    exit;
  if MsgBox('Also remove the Mod Loader and Framework from Darktide?', mbConfirmation, MB_YESNO) = IDYES then
    Exec(SetupExe, '--uninstall', '', SW_SHOWNORMAL, ewWaitUntilTerminated, Code)
  else
    MsgBox('Darktide was left as it is. Mods stay active until you switch them off with Cogitator or toggle_darktide_mods.bat.' + #13#10 + #13#10 +
      'Cogitator''s own settings, logs and your saved Nexus key (Windows Credential Manager, entry "cogitator") are kept too. ' +
      'To remove them, delete the folder ' + ExpandConstant('{localappdata}\cogitator') + ' and that credential.',
      mbInformation, MB_OK);
end;
