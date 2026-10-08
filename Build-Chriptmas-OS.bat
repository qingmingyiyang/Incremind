@echo off
setlocal
cd /d "%~dp0"
title Chriptmas OS Windows Build

echo Building a verified Chriptmas OS candidate...
echo This runs integrity checks and a real packaged startup smoke.
echo Run npm run build:windows:gate for the final full Electron E2E gate.
node "apps\desktop-electron\scripts\build-windows-candidate.cjs" --dir
if errorlevel 1 (
  echo.
  echo Build failed. Review the error above.
  pause
  exit /b 1
)

echo.
echo Build succeeded:
echo %~dp0apps\desktop-electron\release\win-unpacked\Chriptmas OS.exe
pause
