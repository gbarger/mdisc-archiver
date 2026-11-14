@echo off
rem Double-click to start M-Disc Archiver.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw mdisc_archiver.py %* & exit /b)
where pythonw >nul 2>nul && (start "" pythonw mdisc_archiver.py %* & exit /b)
echo Python 3 was not found. Install it from https://www.python.org/downloads/windows/
pause
