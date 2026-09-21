@echo off
rem Build epub-font-unlock.exe (single file, no console) with PyInstaller.
rem Usage: build.bat          -> dist\epub-font-unlock.exe
setlocal
cd /d "%~dp0"

python -m PyInstaller --version >nul 2>&1 || python -m pip install pyinstaller
python -m pip install -r requirements.txt

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name epub-font-unlock ^
  --add-data "lang;lang" ^
  --collect-data tkinterdnd2 ^
  main.py

if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo.
echo Done: dist\epub-font-unlock.exe
endlocal
