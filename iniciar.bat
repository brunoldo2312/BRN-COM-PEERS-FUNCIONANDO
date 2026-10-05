@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo   BRN Node - CLIENTE (sincroniza da Maquina A)
echo ============================================================
echo.

REM ============================================================
REM  0) TAILSCALE - garante conexao entre VLANs
REM ============================================================
if exist "brn_tailscale_setup.py" (
    echo [*] Verificando Tailscale...
    python brn_tailscale_setup.py --auto >nul 2>&1
    if errorlevel 1 (
        echo [!] Tailscale nao esta pronto.
        echo.
        echo     Para configurar agora, rode em outro terminal:
        echo         python brn_tailscale_setup.py
        echo.
        echo     Continuando em 5 segundos sem Tailscale...
        timeout /t 5 >nul
    ) else (
        echo [ok] Tailscale OK
    )
) else (
    echo [i] brn_tailscale_setup.py nao encontrado. Pulando.
)
echo.

REM ============================================================
REM  1) CARREGA brn_network.env
REM ============================================================
set "BRN_NETWORK_SECRET="
set "BRN_TRACKER="
set "BRN_BOOTSTRAP_PEERS="

if exist "brn_network.env" (
    echo [*] Carregando brn_network.env...
    for /f "usebackq tokens=1,* delims==" %%a in ("brn_network.env") do (
        if not "%%a"=="" set "%%a=%%b"
    )
    echo [ok] brn_network.env carregado
) else (
    echo [!] brn_network.env NAO existe
    echo     Copie da Maquina A antes de continuar.
    pause
    exit /b 1
)
echo.

REM ============================================================
REM  2) SENHAS LOCAIS
REM ============================================================
if not defined BRN_NODE_PASSWORD set "BRN_NODE_PASSWORD=senha-no-local-2026"
if not defined BRN_WEB_PASS set "BRN_WEB_PASS=senha-carteira-2026"

REM ============================================================
REM  3) PARAMETROS OPERACIONAIS
REM ============================================================
set "BRN_NODE_AUTORESET=1"
set "BRN_MINER_AUTO=0"
set "BRN_MINER_INTERVAL=30"
set "BRN_ALLOW_SOLO_MINING=0"
set "BRN_MIN_PEER_STABLE=1"
set "BRN_UPNP=1"
set "BRN_P2P_AUTH=optional"
set "BRN_WEB_PORT=5000"
set "BRN_EXPLORER_PORT=8080"
set "BRN_P2P_PORT=6001"
set "BRN_SYNC_BATCH=500"
set "BRN_SYNC_PARALELO_MIN=500"
set "BRN_SYNC_PARALELO_WORKERS=4"
set "BRN_SYNC_RETRY_MAX=5"
set "BRN_TCP_TIMEOUT=30.0"
set "BRN_LOG_LEVEL=INFO"
set "PYTHONUNBUFFERED=1"

REM ============================================================
REM  4) VERIFICA PYTHON
REM ============================================================
where python >nul 2>nul
if errorlevel 1 (
    echo [X] Python nao encontrado no PATH.
    echo     Instale em https://python.org
    echo     (marque "Add to PATH" durante a instalacao)
    pause
    exit /b 1
)
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set "PYVER=%%i"
echo [ok] Python !PYVER!
echo.

REM ============================================================
REM  5) ARQUIVOS ESSENCIAIS
REM ============================================================
set "FALTA="
for %%f in (main.py server.py blockchain.py wallet.py db.py p2p_unified.py brn_config.py brn_logger.py) do (
    if not exist "%%f" set "FALTA=!FALTA! %%f"
)
if not "!FALTA!"=="" (
    echo [X] Arquivos faltando:!FALTA!
    pause
    exit /b 1
)
echo [ok] Arquivos essenciais OK
echo.

REM ============================================================
REM  6) PAPEL: CLIENTE
REM ============================================================
echo cliente> brn_role.txt
echo [ok] Papel: cliente ^(nao origina genesis^)
echo.

REM ============================================================
REM  7) BOOTSTRAP PEERS
REM ============================================================
if not exist bootstrap_peers.json (
    echo ["177.82.132.98:6001"] > bootstrap_peers.json
    echo [ok] bootstrap_peers.json criado
) else (
    echo [ok] bootstrap_peers.json ja existe
)
echo.

REM ============================================================
REM  8) FINGERPRINT DO SEGREDO
REM ============================================================
for /f "delims=" %%i in ('python -c "import hashlib;print(hashlib.sha256('!BRN_NETWORK_SECRET!'.encode()).hexdigest()[:16])" 2^>nul') do set "SEC_FP=%%i"

REM ============================================================
REM  9) PYWEBVIEW
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
REM 10) RESUMO
REM ============================================================
echo ============================================================
echo   CONFIGURACAO ATUAL ^(CLIENTE^)
echo ============================================================
echo   Modo       : CLIENTE ^(sincroniza da rede^)
echo   Auth FP    : !SEC_FP!...  ^<- deve ser IGUAL ao da Maquina A
echo   Tracker    : !BRN_TRACKER!
echo   Bootstrap  : !BRN_BOOTSTRAP_PEERS!
echo   Mineracao  : !BRN_MINER_AUTO! ^(pode ativar na carteira^)
echo   Sync batch : !BRN_SYNC_BATCH! ^(workers !BRN_SYNC_PARALELO_WORKERS!^)
echo   P2P porta  : !BRN_P2P_PORT!
echo   HTTP       : http://127.0.0.1:!BRN_WEB_PORT!
echo   Explorer   : http://127.0.0.1:!BRN_EXPLORER_PORT!
echo ============================================================
echo.
echo   ATENCAO: deixe esta janela ABERTA enquanto o cliente roda.
echo.

REM ============================================================
REM 11) INICIA O NO EM MODO CLIENTE
REM ============================================================
echo [*] Iniciando CLIENTE BRN... ^(Ctrl+C para encerrar^)
echo.
python main.py --client-mode !HEADLESS_FLAG!

set "EXITCODE=!ERRORLEVEL!"
echo.
if !EXITCODE! neq 0 (
    echo [X] O no saiu com codigo !EXITCODE!
    echo.
    echo Se o erro foi "Falha ao decifrar node_identity.enc":
    echo   1. Rode: del node_identity.enc
    echo   2. Rode este .bat de novo
    echo.
    echo Se o erro foi "TIMEOUT esperando genesis":
    echo   - Verifique se a Maquina A esta rodando
    echo   - Verifique se as duas estao na mesma rede/Tailscale
    echo.
) else (
    echo [ok] No encerrado normalmente.
)
pause
endlocal