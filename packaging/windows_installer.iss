; Inno Setup script for IPTV Player.
;
; Build with:  iso: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows_installer.iss
; Normally invoked by packaging\build_windows.bat, which runs PyInstaller first.
;
; VLC is intentionally NOT bundled: the app uses the user's VLC install. The
; installer detects it and warns when it is missing or the wrong architecture,
; because a DLL/app bitness mismatch is the most common failure on Windows.

#define AppName        "IPTV Player"
#define AppVersion     "1.3.1"
#define AppPublisher   "IPTV Player"
#define AppExeName     "IPTVPlayer.exe"
#define VlcUrl         "https://www.videolan.org/vlc/"

[Setup]
AppId={{8E4C1D6A-3F27-4B15-9A6E-2C7D5B0F91A4}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL={#VlcUrl}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=
OutputDir=..\dist
OutputBaseFilename=IPTV-Player-{#AppVersion}-Windows-x64-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The PyInstaller build is 64-bit, so refuse to install on 32-bit Windows
; rather than failing later at launch.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; The whole PyInstaller output folder.
Source: "..\dist\IPTVPlayer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the installed program files; user data is handled in code below so the
; person can choose to keep their playlists and downloads.
Type: filesandordirs; Name: "{app}"

[Code]
const
  VlcRegKey = 'SOFTWARE\VideoLAN\VLC';

function VlcInstallDir(): String;
var
  Dir: String;
begin
  Result := '';
  { 64-bit view first: the app is 64-bit and needs a matching VLC. }
  if RegQueryStringValue(HKLM64, VlcRegKey, 'InstallDir', Dir) then
    Result := Dir
  else if RegQueryStringValue(HKCU64, VlcRegKey, 'InstallDir', Dir) then
    Result := Dir
  else if FileExists(ExpandConstant('{commonpf64}\VideoLAN\VLC\libvlc.dll')) then
    Result := ExpandConstant('{commonpf64}\VideoLAN\VLC');
end;

function Vlc64Present(): Boolean;
var
  Dir: String;
begin
  Dir := VlcInstallDir();
  Result := (Dir <> '') and FileExists(AddBackslash(Dir) + 'libvlc.dll');
end;

function Vlc32Present(): Boolean;
var
  Dir: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM32, VlcRegKey, 'InstallDir', Dir) then
    Result := FileExists(AddBackslash(Dir) + 'libvlc.dll')
  else if FileExists(ExpandConstant('{commonpf32}\VideoLAN\VLC\libvlc.dll')) then
    Result := True;
end;

procedure OpenVlcSite();
var
  ErrorCode: Integer;
begin
  ShellExec('open', '{#VlcUrl}', '', '', SW_SHOW, ewNoWait, ErrorCode);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if Vlc64Present() then
    Exit;

  if Vlc32Present() then
  begin
    { A 32-bit VLC cannot be loaded by this 64-bit app, and the resulting
      failure is confusing, so say so plainly before installing. }
    if MsgBox('A 32-bit version of VLC was found.'#13#10#13#10
              + '{#AppName} is 64-bit and can only use 64-bit VLC, so video '
              + 'will not play until you install the 64-bit build '
              + '(the "Windows 64bit" installer on videolan.org).'#13#10#13#10
              + 'Open the VLC download page now?',
              mbConfirmation, MB_YESNO) = IDYES then
      OpenVlcSite();
  end
  else
  begin
    if MsgBox('VLC was not found on this computer.'#13#10#13#10
              + '{#AppName} plays video using VLC''s engine, so VLC must be '
              + 'installed for playback to work. Choose the "Windows 64bit" '
              + 'installer.'#13#10#13#10
              + 'Open the VLC download page now?',
              mbConfirmation, MB_YESNO) = IDYES then
      OpenVlcSite();
  end;

  { Installing without VLC is allowed; the app explains it again on launch. }
  if MsgBox('Continue installing {#AppName} anyway?'#13#10#13#10
            + 'You can install VLC later - {#AppName} will find it '
            + 'automatically.', mbConfirmation, MB_YESNO) = IDNO then
    Result := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\IPTVPlayer');
    if DirExists(DataDir) then
    begin
      if MsgBox('Remove your playlists, favourites and cached catalog too?'#13#10#13#10
                + 'Choose No to keep them for a future reinstall.'#13#10
                + 'Downloaded videos are never removed.',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
