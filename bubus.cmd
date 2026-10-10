@echo off
setlocal
set "VENV=%~dp0.venv"
if not exist "%VENV%\Scripts\python.exe" (
    py -3.14 -m venv "%VENV%"
    if errorlevel 1 (
        echo Impossible de creer l'environnement Python local BUBUS2.0.
        exit /b 1
    )
)
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo Installation des dependances BUBUS2.0 impossible. Verifie la connexion Internet puis relance bubus.cmd.
    exit /b 1
)
set "PYTHONPATH=%~dp0src"
"%VENV%\Scripts\python.exe" -m bubus %*
exit /b %errorlevel%
