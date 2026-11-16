@echo off
rem ===================================================================
rem  bili-trim launcher. Double-click this file to start the program.
rem
rem  All it does: find a usable Python, then hand run.py to it.
rem  Missing Python packages are handled by run.py; missing adb/ffmpeg
rem  are handled by the program's own environment-check window.
rem
rem  This file is deliberately pure ASCII and does NOT call chcp.
rem  Under "chcp 65001" cmd.exe walks the file by byte offset while
rem  decoding it as UTF-8, so any multi-byte line makes it drift and
rem  it starts running fragments of comments as commands, or silently
rem  skips the next line. Both were observed here. Python sets the
rem  console to UTF-8 itself, so everything the user reads is fine.
rem
rem  Must stay CRLF: with LF line endings, goto cannot find labels.
rem ===================================================================

cd /d "%~dp0"

rem python-path.txt is written by setup.py: the interpreter the packages
rem were installed into. One machine often has several Pythons.
set PY=
if exist python-path.txt set /p PY=<python-path.txt
if not defined PY goto findpy
if exist "%PY%" goto run
set PY=

:findpy
where py >nul 2>&1
if %errorlevel%==0 set PY=py -3
if defined PY goto run
where python >nul 2>&1
if %errorlevel%==0 set PY=python
if defined PY goto run
goto nopython

:run
%PY% run.py %*
rem 9009 = the command itself could not start, so run.py never spoke.
if errorlevel 9009 pause
goto :eof

:nopython
echo.
echo   Python not found. Install it, then double-click this file again.
echo.
echo       winget install -e --id Python.Python.3.12
echo.
echo   Or download it from https://www.python.org/downloads/ and tick
echo   "Add python.exe to PATH" at the bottom of the installer.
echo.
echo   (Chinese version of these steps: see README.md)
echo.
pause
exit /b 1
