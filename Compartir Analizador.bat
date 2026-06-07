@echo off
title Compartir Analizador - link publico
cd /d "%~dp0backend"
set "EXE=%~dp0bin\cloudflared.exe"

if not exist "%EXE%" (
  echo Descargando cloudflared por unica vez...
  if not exist "%~dp0bin" mkdir "%~dp0bin"
  powershell -Command "Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '%EXE%' -UseBasicParsing"
)

echo Iniciando el servidor...
start "" /min "%~dp0.venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8742
powershell -Command "Start-Sleep -Seconds 4" >nul

echo.
echo  ==================================================================
echo    Abajo aparece tu LINK PUBLICO:  https://....trycloudflare.com
echo    Copialo y compartilo. Para apagar todo, cerra esta ventana.
echo  ==================================================================
echo.
"%EXE%" tunnel --url http://127.0.0.1:8742
pause
