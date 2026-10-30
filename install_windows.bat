@echo off
rem DeskBot for Windows 11 (untested; Fedora is the main target).
cd /d "%~dp0"
where py >nul 2>nul || (echo Install Python 3.12+ from https://www.python.org first, then run this again. & pause & exit /b 1)
if not exist .venv\Scripts\python.exe py -3 -m venv .venv || (pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
> run_deskbot.bat echo @echo off
>> run_deskbot.bat echo cd /d "%%~dp0"
>> run_deskbot.bat echo start "" .venv\Scripts\pythonw.exe -m deskbot
echo.
echo Done. Double-click run_deskbot.bat to start DeskBot.
echo Tip: use headphones on Windows; there is no automatic echo cancellation.
pause
