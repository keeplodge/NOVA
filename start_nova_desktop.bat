@echo off
REM NOVA Assistant desktop launcher.
REM   1. cd into nova_ui\electron
REM   2. install electron deps on first run
REM   3. launch the Electron shell (which spawns the Python sidecar itself)

setlocal
cd /d "%~dp0nova_ui\electron"

if not exist node_modules (
  echo [nova] first-run: installing Electron dependencies...
  call npm install
)

echo [nova] starting NOVA Assistant desktop...
call npm start

endlocal
