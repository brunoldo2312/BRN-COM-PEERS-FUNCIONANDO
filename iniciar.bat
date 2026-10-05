@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ============================================================
REM   iniciar_cliente.bat — BRN Node (Windows, modo CLIENTE)
REM ------------------------------------------------------------
REM   Configura automaticamente:
REM     - BRN_NETWORK_SECRET (o mesmo da Maquina A/Ubuntu)
REM     - BRN_BOOTSTRAP_PEERS (IP da Maquina A)
REM     - BRN_NODE_PASSWORD   (identidade Ed25519)
REM     - BRN_WEB_PASS        (carteira)
REM   E roda: python main.py --client-mode
REM ============================================================

echo.
echo ============================================================
echo   BRN Node - Windows (modo CLIENTE)
echo ============================================================
echo.

REM ============================================================
REM   CONFIGURACOES - AJUSTE SE NECESSARIO
REM ============================================================
set "BRN_NODE_PASSWORD=senha-no-temp-123"
set "BRN_WEB_PASS=senha-temp-123"
set "BRN_NETWORK_SECRET=9226edea8ba62bc1c6ae36883c0536c95b4c85642195465d6c4d9456abef20f9"
set "BRN_BOOTSTRAP_PEERS=192.168.0.19:6001"

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

if not exist "p2p_unified.py" (
    echo [X] p2p_unified.py nao encontrado.
    echo     Arquivos do projeto incompletos.
    pause
    exit /b 1
)

echo [ok] Pasta correta: %CD%
echo.

REM ============================================================
REM   2) Verifica Python
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
REM   3) Exporta as variaveis (o Python le via os.environ)
REM ============================================================
set "PYTHONUNBUFFERED=1"

echo [i] Configuracao:
echo     NODE_PASSWORD       = !BRN_NODE_PASSWORD!
echo     WEB_PASS            = !BRN_WEB_PASS!
echo     NETWORK_SECRET      = !BRN_NETWORK_SECRET:~0,16!...
echo     BOOTSTRAP_PEERS     = !BRN_BOOTSTRAP_PEERS!
echo.

REM ============================================================
REM   4) Apaga node_identity.enc antigo (evita "senha errada")
REM ============================================================
if exist "node_identity.enc" (
    echo [i] Removendo node_identity.enc antigo ^(sera recriado^)...
    del /q "node_identity.enc" 2>nul
    del /q "node_identity.enc.bak" 2>nul
    del /q "node_identity.enc.antiga" 2>nul
    del /q "node_identity.enc.corrompida_*" 2>nul
    echo [ok] Removido
    echo.
)

REM ============================================================
REM   5) Atualiza bootstrap_peers.json (para o P2P achar a Maquina A)
REM ============================================================
echo [i] Atualizando bootstrap_peers.json...
> "bootstrap_peers.json" echo ["!BRN_BOOTSTRAP_PEERS!"]
echo [ok] bootstrap_peers.json = ["!BRN_BOOTSTRAP_PEERS!"]
echo.

REM ============================================================
REM   6) Atualiza brn_network.env (para o main.py carregar)
REM ============================================================
echo [i] Atualizando brn_network.env...
> "brn_network.env" echo BRN_NETWORK_SECRET=!BRN_NETWORK_SECRET!
>>"brn_network.env" echo BRN_TRACKER=https://brn-tracker.onrender.com
>>"brn_network.env" echo BRN_BOOTSTRAP_PEERS=!BRN_BOOTSTRAP_PEERS!
echo [ok] brn_network.env atualizado
echo.

REM ============================================================
REM   7) Testa se a Maquina A (Ubuntu) esta respondendo
REM ============================================================
echo [i] Testando conexao com !BRN_BOOTSTRAP_PEERS!...
for /f "tokens=1,2 delims=:" %%a in ("!BRN_BOOTSTRAP_PEERS!") do (
    set "_HOST=%%a"
    set "_PORT=%%b"
)

REM Extrai so o IP (remove http:// se houver)
set "_HOST=! _HOST:http://=!"
set "_HOST=!_HOST:https://=!"

powershell -NoProfile -Command ^
    "$r = Test-NetConnection -ComputerName !_HOST! -Port !_PORT! -WarningAction SilentlyContinue; if ($r.TcpTestSucceeded) { exit 0 } else { exit 1 }" >nul 2>nul

if errorlevel 1 (
    echo [!] Nao consegui alcancar !BRN_BOOTSTRAP_PEERS!
    echo     Verifique:
    echo       - A Maquina A ^(Ubuntu^) esta rodando?
    echo       - O IP !_HOST! esta correto?
    echo       - Firewall da Ubuntu liberou porta 6001?
    echo.
    echo     Continuando mesmo assim ^(o main.py tentara sync sozinho^)...
    echo.
) else (
    echo [ok] Maquina A acessivel na porta !_PORT!
    echo.
)

REM ============================================================
REM   8) Inicia o no
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
    echo     - Se erro foi "falha ao decifrar", rode novamente
    echo       ^(o .bat apaga a identidade automaticamente^)
    echo     - Se travou em "Aguardando genesis", verifique
    echo       se a Maquina A ^(Ubuntu^) esta rodando
    echo.
) else (
    echo   [ok] No encerrado normalmente.
)
echo ============================================================
echo.
pause
endlocal
exit /b !EXITCODE!