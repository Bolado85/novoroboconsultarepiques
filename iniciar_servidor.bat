@echo off
title Hapvida - Servidor Local
color 1F
cls

echo.
echo  =====================================================
echo   HAPVIDA - SERVIDOR LOCAL
echo   Conecta o dashboard online ao robo no seu PC
echo  =====================================================
echo.

cd /d "%~dp0"

:: Verificar Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERRO] Python nao encontrado!
    echo  Instale em: https://www.python.org/downloads/
    echo  Marque "Add Python to PATH" durante a instalacao!
    pause & exit /b
)

echo  Python encontrado!
echo.

:: Instalar dependencias
echo  Instalando dependencias...
python -m pip install fastapi uvicorn pyngrok --quiet --disable-pip-version-check
echo  Dependencias OK!
echo.

:: Iniciar servidor FastAPI em nova janela separada
echo  Abrindo servidor na porta 8000...
start "Hapvida API" cmd /k python servidor_local.py

:: Aguardar servidor subir
timeout /t 5 /nobreak >nul

:: Iniciar ngrok em script separado
echo  Iniciando tunel ngrok...
echo.
python ngrok_tunnel.py

pause
