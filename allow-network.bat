@echo off
net session >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Right-click this file and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)
netsh advfirewall firewall delete rule name="LawBoard 8780" >nul 2>nul
netsh advfirewall firewall add rule name="LawBoard 8780" dir=in action=allow protocol=TCP localport=8780 profile=any
powershell -NoProfile -Command "Get-NetConnectionProfile | Where-Object {$_.NetworkCategory -eq 'Public'} | Set-NetConnectionProfile -NetworkCategory Private"
echo.
echo  Done. Other office computers can now connect.
echo  Use the address shown in the start.bat window, next to "other computers".
echo.
pause
