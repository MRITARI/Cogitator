; Inno Setup script: per-user install of Cogitator, no admin rights, Windows 11 x64 only.
; Build (after packaging\build_exe.ps1):  ISCC.exe packaging\cogitator.iss /DAppVersion=<version>   (packaging\release.py does this)
;
; Resulting layout (what cogitator.selfupdate.detect_layout() recognises as "installer"):
;   %LOCALAPPDATA%\Programs\Cogitator\
;       install.json              written by the [Code] section below
;       app\cogitator.exe         console program (the command line); app\cogitator-web.exe is the Cogitator window (same _internal folder)
;       app\...                   the PyInstaller folder; the self-updater swaps this folder
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
DisableReadyPage=yes
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
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\cogitator\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Cogitator"; Filename: "{app}\app\cogitator-web.exe"; WorkingDir: "{%USERPROFILE}"; IconFilename: "{app}\cogitator.ico"
Name: "{autoprograms}\Cogitator Setup"; Filename: "{app}\setup\Cogitator Setup.exe"; IconFilename: "{app}\cogitator.ico"
Name: "{autodesktop}\Cogitator"; Filename: "{app}\app\cogitator-web.exe"; WorkingDir: "{%USERPROFILE}"; IconFilename: "{app}\cogitator.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: checkedonce
Name: "addtopath"; Description: "Add Cogitator to the &PATH (so the cogitator command works in any new terminal)"
Name: "nxm"; Description: "Open Nexus Mods ""Mod Manager Download"" links in Cogitator (replaces Vortex/MO2 as the nxm:// handler; undone on uninstall)"; Flags: unchecked

[Run]
; The PATH edit lives in cogitator.userpath (tested); a failure here never blocks the install.
Filename: "{app}\app\cogitator.exe"; Parameters: "path add"; Flags: runhidden skipifdoesntexist; Tasks: addtopath
; Registers HKCU\Software\Classes\nxm (cogitator.nxm, tested); the previous handler is remembered and restored on uninstall.
Filename: "{app}\app\cogitator.exe"; Parameters: "nxm register"; Flags: runhidden skipifdoesntexist; Tasks: nxm
; One flow: after the files are in place the wizard opens by itself (not an optional checkbox). Silent installs skip it
; and never touch the game. The Nexus key is typed in the wizard only; nothing is passed on a command line.
Filename: "{app}\setup\Cogitator Setup.exe"; Parameters: "--wizard"; Flags: nowait skipifsilent

[UninstallRun]
Filename: "{app}\app\cogitator.exe"; Parameters: "path remove"; Flags: runhidden skipifdoesntexist; RunOnceId: "RemoveFromPath"
Filename: "{app}\app\cogitator.exe"; Parameters: "nxm unregister"; Flags: runhidden skipifdoesntexist; RunOnceId: "RemoveNxm"

[UninstallDelete]
; Created by the self-updater after installation; Inno does not know about them.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\app.previous"
Type: filesandordirs; Name: "{app}\staging"
Type: files; Name: "{app}\install.json"

[Code]
// Same keys, hives and "version >= 86" rule as cogitator_web.app.webview2_available(); keep the two in step.
function WebView2KeyOk(Root: Integer; Sub, Guid: String): Boolean;
var
  Pv: String;
begin
  Result := False;
  if RegQueryStringValue(Root, Sub + '\' + Guid, 'pv', Pv) then
  begin
    if Pos('.', Pv) > 0 then
      Pv := Copy(Pv, 1, Pos('.', Pv) - 1);
    Result := StrToIntDef(Pv, -1) >= 86;
  end;
end;

function WebView2Present(): Boolean;
var
  Guids: array[0..3] of String;
  I: Integer;
begin
  Guids[0] := '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'; // runtime
  Guids[1] := '{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}'; // Beta
  Guids[2] := '{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}'; // Dev
  Guids[3] := '{65C35B14-6C1D-4122-AC46-7148CC9D6497}'; // Canary
  Result := False;
  for I := 0 to 3 do
    if WebView2KeyOk(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients', Guids[I]) or
       WebView2KeyOk(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients', Guids[I]) or
       WebView2KeyOk(HKCU, 'Software\Microsoft\EdgeUpdate\Clients', Guids[I]) then
    begin
      Result := True;
      exit;
    end;
end;

// The welcome page adds nothing; the Tasks page stays.
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := PageID = wpWelcome;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Its presence next to the app\ folder is what makes the Update panel live (detect_layout -> "installer").
    SaveStringToFile(ExpandConstant('{app}\install.json'),
      '{"installer": "inno", "version": "{#AppVersion}"}' + #13#10, False);
    // No download, no network: the Cogitator window needs WebView2 (Windows 11 has it); the cogitator command works without it.
    if not WebView2Present() then
      SuppressibleMsgBox('WebView2 is required for the Cogitator window (Windows 11 includes it) and was not found on this computer.' + #13#10 + #13#10 +
        'Setup continues. The cogitator command still works without it.', mbInformation, MB_OK, IDOK);
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
