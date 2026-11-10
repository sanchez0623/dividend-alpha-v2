@echo off
REM dividend-alpha local backend (API port from .env DA_API_PORT, default 8100)
REM Keep this file ASCII-only: cmd parses .bat as GBK on zh-CN Windows.
cd /d "%~dp0"

if not exist ".env" (
  echo [ERROR] .env not found. Copy .env.example to .env first.
  pause
  exit /b 1
)

REM Parse .env: KEY=VALUE, skip blank lines and comments (# / REM)
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
  if not "%%A"=="" if not "%%B"=="" set "%%A=%%B"
)

if "%DA_API_PORT%"=="" set "DA_API_PORT=8100"
if "%DA_API_HOST%"=="" set "DA_API_HOST=127.0.0.1"
set "PYTHONPATH=%~dp0src"

echo ============================================
echo  dividend-alpha v3.0 local backend
echo  API :  http://127.0.0.1:%DA_API_PORT%/da/v1/health
echo  Docs:  http://127.0.0.1:%DA_API_PORT%/da/v1/docs
echo  Frontend preview: double-click web\preview.html
echo  Press Ctrl+C to stop
echo ============================================

python -m uvicorn src.da_api.main:app --host %DA_API_HOST% --port %DA_API_PORT%
if errorlevel 1 (
  echo.
  echo [ERROR] uvicorn exited. Check that 'python' is on PATH.
  pause
)
