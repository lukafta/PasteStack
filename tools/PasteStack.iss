; PasteStack installer (Inno Setup 6). Built by tools/build_exe.py, which passes the values
; below with /D from the app's own code; don't compile it by hand with stale ones.
;
; Per-user: installs to %LOCALAPPDATA%\Programs\PasteStack, no administrator rights.
; Not code-signed, so Windows SmartScreen warns on the first run.

#ifndef AppVersion
  #error Build the installer with: py tools/build_exe.py
#endif

[Setup]
; never change AppId: it's how Windows knows a new version replaces the old one
AppId={{A3E1C7B2-5D49-4F86-9C2B-7E61D0F4A825}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersionDotted}
VersionInfoDescription={#AppName} Setup
PrivilegesRequired=lowest
DefaultDirName={userpf}\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\{#AppName}.exe
UninstallDisplayName={#AppName}
OutputDir={#OutputDir}
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
; [Code] asks a running copy to quit first; this is the check after that
AppMutex={#AppMutex}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
ptbr.StartupGroup=Ao entrar no Windows:
en.StartupGroup=When I sign in:
ptbr.StartWithWindows=Abrir o {#AppName} com o Windows (ele fica na bandeja, capturando)
en.StartWithWindows=Start {#AppName} with Windows (it waits in the notification area)
ptbr.CloseForSetup=O {#AppName} está aberto. O instalador vai fechá-lo agora para atualizar.
en.CloseForSetup={#AppName} is open. Setup will close it now so it can be updated.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "{cm:StartWithWindows}"; GroupDescription: "{cm:StartupGroup}"; Flags: unchecked

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; the same AppUserModelID as the running window, so a pinned taskbar button and the Start Menu agree
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; WorkingDir: "{app}"; AppUserModelID: "{#AppUserModelId}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; WorkingDir: "{app}"; AppUserModelID: "{#AppUserModelId}"; Tasks: desktopicon

[Registry]
; the same entry Configurações > "Abrir com o Windows" writes (clipapp/autostart.py)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppName}.exe"" --tray"; Tasks: autostart

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; the program folder only. The saved data in %APPDATA%\ClipApp stays.
Type: filesandordirs; Name: "{app}"

[Code]
const
  RUN_KEY = 'Software\Microsoft\Windows\CurrentVersion\Run';
  APPROVED_KEY = 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run';

{ Asks a running copy to quit the way its tray menu does, then waits up to 10 s. }
procedure CloseRunningApp();
var
  Engine: HWND;
  I: Integer;
begin
  if not CheckForMutexes('{#AppMutex}') then
    Exit;
  Engine := FindWindowByClassName('{#EngineClass}');
  if Engine <> 0 then
    PostMessage(Engine, {#QuitMessage}, 0, 0);
  for I := 1 to 40 do
  begin
    if not CheckForMutexes('{#AppMutex}') then
    begin
      Sleep(800);  { the files go with the process: give Windows a moment to let go of them }
      Exit;
    end;
    Sleep(250);
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if CheckForMutexes('{#AppMutex}') then
  begin
    if SuppressibleMsgBox(CustomMessage('CloseForSetup'), mbInformation, MB_OKCANCEL, IDOK) = IDCANCEL then
      Result := False
    else
      CloseRunningApp();
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseRunningApp();  { in case it was opened again while the wizard was up }
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Command: String;
begin
  if CurUninstallStep = usAppMutexCheck then
    CloseRunningApp();
  if CurUninstallStep <> usPostUninstall then
    Exit;
  { "Abrir com o Windows", set here or in the app: removed if it starts this copy }
  if RegQueryStringValue(HKCU, RUN_KEY, '{#AppName}', Command) and
     (Pos(Lowercase(ExpandConstant('{app}')), Lowercase(Command)) > 0) then
  begin
    RegDeleteValue(HKCU, RUN_KEY, '{#AppName}');
    RegDeleteValue(HKCU, APPROVED_KEY, '{#AppName}');
  end;
end;
