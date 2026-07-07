; Inno Setup script for the Athens Windows installer.
; Run by scripts\build-windows.ps1 after PyInstaller produces dist\Athens\ :
;   iscc /DMyAppVersion=<version> packaging\Athens.iss
; Paths are relative to this file (packaging\).
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

[Setup]
; stable upgrade identity — never change this GUID (uninstall/upgrade keys off it)
AppId={{AAAAF8B1-2982-4499-942C-C765C3DA9E64}
AppName=Athens
AppVersion={#MyAppVersion}
AppPublisher=Nodal Point
DefaultDirName={autopf}\Athens
DefaultGroupName=Athens
UninstallDisplayIcon={app}\Athens.exe
OutputDir=..\dist
OutputBaseFilename=Athens-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
SetupIconFile=Athens.ico
; user data (%APPDATA%\roto-reaper config/links, %LOCALAPPDATA%\Athens\logs) is
; deliberately left behind on uninstall — learned maps/links survive reinstalls

[Files]
Source: "..\dist\Athens\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Athens"; Filename: "{app}\Athens.exe"
Name: "{autodesktop}\Athens"; Filename: "{app}\Athens.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Run]
Filename: "{app}\Athens.exe"; Description: "Launch Athens"; Flags: nowait postinstall skipifsilent

[Code]
{ pywebview needs the WebView2 Runtime; without it the UI silently falls back to
  the deprecated MSHTML engine. Warn + offer the download rather than block. }
function IsWebView2Installed(): Boolean;
var
  V: string;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) or
    RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V);
end;

function InitializeSetup(): Boolean;
var
  ErrorCode: Integer;
begin
  Result := True;
  if not IsWebView2Installed() then
    if MsgBox('Athens uses the Microsoft WebView2 Runtime, which was not found.'#13#10 +
              'Open the WebView2 download page now? (Setup will continue either way.)',
              mbConfirmation, MB_YESNO) = IDYES then
      ShellExecAsOriginalUser('open',
        'https://developer.microsoft.com/microsoft-edge/webview2/',
        '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;
