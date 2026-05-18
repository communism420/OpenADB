@echo off
setlocal
cd /d "%~dp0"
python -m openadb.main
if errorlevel 1 (
  echo.
  echo OpenADB exited with an error. Check the log folder shown in Settings, or openadb-crash.log in the OpenADB logs folder.
  pause
)
