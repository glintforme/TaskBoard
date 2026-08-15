@echo off
rem ===== Create desktop shortcut (pure ASCII) =====
cd /d "%~dp0"
python install_shortcut.py
if errorlevel 1 (
  echo [ERROR] Failed to create shortcut. Make sure Python is on PATH.
)
pause
