@echo off
rem Stops the attendance connector and removes it from Windows start-up. Run as Administrator.
schtasks /End /TN "Attendance connector"
schtasks /Delete /F /TN "Attendance connector"
echo Removed. Punches that were waiting are kept in connector-data.db and are sent when it runs again.
pause
