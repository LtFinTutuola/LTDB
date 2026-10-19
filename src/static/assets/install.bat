@echo off
:: ==============================================================================
:: LTDB - Installer automatico di Produzione (Windows)
:: Repository: https://github.com/LtFinTutuola/LTDB
:: ==============================================================================
TITLE Installazione LTDB - Enterprise Setup
COLOR 0A
SETLOCAL EnableDelayedExpansion

echo ==============================================================================
echo LTDB - SCRIPT DI INSTALLAZIONE AUTOMATICA (WINDOWS)
echo ==============================================================================
echo.

:: ------------------------------------------------------------------------------
:: 1. Rilevamento automatico della cartella del progetto locale
:: ------------------------------------------------------------------------------
:: Priorita':
:: 1. Argomento passato da riga di comando (%1)
:: 2. Se eseguito da src\static\assets\ (3 livelli sopra)
:: 3. Se eseguito dalla radice del progetto
:: 4. Cartella di lavoro corrente (%CD%)
:: 5. Percorso predefinito C:\LTDB

set "TARGET_DIR="

if not "%~1"=="" (
    if exist "%~1\requirements.txt" (
        set "TARGET_DIR=%~f1"
    ) else if exist "%~1" (
        set "TARGET_DIR=%~f1"
    )
)

if "!TARGET_DIR!"=="" (
    if exist "%~dp0..\..\..\requirements.txt" (
        pushd "%~dp0..\..\.."
        set "TARGET_DIR=!CD!"
        popd
    ) else if exist "%~dp0requirements.txt" (
        pushd "%~dp0"
        set "TARGET_DIR=!CD!"
        popd
    ) else if exist "%CD%\requirements.txt" (
        set "TARGET_DIR=%CD%"
    ) else if exist "C:\LTDB\requirements.txt" (
        set "TARGET_DIR=C:\LTDB"
    ) else (
        pushd "%~dp0..\..\.." 2>nul && (set "TARGET_DIR=!CD!" & popd) || set "TARGET_DIR=C:\LTDB"
    )
)

:: Rimuove eventuale backslash finale
if "!TARGET_DIR:~-1!"=="\" set "TARGET_DIR=!TARGET_DIR:~0,-1!"

set "REPO_URL=https://github.com/LtFinTutuola/LTDB.git"
set "VENV_DIR=!TARGET_DIR!\venv"
set "NGROK_DIR=!TARGET_DIR!\ngrok"

echo [INFO] Cartella di destinazione LTDB: !TARGET_DIR!
echo.

:: ------------------------------------------------------------------------------
:: 2. Verifica Permessi Amministrativi (Opzionale)
:: ------------------------------------------------------------------------------
net session >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [WARNING] Lo script non e eseguito come Amministratore.
    echo [WARNING] Se necessario, concedi i permessi UAC durante l'installazione.
    echo.
)

:: ------------------------------------------------------------------------------
:: 3. Verifica / Installazione di Python
:: ------------------------------------------------------------------------------
echo [STEP 1/5] Verifica presenza di Python...
set "PYTHON_CMD="

python --version >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    set "PYTHON_CMD=python"
) ELSE (
    py --version >nul 2>&1
    IF !ERRORLEVEL! EQU 0 (
        set "PYTHON_CMD=py"
    )
)

IF "!PYTHON_CMD!"=="" (
    echo [INFO] Python non trovato nel PATH di sistema.
    echo [INFO] Tentativo di installazione automatica di Python 3.12 tramite winget...
    winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements >nul 2>&1
    IF !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Impossibile installare Python automaticamente con winget.
        echo [AZIONE] Per favore installa Python 3.10 o superiore manualmente (assicurati di selezionare "Add Python to PATH") e riavvia lo script.
        PAUSE
        EXIT /B 1
    )
    echo [OK] Python installato con successo.
    :: Aggiorna PATH per la sessione corrente
    set "PATH=%LocalAppData%\Programs\Python\Python312\;%LocalAppData%\Programs\Python\Python312\Scripts\;!PATH!"
    set "PYTHON_CMD=python"
) ELSE (
    FOR /F "tokens=*" %%g IN ('!PYTHON_CMD! --version 2^>^&1') DO echo [OK] Trovato %%g
)
echo.

:: ------------------------------------------------------------------------------
:: 4. Verifica della Cartella del Progetto (Locale o Fallback Clonazione)
:: ------------------------------------------------------------------------------
echo [STEP 2/5] Verifica della cartella del progetto LTDB...

IF NOT EXIST "!TARGET_DIR!" (
    mkdir "!TARGET_DIR!"
)

IF EXIST "!TARGET_DIR!\requirements.txt" (
    echo [OK] Cartella LTDB trovata in !TARGET_DIR! (copia locale gia presente).
    echo [INFO] Salto il download/clonazione del codice sorgente.
) ELSE (
    echo [INFO] Codice sorgente non trovato in !TARGET_DIR!.
    echo [INFO] Tentativo di download da GitHub (%REPO_URL%)...
    where git >nul 2>&1
    IF !ERRORLEVEL! EQU 0 (
        IF EXIST "!TARGET_DIR!\.git" (
            echo [INFO] Repository Git gia inizializzato. Aggiornamento codice con git pull...
            cd /d "!TARGET_DIR!"
            git pull origin main
        ) ELSE (
            echo [INFO] Clonazione del repository con Git...
            git clone %REPO_URL% "!TARGET_DIR!"
        )
    ) ELSE (
        echo [INFO] Git non trovato. Scaricamento archivio ZIP tramite PowerShell...
        powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/LtFinTutuola/LTDB/archive/refs/heads/main.zip' -OutFile '!TARGET_DIR!\repo.zip'"
        powershell -Command "Expand-Archive -Path '!TARGET_DIR!\repo.zip' -DestinationPath '!TARGET_DIR!\temp' -Force"
        xcopy /E /Y "!TARGET_DIR!\temp\LTDB-main\*" "!TARGET_DIR!\" >nul
        rmdir /S /Q "!TARGET_DIR!\temp"
        del /F /Q "!TARGET_DIR!\repo.zip"
        echo [OK] Codice sorgente estratto con successo.
    )
)
cd /d "!TARGET_DIR!"
echo.

:: ------------------------------------------------------------------------------
:: 5. Creazione Ambiente Virtuale, Dipendenze Pip e Database
:: ------------------------------------------------------------------------------
echo [STEP 3/5] Configurazione Virtual Environment Python e dipendenze...

IF NOT EXIST "!VENV_DIR!" (
    echo [INFO] Creazione Virtual Environment in !VENV_DIR!...
    !PYTHON_CMD! -m venv "!VENV_DIR!"
    IF !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Impossibile creare l'ambiente virtuale venv.
        PAUSE
        EXIT /B 1
    )
    echo [OK] Virtual Environment creato con successo.
) ELSE (
    echo [INFO] Virtual Environment gia esistente in !VENV_DIR!.
)

echo [INFO] Attivazione ambiente virtuale e aggiornamento pip...
call "!VENV_DIR!\Scripts\activate.bat"
python -m pip install --upgrade pip --quiet

IF EXIST "!TARGET_DIR!\requirements.txt" (
    echo [INFO] Installazione pacchetti da requirements.txt...
    pip install -r "!TARGET_DIR!\requirements.txt"
    IF !ERRORLEVEL! EQU 0 (
        echo [OK] Dipendenze installate con successo.
    ) ELSE (
        echo [ERROR] Errore durante l'installazione delle dipendenze da requirements.txt.
    )
) ELSE (
    echo [WARNING] requirements.txt non trovato. Installazione librerie base...
    pip install fastapi "uvicorn[standard]" pydantic pydantic-settings sqlalchemy alembic pyyaml requests
)

echo.
echo [INFO] Inizializzazione / Aggiornamento Database SQLite (Alembic)...
cd /d "!TARGET_DIR!"
alembic upgrade head
IF !ERRORLEVEL! EQU 0 (
    echo [OK] Schema del database aggiornato con successo.
) ELSE (
    echo [WARNING] Verifica o migrazione database terminata con codice !ERRORLEVEL!.
)
echo.

:: ------------------------------------------------------------------------------
:: 6. Verifica / Configurazione Ngrok (Opzionale per Tunnel Remoto)
:: ------------------------------------------------------------------------------
echo [STEP 4/5] Configurazione Ngrok (Opzionale per accesso remoto)...
IF NOT EXIST "!NGROK_DIR!" mkdir "!NGROK_DIR!"

where ngrok >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    echo [OK] Ngrok rilevato nel PATH di sistema.
) ELSE IF EXIST "!NGROK_DIR!\ngrok.exe" (
    echo [OK] Ngrok trovato in !NGROK_DIR!\ngrok.exe.
    set "PATH=!NGROK_DIR!;!PATH!"
) ELSE (
    echo [INFO] Ngrok non trovato. Tentativo di download per Windows...
    powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri 'https://bin.equinox.io/c/b4Pvu3rwcdy/ngrok-v3-stable-windows-amd64.zip' -OutFile '!NGROK_DIR!\ngrok.zip'; Expand-Archive -Path '!NGROK_DIR!\ngrok.zip' -DestinationPath '!NGROK_DIR!' -Force; Remove-Item '!NGROK_DIR!\ngrok.zip' -Force; exit 0 } catch { exit 1 }"
    IF !ERRORLEVEL! EQU 0 (
        echo [OK] Ngrok scaricato e installato in !NGROK_DIR!.
        set "PATH=!NGROK_DIR!;!PATH!"
    ) ELSE (
        echo [INFO] Download di Ngrok saltato o non riuscito. L'applicazione funzionera normalmente in locale.
    )
)

echo.
SET /P NGROK_TOKEN="Inserisci l'Authtoken di Ngrok (premi INVIO per saltare): "
IF NOT "!NGROK_TOKEN!"=="" (
    IF EXIST "!NGROK_DIR!\ngrok.exe" (
        "!NGROK_DIR!\ngrok.exe" config add-authtoken !NGROK_TOKEN!
    ) ELSE (
        where ngrok >nul 2>&1
        IF !ERRORLEVEL! EQU 0 (
            ngrok config add-authtoken !NGROK_TOKEN!
        )
    )
    echo [OK] Authtoken Ngrok configurato.
)
echo.

:: ------------------------------------------------------------------------------
:: 7. Generazione Script di Avvio (run_ltdb.bat & run_silent.vbs)
:: ------------------------------------------------------------------------------
echo [STEP 5/5] Generazione degli script di avvio...

:: Generazione di run_ltdb.bat nella cartella del progetto
(
    echo @echo off
    echo TITLE LTDB Application Runtime
    echo cd /d "%%~dp0"
    echo call "%%~dp0venv\Scripts\activate.bat"
    echo echo ==============================================================
    echo echo LTDB - AVVIO APPLICAZIONE
    echo echo ==============================================================
    echo echo [INFO] Avvio Server Backend FastAPI...
    echo start "LTDB Backend" /min cmd /k "python -m uvicorn src.main:app --host 0.0.0.0 --port 8000"
    echo timeout /t 3 ^>nul
    echo echo [INFO] Server attivo su: http://localhost:8000
    echo start http://localhost:8000
    echo echo.
    echo IF EXIST "%%~dp0ngrok\ngrok.exe" ^(
    echo     echo [INFO] Avvio tunnel Ngrok sulla porta 8000...
    echo     "%%~dp0ngrok\ngrok.exe" http 8000
    echo ^) ELSE ^(
    echo     where ngrok ^>nul 2^>^&1
    echo     IF NOT ERRORLEVEL 1 ^(
    echo         echo [INFO] Avvio tunnel Ngrok sulla porta 8000...
    echo         ngrok http 8000
    echo     ^) ELSE ^(
    echo         echo [INFO] Ngrok non configurato. L'applicazione e accessibile in locale su http://localhost:8000
    echo         echo Finestra di controllo LTDB attiva. Premi un tasto per chiudere.
    echo         pause ^>nul
    echo     ^)
    echo ^)
) > "!TARGET_DIR!\run_ltdb.bat"

:: Generazione di run_silent.vbs nella cartella del progetto
(
    echo Set WshShell = CreateObject^("WScript.Shell"^)
    echo Set fso = CreateObject^("Scripting.FileSystemObject"^)
    echo currentDir = fso.GetParentFolderName^(WScript.ScriptFullName^)
    echo WshShell.Run "cmd /c """ ^& currentDir ^& "\run_ltdb.bat""", 0, False
) > "!TARGET_DIR!\run_silent.vbs"

:: Copia di install.bat nella radice se eseguito da sottocartella
if not exist "!TARGET_DIR!\install.bat" (
    copy /y "%~f0" "!TARGET_DIR!\install.bat" >nul 2>&1
)

echo [OK] Script di avvio creato: !TARGET_DIR!\run_ltdb.bat
echo [OK] Script silenzioso creato: !TARGET_DIR!\run_silent.vbs
echo.
echo ==============================================================================
echo INSTALLAZIONE E CONFIGURAZIONE COMPLETATA CON SUCCESSO!
echo ==============================================================================
echo Cartella di installazione: !TARGET_DIR!
echo.
echo Per avviare l'applicazione puoi:
echo  1. Eseguire: !TARGET_DIR!\run_ltdb.bat
echo  2. Oppure eseguire (avvio in background): !TARGET_DIR!\run_silent.vbs
echo.
PAUSE