@echo off
REM Windows one-click entry: double-click this file.
REM All the Chinese output comes from bootstrap.py (Python handles UTF-8 fine).
REM This launcher keeps its own messages ASCII-only, because cmd.exe parses
REM batch files in the console's current code page and CJK here would garble.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  where python3 >nul 2>nul && set "PY=python3"
)

if not defined PY (
  echo [X] Python not found.
  echo     Please install Python 3.10+ first:
  echo       https://www.python.org/downloads/windows/
  echo     IMPORTANT: tick "Add python.exe to PATH" during installation.
  echo     Or, if you have winget:  winget install -e --id Python.Python.3.12
  echo.
  pause
  exit /b 1
)

%PY% bootstrap.py %*
set "CODE=%ERRORLEVEL%"

echo.
echo --------------------------------------------
echo Exit code: %CODE%
if "%CODE%"=="0" (
  echo The wizard finished and the server stopped normally.
) else (
  echo The wizard did not finish cleanly - please screenshot the output above.
)
echo --------------------------------------------
pause
exit /b %CODE%
