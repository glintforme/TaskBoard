@echo off
rem ===== TaskBoard launcher (pure ASCII, double-click safe) =====
cd /d "%~dp0"
where pythonw >nul 2>nul
if not errorlevel 1 goto :use_pythonw
where python >nul 2>nul
if not errorlevel 1 goto :use_python
goto :no_python

:use_pythonw
start "" pythonw "%~dp0main.py"
goto :done

:use_python
start "" python "%~dp0main.py"
goto :done

:no_python
echo [ERROR] Python not found. Install Python 3 and add it to PATH.
pause
exit /b 1

:done
exit /b 0
