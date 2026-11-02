@echo off
rem Camera Profile Studio 0.2.7 - Windows build launcher. Double-click to run.
rem Uses Windows PowerShell 5.1 (the shell that produced the 0.2.0 release).
rem Options are passed through, e.g.:  Build-0.2.7.cmd -CheckOnly
rem                                    Build-0.2.7.cmd -BuildRoot C:\cpsb
setlocal
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Build-0.2.7.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
