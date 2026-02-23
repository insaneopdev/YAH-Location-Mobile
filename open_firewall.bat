@echo off
:: HamLerc — Open Firewall for Mobile Navigation
:: Right-click this file → "Run as administrator"

echo ============================================
echo   Opening Windows Firewall for HamLerc...
echo ============================================

:: Remove old rule if exists (ignore errors)
netsh advfirewall firewall delete rule name="HamLerc Navigation Server" >nul 2>&1

:: Add inbound rule for TCP port 8000
netsh advfirewall firewall add rule name="HamLerc Navigation Server" dir=in action=allow protocol=TCP localport=8000

if %ERRORLEVEL%==0 (
    echo.
    echo   ✓ Firewall rule added successfully!
    echo   Port 8000 is now open for incoming connections.
    echo.
) else (
    echo.
    echo   ✗ Failed! Make sure you right-clicked
    echo     and chose "Run as administrator"
    echo.
)

pause
