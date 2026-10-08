@echo off
rem Incremind one-click start: double-click this file, close its window to stop.
rem Options: start.bat -DataRoot D:\IncremindData   start.bat -NoBrowser
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start.ps1" %*
if errorlevel 1 pause
