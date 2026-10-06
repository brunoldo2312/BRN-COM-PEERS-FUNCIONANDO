@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ============================================================
REM   iniciar_cliente.bat — BRN Node (Windows, modo CLIENTE) v2
REM ------------------------------------------------------------
REM   Compatível com: main.py v10.1 + p2p_auth v2 + wallet v8
REM
REM   Novidades v2:
REM     - Adiciona todas as variáveis de segurança
REM     - Corrige bug do teste de bootstrap
REM     - Não apaga identidade sem necessidade
REM     - Valida arquivos essenciais das atualizações de segurança
REM     - Gera brn_network.env completo
REM ============================================================

echo.
echo ============================================================
echo   BRN Node - Windows (modo CLIENTE) v2
echo ============================================================
echo.

REM ============================================================
REM   CONFIGURACOES - AJUSTE SE NECESSARIO
REM ============================================================
REM ---- Rede / identidade do nó ----
set "BRN_NODE_PASSWORD=senha-no-temp-123"
set "BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9"
set "BRN_BOOTSTRAP_PEERS=192.168.0.19:6001"

REM ---- Carteira (SENHA FORTE OBRIGATÓRIA) ----
set "BRN_WEB_PASS=senha-temp-123"
set "BRN_WALLET_SESSION_PASS=senha-forte-da-carteira-2026"

REM ---- Modo do nó ----
set "BRN_IS_ORIGIN=0"

REM ---- Segurança HTTP ----
set "BRN_HTTPS=1"
set "BRN_AUDIT_ENABLED=1"
set "BRN_AUDIT_LOG=audit.log"

REM ---- Autenticação P2P ----
set "BRN_P2P_AUTH=optional"
set "BRN_P2P_AUTH_WINDOW=30"
set "BRN_P2P_NONCE_CACHE=10000"

REM ---- Verificação de integridade no boot ----
set "BRN_SKIP_VERIFY=0"
set "BRN_VERIFY_STRICT=0"

REM ---- Outros ----
set "BRN_TRACKER=https://brn-tracker.onrender.com"

REM ============================================================
REM   1) Confirma que esta na pasta correta
REM ============================================================
if not exist "main.py" (
    echo [X] main.py nao encontrado nesta pasta.
    echo     Este .bat precisa estar na mesma pasta que main.py
    echo     Pasta atual: %CD%
    echo.
    pause
    exit /b 1
)

REM ============================================================
REM   2) Valida arquivos essenciais (inclui novos de seguranca)
REM ============================================================
echo [i] Validando arquivos...
set "FALTANDO="
for %%F in (
    main.py
    server.py
    blockchain.py
    wallet.py
    db.py
    p2p_unified.py
    p2p_auth.py
    security.py
    secure_store.py
    brn_config.py
    brn_logger.py
    checkpoints.py
    chain_validator.py
    app_wallet_v3.py
    index_wallet.html
) do (
    if not exist "%%F" (
        echo    [X] Faltando: %%F
        set "FALTANDO=!FALTANDO! %%F"
    ) else (
        echo    [ok] %%F
    )
)
if not "!FALTANDO!"=="" (
    echo.
    echo [X] Arquivos faltando:!FALTANDO!
    echo     Aplique as atualizacoes de seguranca antes de rodar.
    pause
    exit /b 1
)
echo.

REM ============================================================
REM   3) Verifica Python
REM ============================================================
where python >nul 2>nul
if errorlevel 1 (
    echo [X] Python nao encontrado no PATH.
    echo     Instale em https://python.org ^(marque "Add to PATH"^)
    pause
    exit /b 1
)
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set "PYVER=%%i"
echo [ok] Python !PYVER!
echo.

REM ============================================================
REM   4) Verifica dependencias Python (opcional, só checa)
REM ============================================================
python -c "import cryptography" 2>nul
if errorlevel 1 (
    echo [!] Biblioteca 'cryptography' ausente.
    echo     Instalando...
    python -m pip install --quiet cryptography argon2-cffi flask flask-cors requests orjson pywebview mnemonic
    if errorlevel 1 (
        echo [X] Falha ao instalar dependencias.
        pause
        exit /b 1
    )
)
echo [ok] Dependencias OK
echo.

REM ============================================================
REM   5) Exporta as variaveis (o Python le via os.environ)
REM ============================================================
set "PYTHONUNBUFFERED=1"

echo [i] Configuracao:
echo     NODE_PASSWORD       = !BRN_NODE_PASSWORD!
echo     WALLET_SESSION_PASS = !BRN_WALLET_SESSION_PASS!
echo     WEB_PASS            = !BRN_WEB_PASS!
echo     NETWORK_SECRET      = !BRN_NETWORK_SECRET:~0,16!...
echo     BOOTSTRAP_PEERS     = !BRN_BOOTSTRAP_PEERS!
echo     IS_ORIGIN           = !BRN_IS_ORIGIN!
echo     HTTPS               = !BRN_HTTPS!
echo     P2P_AUTH            = !BRN_P2P_AUTH!
echo.

REM ============================================================
REM   6) Gerencia node_identity.enc
REM      (NAO apaga por padrao - so se pedido explicitamente)
REM ============================================================
if exist "node_identity.enc" (
    echo [ok] node_identity.enc existente - mantendo
    echo      (para apagar: del node_identity.enc e rode de novo)
) else (
    echo [i] node_identity.enc sera criado no primeiro boot
)
echo.

REM ============================================================
REM   7) Atualiza bootstrap_peers.json
REM ============================================================
echo [i] Atualizando bootstrap_peers.json...
> "bootstrap_peers.json" echo ["!BRN_BOOTSTRAP_PEERS!"]
echo [ok] bootstrap_peers.json = ["!BRN_BOOTSTRAP_PEERS!"]
echo.

REM ============================================================
REM   8) Atualiza brn_network.env (completo, com seguranca)
REM ============================================================
echo [i] Atualizando brn_network.env...
> "brn_network.env" echo BRN_NETWORK_SECRET=!BRN_NETWORK_SECRET!
>>"brn_network.env" echo BRN_BOOTSTRAP_PEERS=!BRN_BOOTSTRAP_PEERS!
>>"brn_network.env" echo BRN_TRACKER=!BRN_TRACKER!
>>"brn_network.env" echo BRN_IS_ORIGIN=!BRN_IS_ORIGIN!
>>"brn_network.env" echo BRN_HTTPS=!BRN_HTTPS!
>>"brn_network.env" echo BRN_AUDIT_ENABLED=!BRN_AUDIT_ENABLED!
>>"brn_network.env" echo BRN_AUDIT_LOG=!BRN_AUDIT_LOG!
>>"brn_network.env" echo BRN_P2P_AUTH=!BRN_P2P_AUTH!
>>"brn_network.env" echo BRN_P2P_AUTH_WINDOW=!BRN_P2P_AUTH_WINDOW!
>>"brn_network.env" echo BRN_P2P_NONCE_CACHE=!BRN_P2P_NONCE_CACHE!
>>"brn_network.env" echo BRN_SKIP_VERIFY=!BRN_SKIP_VERIFY!
>>"brn_network.env" echo BRN_VERIFY_STRICT=!BRN_VERIFY_STRICT!
echo [ok] brn_network.env atualizado
echo.

REM ============================================================
REM   9) Testa se a Maquina A (Ubuntu) esta respondendo
REM ============================================================
echo [i] Testando conexao com !BRN_BOOTSTRAP_PEERS!...

REM Parse IP:porta (fix do bug do espaco em branco)
set "_HOST="
set "_PORT="
for /f "tokens=1,2 delims=:" %%a in ("!BRN_BOOTSTRAP_PEERS!") do (
    set "_HOST=%%a"
    set "_PORT=%%b"
)

REM Remove esquemas se existirem (sem espaco!)
set "_HOST=!_HOST:http://=!"
set "_HOST=!_HOST:https://=!"

if "!_HOST!"=="" (
    echo [!] Nao consegui extrair IP de !BRN_BOOTSTRAP_PEERS!
) else (
    powershell -NoProfile -Command ^
        "$r = Test-NetConnection -ComputerName '!_HOST!' -Port !_PORT! -WarningAction SilentlyContinue; if ($r.TcpTestSucceeded) { exit 0 } else { exit 1 }" >nul 2>nul

    if errorlevel 1 (
        echo [!] Nao consegui alcancar !BRN_BOOTSTRAP_PEERS!
        echo     Verifique:
        echo       - A Maquina A ^(Ubuntu^) esta rodando?
        echo       - O IP !_HOST! esta correto?
        echo       - Firewall da Ubuntu liberou a porta !_PORT!?
        echo       - Ambas as maquinas estao na mesma rede/Tailscale?
        echo.
        echo     Continuando mesmo assim ^(o main.py tentara sync sozinho^)...
        echo.
    ) else (
        echo [ok] Maquina A acessivel em !_HOST!:!_PORT!
        echo.
    )
)

REM ============================================================
REM  10) Verifica se ha arquivo legado em texto puro
REM ============================================================
if exist "current_wallet.json" (
    if not exist "current_wallet.enc" (
        echo [!] Detectado current_wallet.json em TEXTO PURO.
        echo     Migrando para formato cifrado...
        python -c "from wallet import WalletManager; import os; p=os.environ.get('BRN_WALLET_SESSION_PASS',''); print(WalletManager.migrate_legacy_current(p))"
        echo.
    )
)
if exist "node_identity.enc" (
    echo [ok] node_identity.enc presente
)

REM ============================================================
REM  11) Inicia o no
REM ============================================================
echo ============================================================
echo   Iniciando BRN Node em modo CLIENTE
echo ============================================================
echo   Ctrl+C para encerrar
echo.

python main.py --client-mode

set "EXITCODE=!ERRORLEVEL!"

echo.
echo ============================================================
if !EXITCODE! neq 0 (
    echo   [X] O no saiu com codigo !EXITCODE!
    echo.
    echo   Diagnostico:
    echo     - "Falha ao decifrar node_identity.enc":
    echo       a senha BRN_NODE_PASSWORD mudou. Restaure o backup
    echo       OU apague node_identity.enc e rode de novo.
    echo     - "Aguardando genesis":
    echo       verifique se a Maquina A ^(Ubuntu^) esta rodando.
    echo     - "BRN_WALLET_SESSION_PASS nao definido":
    echo       edite este .bat e preencha a senha da carteira.
    echo     - "Falha ao verificar cadeia":
    echo       veja chain_validator no log. Se corrompida,
    echo       set BRN_SKIP_VERIFY=1 temporariamente.
    echo.
) else (
    echo   [ok] No encerrado normalmente.
)
echo ============================================================
echo.
pause
endlocal
exit /b !EXITCODE!
