; Inno Setup script for the Firefly Windows installer.
; CI passes /DAppVersion=x.y.z /DSourceDir=<flutter Release dir>
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "build\windows\x64\runner\Release"
#endif

[Setup]
AppId={{6C2B1F0E-3F7A-4E6B-9C55-0F1EF1A00001}
AppName=Firefly
AppVersion={#AppVersion}
AppPublisher=grimmmyy
AppPublisherURL=https://github.com/grimmmyy/Firefly
AppSupportURL=https://github.com/grimmmyy/Firefly/issues
DefaultDirName={autopf}\Firefly
DefaultGroupName=Firefly
UninstallDisplayIcon={app}\firefly.exe
OutputDir=dist
OutputBaseFilename=Firefly-Setup
SetupIconFile=windows\runner\resources\app_icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Firefly"; Filename: "{app}\firefly.exe"
Name: "{group}\Uninstall Firefly"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Firefly"; Filename: "{app}\firefly.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\firefly.exe"; Description: "{cm:LaunchProgram,Firefly}"; Flags: nowait postinstall skipifsilent
