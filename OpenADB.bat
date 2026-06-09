@echo off
setlocal EnableExtensions

for %%I in ("%~dp0.") do set "APP_DIR=%%~fI"
pushd "%APP_DIR%" >nul 2>&1
if errorlevel 1 (
  echo OpenADB launcher cannot open the program folder:
  echo %APP_DIR%
  pause
  exit /b 1
)

if defined APPDATA (
  set "LOG_DIR=%APPDATA%\OpenADB\logs"
) else (
  set "LOG_DIR=%APP_DIR%\OpenADB-data\logs"
)
mkdir "%LOG_DIR%" >nul 2>&1
set "LOG_FILE=%LOG_DIR%\openadb-launcher.log"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY_CMD="
set "PY_ARGS=-m openadb.main"
set "PY_DETACHED=0"

where pythonw.exe >nul 2>&1
if not errorlevel 1 (
  set "PY_CMD=pythonw.exe"
  set "PY_DETACHED=1"
)

if not defined PY_CMD (
  where pyw.exe >nul 2>&1
  if not errorlevel 1 (
    set "PY_CMD=pyw.exe"
    set "PY_ARGS=-3 -m openadb.main"
    set "PY_DETACHED=1"
  )
)

if not defined PY_CMD (
  where python.exe >nul 2>&1
  if not errorlevel 1 (
    set "PY_CMD=python.exe"
  )
)

if not defined PY_CMD (
  echo Python was not found. Install Python 3 and enable "Add python.exe to PATH".
  pause
  popd
  exit /b 1
)

if "%OPENADB_LAUNCHER_DRY_RUN%"=="1" (
  echo APP_DIR=%APP_DIR%
  echo LOG_FILE=%LOG_FILE%
  echo PY_CMD=%PY_CMD%
  echo PY_ARGS=%PY_ARGS%
  echo PY_DETACHED=%PY_DETACHED%
  popd
  exit /b 0
)

if "%PY_DETACHED%"=="1" (
  start "" /D "%APP_DIR%" %PY_CMD% %PY_ARGS% 1>nul 2>nul
  popd
  exit /b 0
) else (
  %PY_CMD% %PY_ARGS% >>"%LOG_FILE%" 2>&1
  if errorlevel 1 (
    echo.
    echo OpenADB exited with an error. Details were saved to:
    echo %LOG_FILE%
    pause
  )
  popd
  exit /b 0
)
