@echo off
rem Camera Profile Studio - builds the newest source version in this folder.
rem Keep this file; it simply runs the matching Build-<version>.cmd.
setlocal
cd /d "%~dp0"
call "%~dp0Build-0.3.0-alpha.1.cmd" %*
exit /b %ERRORLEVEL%
