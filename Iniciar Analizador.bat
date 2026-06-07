@echo off
title Analizador de Notas Estructuradas
cd /d "%~dp0backend"
echo.
echo   Analizador de Notas Estructuradas
echo   --------------------------------------------------
echo   Servidor:  http://127.0.0.1:8742
echo   (Se abre el navegador solo en unos segundos)
echo   Para APAGARLO: cerra esta ventana.
echo.
start "" cmd /c "timeout /t 3 /nobreak >nul & start "" http://127.0.0.1:8742"
"%~dp0.venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8742
echo.
echo   El servidor se detuvo. Podes cerrar esta ventana.
pause
