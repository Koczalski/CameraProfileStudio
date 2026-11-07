@echo off
setlocal enabledelayedexpansion
rem Camera Profile Studio: launch the GUI or run the tests, always from this working tree (latest source).
rem   TestRun.bat          menu: 1 = launch the app (default after 5 s), 2 = gate, 3 = all tests
rem   TestRun.bat app      launch the GUI
rem   TestRun.bat gate     the completion gate (scripts\check.py), about 1 minute
rem   TestRun.bat all      the gate, then every test including the GUI tests, about 3 minutes
rem App runs are logged to testrun-logs\session-<date>-<time>.log (log panel, jobs, console output, crashes,
rem GUI-thread stalls, memory heartbeat). Send that file when something goes wrong.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo .venv was not found. Create it first:
    echo   py -3.12 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -e .[gui,raw,test]
    pause
    exit /b 1
)

rem Always import the package from .\src (not from a copy installed in the venv).
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

rem LittleCMS is not in the repository: use CPS_LCMS2_DLL if set, else a DLL from the build cache.
if not defined CPS_LCMS2_DLL (
    for /d %%D in ("%LOCALAPPDATA%\CameraProfileStudio-build-cache\lcms\*") do (
        if exist "%%D\lcms2.dll" set "CPS_LCMS2_DLL=%%D\lcms2.dll"
    )
)
if defined CPS_LCMS2_DLL (
    echo CPS_LCMS2_DLL=%CPS_LCMS2_DLL%
) else (
    echo CPS_LCMS2_DLL is not set. Features and tests that need lcms2.dll will fail. See scripts\build-lcms.ps1.
)

set "MODE=%~1"
if /i "!MODE!"=="test" set "MODE=all"
if not defined MODE (
    echo.
    echo   1 = launch the app    2 = gate, quick check    3 = all tests
    choice /c 123 /t 5 /d 1 /n /m "Select 1, 2 or 3 - default 1 in 5 s: "
    if errorlevel 3 (
        set "MODE=all"
    ) else if errorlevel 2 (
        set "MODE=gate"
    ) else (
        set "MODE=app"
    )
)

if /i "!MODE!"=="gate" (
    ".venv\Scripts\python.exe" scripts\check.py
    set "RC=!ERRORLEVEL!"
    echo.
    if "!RC!"=="0" (echo Finished: OK) else (echo Finished with failures, exit code !RC!)
    pause
    exit /b !RC!
)

if /i "!MODE!"=="all" (
    ".venv\Scripts\python.exe" scripts\check.py
    set "RC=!ERRORLEVEL!"
    ".venv\Scripts\python.exe" -m pytest -q
    if not "!ERRORLEVEL!"=="0" set "RC=!ERRORLEVEL!"
    echo.
    if "!RC!"=="0" (echo Finished: OK) else (echo Finished with failures, exit code !RC!)
    pause
    exit /b !RC!
)

rem Hands-on testing: the app writes a session log here (see gui\session_log.py).
set "CPS_SESSION_LOG=%~dp0testrun-logs"

rem Show exactly what is about to start, so the run can be checked against the code you just changed.
".venv\Scripts\python.exe" -c "import camera_profile_studio as c; print('Camera Profile Studio', c.__version__, 'from', c.__file__)"
if errorlevel 1 (
    echo The package could not be imported.
    pause
    exit /b 1
)

echo Session log folder: %CPS_SESSION_LOG%
".venv\Scripts\python.exe" -X faulthandler -m camera_profile_studio.gui.app
set "RC=!ERRORLEVEL!"
echo.
echo Session logs are in: %CPS_SESSION_LOG%
if not "!RC!"=="0" (
    echo The application exited with an error, exit code !RC!. The newest session log has the details.
    pause
)
