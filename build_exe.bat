@echo off
rem Build a single-file NexusSwitch.exe with PyInstaller.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt pyinstaller || goto :fail
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --noupx --console --name NexusSwitch main.py || goto :fail

echo.
echo Built dist\NexusSwitch.exe
exit /b 0

:fail
echo Build failed.
exit /b 1
