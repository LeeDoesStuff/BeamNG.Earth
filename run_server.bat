@echo off
rem BeamNG.Earth: start the world server with the recommended options.
rem Double-click this file, then start BeamNG.drive. Keep this window open while you play.
cd /d "%~dp0"
rem local: the carriageways as crisp asphalt meshes under the lane lines, not painted into the 2 m ground (wavy edges)
if not defined BNGE_ROADS_IN_GROUND set BNGE_ROADS_IN_GROUND=0
if exist ".venv\Scripts\python.exe" goto run
echo The server is not installed yet. Running the installer first...
where py >nul 2>nul
if errorlevel 1 goto plainpython
py -3 install.py
goto checkinstall
:plainpython
python install.py
:checkinstall
if exist ".venv\Scripts\python.exe" goto run
echo.
echo The installer did not finish. Install Python 3.10+ from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then double-click run_server.bat again.
pause
exit /b 1
:run
".venv\Scripts\python.exe" start.py %*
echo.
echo The server has stopped.
pause
