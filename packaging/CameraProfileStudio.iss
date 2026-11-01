#ifndef PayloadDir
  #error PayloadDir must point to the standalone directory
#endif
#ifndef OutputDir
  #error OutputDir must be supplied
#endif
#ifndef ReleaseVersion
  #define ReleaseVersion "0.1.0"
#endif
#ifndef ArtifactStem
  #define ArtifactStem "CameraProfileStudio"
#endif
[Setup]
AppId={{B15FF861-C98A-4F3F-B46D-143C62935FA3}
AppName=Camera Profile Studio
AppVersion={#ReleaseVersion}
AppPublisher=Camera Profile Studio Contributors
DefaultDirName={localappdata}\Programs\CameraProfileStudio
DefaultGroupName=Camera Profile Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#ArtifactStem}-Setup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\CameraProfileStudio.exe
SetupLogging=yes
[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Tasks]
Name: "startmenu"; Description: "Create a Start menu shortcut"; Flags: unchecked
[Icons]
Name: "{group}\Camera Profile Studio"; Filename: "{app}\CameraProfileStudio.exe"; Tasks: startmenu
[Run]
Filename: "{app}\CameraProfileStudio.exe"; Description: "Launch Camera Profile Studio"; Flags: nowait postinstall skipifsilent
