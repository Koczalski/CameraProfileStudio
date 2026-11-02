#ifndef PayloadDir
  #error PayloadDir must point to the standalone directory
#endif
#ifndef OutputDir
  #error OutputDir must be supplied
#endif
#ifndef ReleaseVersion
  #define ReleaseVersion "0.1.0"
#endif
#ifndef BlockThreads
  #define BlockThreads 4
#endif
#ifndef ArtifactStem
  #define ArtifactStem "CameraProfileStudio"
#endif
; Named mutex the application creates at start-up (camera_profile_studio.updates.APP_MUTEX).
#define AppMutexName "CameraProfileStudio_SingleInstance"
; File version needs four numeric parts: MAJOR.MINOR.PATCH[-suffix] becomes MAJOR.MINOR.PATCH.0
#if Pos("-", Str(ReleaseVersion)) > 0
  #define VersionCore Copy(Str(ReleaseVersion), 1, Pos("-", Str(ReleaseVersion)) - 1)
#else
  #define VersionCore Str(ReleaseVersion)
#endif
[Setup]
AppId={{B15FF861-C98A-4F3F-B46D-143C62935FA3}
AppName=Camera Profile Studio
AppVersion={#ReleaseVersion}
VersionInfoVersion={#VersionCore}.0
AppPublisher=Camera Profile Studio Contributors
DefaultDirName={localappdata}\Programs\CameraProfileStudio
; In-place upgrade (same AppId): reuse the folder of the installed version, close the running app
; (the in-app updater starts Setup with /SILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS) and restart it afterwards.
UsePreviousAppDir=yes
CloseApplications=yes
RestartApplications=yes
AppMutex={#AppMutexName}
DefaultGroupName=Camera Profile Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#ArtifactStem}-Setup
; Build speed first: lzma2/fast, split into blocks that are compressed on BlockThreads threads (the build passes the
; logical processor count with /DBlockThreads; Inno Setup 6.0.5 accepts 1-256).
Compression=lzma2/fast
SolidCompression=yes
LZMANumBlockThreads={#BlockThreads}
LZMAUseSeparateProcess=yes
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
; In-app update (silent): the app has already quit, so Restart Manager has nothing to restart; start the new version.
Filename: "{app}\CameraProfileStudio.exe"; Flags: nowait skipifnotsilent; Check: RestartAfterUpdate
[Code]
function RestartAfterUpdate: Boolean;
begin
  { /CPSRELAUNCH=1 comes from the in-app updater; do not start a second copy if Restart Manager already did. }
  Result := (ExpandConstant('{param:CPSRELAUNCH|0}') = '1') and not CheckForMutexes('{#AppMutexName}');
end;
