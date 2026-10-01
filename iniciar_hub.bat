@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ============================================================
REM BRN Node - HUB CENTRAL - MODO 1 CLIQUE
REM ============================================================
echo ============================================================
echo BRN Node - HUB CENTRAL - Instalacao Automatica
echo ============================================================
echo.

REM ============================================================
REM >>> SENHAS - AJUSTE AQUI (APENAS VOCE) <<<
REM ============================================================
set "BRN_NODE_PASSWORD="senha-da-carteira-2026""
set "BRN_WEB_PASS="senha-da-carteira-2026""
set "BRN_NETWORK_SECRET=brunocoin-lan-2026"
set "BRN_TRACKER=https://brn-tracker.onrender.com"
set "BRN_BOOTSTRAP_PEERS=177.82.132.98:6001"
set "BRN_MINER_AUTO=1"
set "BRN_MINER_INTERVAL=30"
set "BRN_UPNP=1"
set "BRN_P2P_AUTH=optional"
set "BRN_WEB_PORT=5000"
set "BRN_EXPLORER_PORT=8080"
set "BRN_P2P_PORT=6001"
set "PYTHONUNBUFFERED=1"

REM ============================================================
REM 1) VERIFICA PYTHON
REM ============================================================
where python >nul 2>nul
if errorlevel 1 (
    echo [X] Python nao encontrado.
    echo Baixe em https://python.org e marque "Add to PATH"
    pause
    exit /b 1
)
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set "PYVER=%%i"
echo [ok] Python!PYVER!
echo.

REM ============================================================
REM 2) CONFIGURACAO AUTOMATICA PARA CLIENTE LEIGO - NOVO
REM ============================================================
echo [*] Verificando configuracao inicial...

REM Se existe.env.example e nao existe.env, cria automaticamente
if not exist.env (
    if exist.env.example (
        echo [i] Criando.env a partir do.env.example...
        copy /Y.env.example.env >nul
        echo [ok].env criado automaticamente
    ) else (
        echo [i] Criando.env basico...
        (
            echo # Configurado automaticamente
            echo BRN_NODE_PASSWORD=!BRN_NODE_PASSWORD!
            echo BRN_WEB_PASS=!BRN_WEB_PASS!
            echo BRN_NETWORK_SECRET=!BRN_NETWORK_SECRET!
            echo BRN_TRACKER=!BRN_TRACKER!
            echo REDE=testnet
            echo INFISICAL_TOKEN=
            echo INFISICAL_PROJECT_ID=
            echo BTC_WIF=
            echo BTC_SEED=
        ) >.env
        echo [ok].env basico criado
    )
) else (
    echo [ok].env ja existe
)

REM Se existe setup.py, roda para gerar carteira sem precisar saber tecnico
if exist setup.py (
    if not exist wallets\ (
        echo [*] Primeira vez - gerando carteira automatica...
        python setup.py --auto >nul 2>nul
        echo [ok] Carteira gerada
    )
)
echo.

REM ============================================================
REM 3) INSTALA DEPENDENCIAS (com as novas do Infisical)
REM ============================================================
if exist requirements.txt (
    echo [*] Instalando dependencias...
    python -m pip install --quiet --disable-pip-version-check -r requirements.txt
) else (
    echo [*] Instalando dependencias padrao + Infisical...
    python -m pip install --quiet flask flask-cors requests orjson cryptography argon2-cffi mnemonic pywebview python-dotenv infisical-python bitcoinlib
)
echo [ok] Dependencias prontas
echo.

REM ============================================================
REM 4) VERIFICA ARQUIVOS ESSENCIAIS
REM ============================================================
set "FALTA="
for %%f in (main.py server.py blockchain.py wallet.py db.py p2p_unified.py) do (
    if not exist "%%f" set "FALTA=!FALTA! %%f"
)
if not "!FALTA!"=="" (
    echo [X] Arquivos faltando:!FALTA!
    pause
    exit /b 1
)
echo [ok] Arquivos essenciais presentes
echo.

REM ============================================================
REM 5) VERIFICA / CRIA IDENTIDADE
REM ============================================================
if exist node_identity.enc (
    echo [i] node_identity.enc JA EXISTE
) else (
    echo [i] node_identity.enc sera criada agora
)
echo.

REM ============================================================
REM 6) BOOTSTRAP PEERS
REM ============================================================
if not exist bootstrap_peers.json (
    echo ["177.82.132.98:6001"] > bootstrap_peers.json
    echo [ok] bootstrap_peers.json criado
) else (
    echo [ok] bootstrap_peers.json ja existe
)
echo.

REM ============================================================
REM 7) TESTA TRACKER
REM ============================================================
echo [*] Testando tracker!BRN_TRACKER!...
python -c "import urllib.request; r=urllib.request.urlopen('!BRN_TRACKER!/', timeout=10); print('[ok]', r.read().decode().strip())" 2>nul
if errorlevel 1 (
    echo [!] Tracker nao respondeu (pode estar dormindo)
)
echo.

REM ============================================================
REM 8) VERIFICA pywebview
REM ============================================================
set "HEADLESS_FLAG="
python -c "import webview" 2>nul
if errorlevel 1 (
    set "HEADLESS_FLAG=--headless"
    echo [!] pywebview ausente - modo HEADLESS
) else (
    echo [ok] pywebview OK - carteira desktop vai abrir
)
echo.

REM ============================================================
REM 9) RESUMO
REM ============================================================
echo ============================================================
echo CONFIGURACAO ATUAL - CLIENTE NAO PRECISA MEXER
echo ============================================================
echo Rede :!BRN_NETWORK_SECRET!
echo Tracker :!BRN_TRACKER!
echo P2P porta :!BRN_P2P_PORT!
echo HTTP : http://127.0.0.1:!BRN_WEB_PORT!
echo Explorer : http://127.0.0.1:!BRN_EXPLORER_PORT!
echo ============================================================
echo.
echo Tudo configurado! Deixe esta janela ABERTA.
echo.

REM ============================================================
REM 10) INICIA O NO
REM ============================================================
echo [*] Iniciando HUB BRN... (Ctrl+C para encerrar)
echo.
python main.py!HEADLESS_FLAG!

set "EXITCODE=!ERRORLEVEL!"
echo.
if!EXITCODE! neq 0 (
    echo [X] O no saiu com codigo!EXITCODE!
    echo Se foi "Falha ao decifrar node_identity.enc":
    echo ren node_identity.enc node_identity.enc.antiga
    echo e rode de novo
) else (
    echo [ok] No encerrado normalmente.
)
pause
endlocal