@echo off
title NOVA Neural Brain — Initializing
color 0B

REM Always run from the directory this .bat lives in — fixes "package.json not
REM found" when launched from a different cwd (e.g. shell, scheduler, shortcut).
cd /d "%~dp0"

echo.
echo  ███╗   ██╗ ██████╗ ██╗   ██╗ █████╗
echo  ████╗  ██║██╔═══██╗██║   ██║██╔══██╗
echo  ██╔██╗ ██║██║   ██║██║   ██║███████║
echo  ██║╚██╗██║██║   ██║╚██╗ ██╔╝██╔══██║
echo  ██║ ╚████║╚██████╔╝ ╚████╔╝ ██║  ██║
echo  ╚═╝  ╚═══╝ ╚═════╝   ╚═══╝  ╚═╝  ╚═╝
echo.
echo  NEURAL BRAIN — Persistent Memory System
echo  =========================================
echo.

:: Check if node_modules exists
if not exist "node_modules\" (
    echo [1/3] Installing Electron dependencies...
    call npm install
    if errorlevel 1 (
        echo ERROR: npm install failed. Make sure Node.js is installed.
        pause
        exit /b 1
    )
    echo [1/3] Done.
) else (
    echo [1/3] Electron dependencies OK.
)

:: Check if Python packages installed
python -c "import fastapi, uvicorn, aiosqlite, httpx" 2>nul
if errorlevel 1 (
    echo [2/3] Installing Python dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: pip install failed. Make sure Python is installed.
        pause
        exit /b 1
    )
    echo [2/3] Done.
) else (
    echo [2/3] Python dependencies OK.
)

:: Check Ollama
echo [3/3] Checking Ollama...
curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo.
    echo  WARNING: Ollama is not running.
    echo  The brain will launch but AI responses won't work until Ollama is started.
    echo  To fix: Download Ollama from https://ollama.com then run: ollama pull llama3
    echo.
    timeout /t 3 >nul
) else (
    echo [3/3] Ollama online.
)

echo.
echo  Launching NOVA Neural Brain...
echo.

npm start
