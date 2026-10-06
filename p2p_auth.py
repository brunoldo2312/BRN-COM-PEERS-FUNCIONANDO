"""
p2p_auth.py — Autenticacao Ed25519 para handshake P2P do BRN (v2).

Nivel 2: cada mensagem P2P carrega um bloco "_auth" com a assinatura
Ed25519 do remetente sobre (domain || pub || nonce || ts || msg_hash).
NAO criptografa o trafego. So prova posse da chave privada E amarra
a assinatura ao payload, impedindo replay e payload swap.

Modos (env BRN_P2P_AUTH):
  off       - desabilitado (nao valida nada)
  optional  - valida se vier; aceita se nao vier (com aviso) [padrao]
  required  - rejeita quem nao autentica

Allowlist (env BRN_P2P_ALLOWLIST):
  hex,hex,...  - se preenchida, so aceita pubkeys nesta lista
  vazio        - aceita qualquer pubkey com assinatura valida

Janela (env BRN_P2P_AUTH_WINDOW):
  segundos de tolerancia no timestamp (padrao: 30)

Cache de nonces (env BRN_P2P_NONCE_CACHE):
  numero maximo de nonces lembrados (padrao: 10000). Anti-replay.

v2 (SEGURANÇA):
  + [CRITICO] Anti-replay via cache LRU de nonces (nao aceita repetido)
  + [CRITICO] Binding criptografico ao payload (msg_hash na assinatura)
  + [NOVO]    Funcao _msg_hash deterministica (ordena chaves, remove _auth)
  + [NOVO]    reset_nonce_cache() para testes
"""
import os
import time
import json
import hashlib
import secrets
import threading
from collections import OrderedDict

# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------
AUTH_MODE = os.environ.get("BRN_P2P_AUTH", "optional").lower()
ALLOWLIST_RAW = os.environ.get("BRN_P2P_ALLOWLIST", "").strip()
AUTH_WINDOW = int(os.environ.get("BRN_P2P_AUTH_WINDOW", "30"))
NONCE_CACHE_SIZE = int(os.environ.get("BRN_P2P_NONCE_CACHE", "10000"))

ALLOWLIST = set()
for x in ALLOWLIST_RAW.split(","):
    x = x.strip().lower()
    if x:
        ALLOWLIST.add(x)

DOMAIN = b"BRN-AUTH-v2|"

# Global: setado uma vez pelo P2PManager
_NODE_ID_PRIV = None


def set_node_id_priv(priv):
    """Registra a chave privada Ed25519 do no (chamado pelo P2PManager)."""
    global _NODE_ID_PRIV
    _NODE_ID_PRIV = priv


def get_node_id_priv():
    return _NODE_ID_PRIV


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------
def auth_enabled() -> bool:
    return AUTH_MODE != "off"


def auth_required() -> bool:
    return AUTH_MODE == "required"


def _payload(pub_hex: str, nonce_hex: str, ts: int, msg_hash_hex: str = "") -> bytes:
    """
    Bytes que sao realmente assinados.
    Se msg_hash_hex for fornecido, amarra a assinatura ao payload.
    """
    base = DOMAIN + f"{pub_hex}|{nonce_hex}|{ts}".encode()
    if msg_hash_hex:
        base += b"|" + msg_hash_hex.encode()
    return base


def _msg_hash(msg) -> str:
    """
    SHA256 deterministico do payload.
    - Remove _auth (nao pode se auto-assinar)
    - Remove _port (metadado de rede, nao faz parte da semantica)
    - Ordena chaves (sort_keys=True)
    """
    if msg is None:
        return ""
    try:
        payload = {k: v for k, v in msg.items() if k not in ("_auth", "_port")}
        canonical = json.dumps(
            payload, separators=(",", ":"), sort_keys=True, default=str
        ).encode()
        return hashlib.sha256(canonical).hexdigest()
    except Exception:
        return ""


# ------------------------------------------------------------------
# CACHE DE NONCES (anti-replay)
# ------------------------------------------------------------------
_nonce_lock = threading.Lock()
_seen_nonces = OrderedDict()


def _check_nonce(nonce_hex: str) -> bool:
    """
    True se o nonce e novo. False se ja foi visto (replay).
    Mantem um cache LRU limitado por NONCE_CACHE_SIZE.
    """
    with _nonce_lock:
        if nonce_hex in _seen_nonces:
            # Renova posicao (uso recente)
            _seen_nonces.move_to_end(nonce_hex)
            return False
        _seen_nonces[nonce_hex] = time.time()
        # Poda os mais antigos se exceder o limite
        while len(_seen_nonces) > NONCE_CACHE_SIZE:
            _seen_nonces.popitem(last=False)
        return True


def reset_nonce_cache():
    """Limpa o cache de nonces. Util para testes."""
    with _nonce_lock:
        _seen_nonces.clear()


# ------------------------------------------------------------------
# BUILD
# ------------------------------------------------------------------
def build_auth(node_id_priv=None, msg=None) -> dict:
    """
    Cria bloco _auth assinado pela chave privada Ed25519.
    Se `msg` for passado, a assinatura fica amarrada ao payload.
    """
    if node_id_priv is None:
        node_id_priv = _NODE_ID_PRIV
    if node_id_priv is None:
        raise RuntimeError("node_id_priv nao configurado")

    pub_hex = node_id_priv.public_key().public_bytes_raw().hex()
    nonce = secrets.token_bytes(32).hex()
    ts = int(time.time())
    mh = _msg_hash(msg)
    sig = node_id_priv.sign(_payload(pub_hex, nonce, ts, mh)).hex()

    out = {"pub": pub_hex, "nonce": nonce, "ts": ts, "sig": sig}
    if mh:
        out["msg_hash"] = mh
    return out


# ------------------------------------------------------------------
# VERIFY
# ------------------------------------------------------------------
def verify_auth(auth: dict, allowlist=None, msg=None) -> tuple:
    """
    Verifica um bloco _auth. Retorna (ok, motivo).

    Se `msg` for passado, exige binding criptografico ao payload.
    Sempre valida anti-replay (nonce unico) e janela de timestamp.
    """
    if not isinstance(auth, dict):
        return False, "auth nao e dict"

    for k in ("pub", "nonce", "ts", "sig"):
        if k not in auth:
            return False, f"auth faltando campo {k}"

    pub_hex = str(auth["pub"]).lower()
    nonce_hex = str(auth["nonce"])

    try:
        ts = int(auth["ts"])
    except Exception:
        return False, "ts invalido"

    delta = abs(time.time() - ts)
    if delta > AUTH_WINDOW:
        return False, f"ts fora da janela ({delta:.0f}s > {AUTH_WINDOW}s)"

    try:
        pub_bytes = bytes.fromhex(pub_hex)
        nonce_bytes = bytes.fromhex(nonce_hex)
        sig_bytes = bytes.fromhex(str(auth["sig"]))
    except Exception:
        return False, "hex invalido"

    if len(pub_bytes) != 32:
        return False, "pubkey deve ter 32 bytes (Ed25519)"
    if len(nonce_bytes) != 32:
        return False, "nonce deve ter 32 bytes"
    if len(sig_bytes) != 64:
        return False, "assinatura deve ter 64 bytes"

    # --- Allowlist ---
    al = ALLOWLIST if allowlist is None else allowlist
    if al and pub_hex not in al:
        return False, "pubkey nao esta na allowlist"

    # --- Anti-replay: nonce unico ---
    if not _check_nonce(nonce_hex):
        return False, "nonce ja visto (replay)"

    # --- Binding ao payload ---
    mh = ""
    if msg is not None:
        mh = _msg_hash(msg)
        expected_mh = str(auth.get("msg_hash", ""))
        if not mh:
            # Nao consegui hashear o payload -> rejeita por seguranca
            return False, "falha ao hashear payload"
        if expected_mh != mh:
            return False, "msg_hash nao corresponde ao payload"

    # --- Assinatura Ed25519 ---
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pk = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pk.verify(sig_bytes, _payload(pub_hex, nonce_hex, ts, mh))
    except Exception:
        return False, "assinatura Ed25519 invalida"

    return True, ""


# ------------------------------------------------------------------
# DIAGNOSTICO
# ------------------------------------------------------------------
def stats() -> dict:
    """Retorna estatisticas para debug."""
    with _nonce_lock:
        nonces = len(_seen_nonces)
    return {
        "mode":             AUTH_MODE,
        "window_s":         AUTH_WINDOW,
        "nonce_cache_size": NONCE_CACHE_SIZE,
        "nonces_cached":    nonces,
        "allowlist_active": bool(ALLOWLIST),
        "allowlist_count":  len(ALLOWLIST),
        "node_pub":         (_NODE_ID_PRIV.public_key().public_bytes_raw().hex()[:16] + "...")
                            if _NODE_ID_PRIV else "(nao setada)",
    }
