@echo off
rem Starts the attendance connector with Windows, in the background, even when nobody is signed in.
rem Run this as Administrator from the folder that has connector.exe and connector.json.
cd /d "%~dp0"
if not exist connector.json (
  echo connector.json is missing. Download it from the HR system ^(Devices, the device, Connector settings file^).
  pause
  exit /b 1
)
schtasks /Create /F /TN "Attendance connector" /SC ONSTART /RU SYSTEM /RL HIGHEST /TR "\"%~dp0connector.exe\" run --folder \"%~dp0.\""
schtasks /Run /TN "Attendance connector"
echo Installed. The connector now starts with Windows. Its log is connector.log in this folder.
pause
