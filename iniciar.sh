#!/usr/bin/env bash
# ============================================================
#   iniciar.sh - BRN Node CLIENTE (sincroniza da Maquina A)
# ============================================================
#   Equivalente ao iniciar.bat do Windows.
#   100% ASCII, sem acentos.
# ============================================================

set -o pipefail

# -------- Cores --------
if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_BLU=$'\033[34m'; C_RST=$'\033[0m'; C_BLD=$'\033[1m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_RST=""; C_BLD=""
fi

info()  { printf '%s[*]%s %s\n' "$C_BLU" "$C_RST" "$*"; }
ok()    { printf '%s[ok]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
warn()  { printf '%s[!]%s %s\n' "$C_YEL" "$C_RST" "$*"; }
err()   { printf '%s[X]%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }

# -------- Diretorio do script --------
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
cd "$SCRIPT_DIR" || { err "Nao consegui cd para $SCRIPT_DIR"; exit 1; }

echo "============================================================"
echo "  BRN Node - CLIENTE (sincroniza da Maquina A)"
echo "============================================================"
echo

# ============================================================
#  0) TAILSCALE (opcional - para VLANs separadas)
# ============================================================
if [ -f "brn_tailscale_setup.py" ]; then
    info "Verificando Tailscale..."
    if python3 brn_tailscale_setup.py --auto 2>/dev/null; then
        ok "Tailscale OK"
    else
        warn "Tailscale nao esta pronto."
        echo "         Para configurar agora: python3 brn_tailscale_setup.py"
        echo "         Continuando em 5s sem Tailscale..."
        sleep 5
    fi
else
    info "brn_tailscale_setup.py nao encontrado. Pulando."
fi
echo

# ============================================================
#  1) CARREGA brn_network.env
# ============================================================
if [ ! -f "brn_network.env" ]; then
    err "brn_network.env NAO existe"
    echo "     Copie da Maquina A antes de continuar."
    exit 1
fi

info "Carregando brn_network.env..."

# Le sem BOM, sem CRLF
set -a
while IFS='=' read -r k v || [ -n "$k" ]; do
    # Remove CR se vier do Windows
    k="$(echo -n "$k" | tr -d '\r\n')"
    v="$(echo -n "$v" | tr -d '\r\n')"
    [ -z "$k" ] && continue
    case "$k" in \#*) continue ;; esac
    export "$k=$v"
done < <(sed 's/\r$//' brn_network.env)
set +a

ok "brn_network.env carregado"
echo

# ============================================================
#  2) SENHAS LOCAIS
# ============================================================
: "${BRN_NODE_PASSWORD:=senha-no-local-2026}"
: "${BRN_WEB_PASS:=senha-carteira-2026}"
export BRN_NODE_PASSWORD
export BRN_WEB_PASS

# ============================================================
#  3) PARAMETROS OPERACIONAIS
# ============================================================
export BRN_NODE_AUTORESET=1
export BRN_MINER_AUTO=0
export BRN_MINER_INTERVAL=30
export BRN_ALLOW_SOLO_MINING=0
export BRN_MIN_PEER_STABLE=1
export BRN_UPNP=1
export BRN_P2P_AUTH=optional
export BRN_WEB_PORT=5000
export BRN_EXPLORER_PORT=8080
export BRN_P2P_PORT=6001
export BRN_SYNC_BATCH=500
export BRN_SYNC_PARALELO_MIN=500
export BRN_SYNC_PARALELO_WORKERS=4
export BRN_SYNC_RETRY_MAX=5
export BRN_TCP_TIMEOUT=30.0
export BRN_LOG_LEVEL=INFO
export PYTHONUNBUFFERED=1

# ============================================================
#  4) VERIFICA PYTHON
# ============================================================
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    err "Python 3 nao encontrado"
    echo "     Instale: sudo apt install -y python3 python3-venv python3-pip"
    exit 1
fi

PYVER=$($PY -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null)
ok "Python $PYVER ($(command -v $PY))"
echo

# ============================================================
#  5) ARQUIVOS ESSENCIAIS
# ============================================================
FALTA=""
for f in main.py server.py blockchain.py wallet.py db.py p2p_unified.py brn_config.py brn_logger.py; do
    [ -f "$f" ] || FALTA="$FALTA $f"
done
if [ -n "$FALTA" ]; then
    err "Arquivos faltando:$FALTA"
    exit 1
fi
ok "Arquivos essenciais OK"
echo

# ============================================================
#  6) PAPEL: CLIENTE
# ============================================================
printf 'cliente\n' > brn_role.txt
ok "Papel: cliente (nao origina genesis)"
echo

# ============================================================
#  7) BOOTSTRAP PEERS
# ============================================================
if [ ! -f "bootstrap_peers.json" ]; then
    echo '["177.82.132.98:6001"]' > bootstrap_peers.json
    ok "bootstrap_peers.json criado"
else
    ok "bootstrap_peers.json ja existe"
fi
echo

# ============================================================
#  8) FINGERPRINT DO SEGREDO
# ============================================================
if [ -n "${BRN_NETWORK_SECRET:-}" ]; then
    SEC_FP=$($PY -c "import hashlib,os;print(hashlib.sha256(os.environ['BRN_NETWORK_SECRET'].encode()).hexdigest()[:16])" 2>/dev/null)
else
    SEC_FP="(vazio)"
fi

# ============================================================
#  9) PYWEBVIEW (GUI ou headless)
# ============================================================
HEADLESS_FLAG=""
if ! $PY -c "import webview" >/dev/null 2>&1; then
    HEADLESS_FLAG="--headless"
    warn "pywebview ausente - modo HEADLESS"
else
    if ! $PY -c "import gi; gi.require_version('Gtk','3.0')" >/dev/null 2>&1; then
        warn "pywebview instalado mas faltam libs GTK - modo HEADLESS"
        HEADLESS_FLAG="--headless"
    elif [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
        warn "Sem display grafico - modo HEADLESS"
        HEADLESS_FLAG="--headless"
    else
        ok "pywebview OK - carteira desktop vai abrir"
    fi
fi
echo

# ============================================================
# 10) RESUMO
# ============================================================
echo "============================================================"
echo "  CONFIGURACAO ATUAL (CLIENTE)"
echo "============================================================"
echo "  Modo       : CLIENTE (sincroniza da rede)"
echo "  Auth FP    : $SEC_FP...  <- deve ser IGUAL ao da Maquina A"
echo "  Tracker    : ${BRN_TRACKER:-(nao definido)}"
echo "  Bootstrap  : ${BRN_BOOTSTRAP_PEERS:-(nao definido)}"
echo "  Mineracao  : $BRN_MINER_AUTO (pode ativar na carteira)"
echo "  Sync batch : $BRN_SYNC_BATCH (workers $BRN_SYNC_PARALELO_WORKERS)"
echo "  P2P porta  : $BRN_P2P_PORT"
echo "  HTTP       : http://127.0.0.1:$BRN_WEB_PORT"
echo "  Explorer   : http://127.0.0.1:$BRN_EXPLORER_PORT"
echo "  Python     : $PYVER"
echo "  pywebview  : $([ -z "$HEADLESS_FLAG" ] && echo GUI || echo HEADLESS)"
echo "============================================================"
echo
echo "  ATENCAO: deixe este terminal ABERTO enquanto o cliente roda."
echo

# ============================================================
# 11) INICIA O NO EM MODO CLIENTE
# ============================================================
info "Iniciando CLIENTE BRN... (Ctrl+C para encerrar)"
echo

cleanup() {
    echo
    warn "Encerrando no..."
    exit 130
}
trap cleanup INT TERM

# shellcheck disable=SC2086
$PY main.py --client-mode $HEADLESS_FLAG
EXITCODE=$?

echo
if [ "$EXITCODE" -ne 0 ]; then
    err "O no saiu com codigo $EXITCODE"
    echo
    echo "  Se o erro foi 'Falha ao decifrar node_identity.enc':"
    echo "    rm node_identity.enc && ./iniciar.sh"
    echo
    echo "  Se o erro foi 'TIMEOUT esperando genesis':"
    echo "    - Verifique se a Maquina A esta rodando"
    echo "    - Verifique se as duas estao na mesma rede/Tailscale"
    echo "    - Verifique se os segredos batem: cat brn_network.env"
    echo
else
    ok "No encerrado normalmente."
fi

# Se chamado por clique duplo, espera antes de fechar
if [ -t 0 ]; then
    read -r -p "Pressione ENTER para fechar..." _
fi

exit "$EXITCODE"