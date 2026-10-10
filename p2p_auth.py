"""
p2p_auth.py — Autenticação Ed25519 para o P2P BRN (v2)
================================================================
Fornece:
  - auth_enabled()         → bool: autenticação ligada?
  - auth_required()        → bool: autenticação obrigatória?
  - build_auth(msg=...)    → dict: assina o hash do payload
  - verify_auth(auth,msg=) → (bool, str): verifica assinatura + hash + replay
  - set_node_id_priv(priv) → registra chave privada do nó

Modos (env BRN_P2P_AUTH):
  "off"      → desliga autenticação
  "optional" → aceita peers sem auth (default)
  "required" → rejeita peers sem auth

Anti-replay:
  - Cada auth tem `ts` (timestamp Unix)
  - Janela máxima: BRN_P2P_AUTH_WINDOW segundos (default 30)
  - Cache de nonces: BRN_P2P_NONCE_CACHE entradas (default 10000)
  - Nonces repetidos na janela são rejeitados

Anti payload-swap:
  - O payload é serializado de forma canônica (JSON sort_keys, sem espaços)
  - O hash SHA256 do payload entra na mensagem assinada
  - Se um MITM trocar o payload mantendo o `_auth`, o hash muda e a
    verificação falha
================================================================
"""
from __future__ import annotations

import os
import json
import time
import hmac
import hashlib
import logging
import threading
from collections import OrderedDict

log = logging.getLogger("p2p_auth")

# ============================================================
# CONFIG
# ============================================================
_AUTH_MODE = os.environ.get("BRN_P2P_AUTH", "optional").strip().lower()
_AUTH_WINDOW = int(os.environ.get("BRN_P2P_AUTH_WINDOW", "30"))
_NONCE_CACHE_SIZE = int(os.environ.get("BRN_P2P_NONCE_CACHE", "10000"))

# Domínio separador (evita cross-protocol reuse)
_DOMAIN = b"BRN-P2P-AUTH-v2|"

# ============================================================
# ESTADO GLOBAL
# ============================================================
_node_id_priv = None      # Ed25519PrivateKey
_node_id_pub_hex = ""     # str (hex)

# Cache de nonces: OrderedDict para LRU
_nonce_cache: "OrderedDict[str, float]" = OrderedDict()
_nonce_lock = threading.RLock()


# ============================================================
# CONFIGURAÇÃO PÚBLICA
# ============================================================
def auth_enabled() -> bool:
    """Autenticação está ligada?"""
    return _AUTH_MODE in ("optional", "required")


def auth_required() -> bool:
    """Autenticação é obrigatória?"""
    return _AUTH_MODE == "required"


def set_node_id_priv(priv) -> None:
    """
    Registra a chave privada Ed25519 deste nó.
    Chamado pelo main.py no boot.
    """
    global _node_id_priv, _node_id_pub_hex
    _node_id_priv = priv
    try:
        _node_id_pub_hex = priv.public_key().public_bytes_raw().hex()
    except Exception:
        _node_id_pub_hex = ""
    log.info("p2p_auth: chave registrada pub=" + _node_id_pub_hex[:16] + "...")


def get_node_id_pub() -> str:
    """Retorna a pubkey hex do próprio nó."""
    return _node_id_pub_hex


# ============================================================
# HELPERS INTERNOS
# ============================================================
def _canonical_json(obj) -> bytes:
    """
    Serialização canônica para assinatura.
    Ordena chaves, remove espaços, força UTF-8.
    """
    return json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _msg_hash(msg) -> str:
    """
    SHA256 hex do payload canônico.
    Remove '_auth' se presente (não assina a própria assinatura).
    """
    if msg is None:
        return "0" * 64
    try:
        clean = {k: v for k, v in msg.items() if k != "_auth"}
        return hashlib.sha256(_canonical_json(clean)).hexdigest()
    except Exception:
        return "0" * 64


def _sign(data: bytes) -> str:
    """Assina com a chave privada do nó. Retorna hex."""
    if _node_id_priv is None:
        raise RuntimeError("p2p_auth: chave privada não registrada")
    sig = _node_id_priv.sign(data)
    if isinstance(sig, (bytes, bytearray)):
        return sig.hex()
    return str(sig)


def _verify_sig(pub_hex: str, sig_hex: str, data: bytes) -> bool:
    """Verifica assinatura Ed25519. Aceita pubkey e sig em hex."""
    try:
        from crypto import Ed25519PublicKey
    except ImportError:
        log.warning("p2p_auth: crypto.Ed25519PublicKey indisponível")
        return False

    try:
        pub_bytes = bytes.fromhex(pub_hex)
        sig_bytes = bytes.fromhex(sig_hex)
        pk = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pk.verify(sig_bytes, data)
        return True
    except Exception:
        return False


# ============================================================
# CACHE DE NONCES (anti-replay)
# ============================================================
def _nonce_check(nonce: str, ts: int) -> bool:
    """
    Verifica se o nonce é novo.
    Retorna True se aceito (novo), False se já visto.
    """
    if not nonce:
        return True

    key = nonce + "|" + str(ts // _AUTH_WINDOW)  # bucket por janela
    now = time.time()

    with _nonce_lock:
        # Limpa expirados
        cutoff = now - (_AUTH_WINDOW * 2)
        while _nonce_cache:
            k, t = next(iter(_nonce_cache.items()))
            if t < cutoff:
                _nonce_cache.popitem(last=False)
            else:
                break

        # Já visto?
        if key in _nonce_cache:
            return False

        # Adiciona
        _nonce_cache[key] = now

        # Limita tamanho (LRU)
        while len(_nonce_cache) > _NONCE_CACHE_SIZE:
            _nonce_cache.popitem(last=False)

        return True


# ============================================================
# BUILD AUTH
# ============================================================
def build_auth(msg=None) -> dict:
    """
    Cria o bloco _auth para um payload.

    Retorna:
        {
          "pub": "<hex pubkey>",
          "ts": <unix ts>,
          "nonce": "<hex>",
          "msg_hash": "<sha256 hex do payload>",
          "sig": "<hex assinatura>"
        }

    Assinatura cobre: DOMAIN || pub || ts || nonce || msg_hash
    """
    if _node_id_priv is None:
        return {}

    try:
        ts = int(time.time())
        nonce = os.urandom(16).hex()
        msg_hash = _msg_hash(msg)

        # Mensagem assinada (canônica, sem ambiguidade)
        parts = [
            _DOMAIN,
            _node_id_pub_hex.encode(),
            b"|",
            str(ts).encode(),
            b"|",
            nonce.encode(),
            b"|",
            msg_hash.encode(),
        ]
        to_sign = b"".join(parts)

        sig_hex = _sign(to_sign)

        return {
            "pub": _node_id_pub_hex,
            "ts": ts,
            "nonce": nonce,
            "msg_hash": msg_hash,
            "sig": sig_hex,
        }
    except Exception as e:
        log.warning("build_auth falhou: " + str(e))
        return {}


# ============================================================
# VERIFY AUTH
# ============================================================
def verify_auth(auth: dict, msg=None) -> tuple[bool, str]:
    """
    Verifica um bloco _auth.

    Checa em ordem:
      1. Estrutura básica
      2. Timestamp dentro da janela
      3. Anti-replay (nonce)
      4. Binding ao payload (msg_hash)
      5. Assinatura Ed25519

    Retorna:
        (True, "")          → OK
        (False, "motivo")   → falha
    """
    if not auth:
        return False, "auth vazio"

    if not isinstance(auth, dict):
        return False, "auth não é dict"

    pub = auth.get("pub", "")
    ts = auth.get("ts", 0)
    nonce = auth.get("nonce", "")
    msg_hash = auth.get("msg_hash", "")
    sig = auth.get("sig", "")

    # 1) Estrutura
    if not pub or not sig or not ts:
        return False, "auth incompleto"
    if not isinstance(ts, int):
        try:
            ts = int(ts)
        except Exception:
            return False, "ts invalido"

    # 2) Janela de tempo
    now = int(time.time())
    delta = abs(now - ts)
    if delta > _AUTH_WINDOW:
        return False, "ts fora da janela (delta=" + str(delta) + "s)"

    # 3) Anti-replay
    if not _nonce_check(nonce, ts):
        return False, "replay detectado (nonce repetido)"

    # 4) Binding ao payload
    if msg is not None:
        expected_hash = _msg_hash(msg)
        if not msg_hash:
            return False, "msg_hash ausente no auth"
        if not hmac.compare_digest(msg_hash, expected_hash):
            return False, "msg_hash não corresponde ao payload"

    # 5) Assinatura
    parts = [
        _DOMAIN,
        pub.encode(),
        b"|",
        str(ts).encode(),
        b"|",
        nonce.encode(),
        b"|",
        msg_hash.encode(),
    ]
    to_verify = b"".join(parts)

    if not _verify_sig(pub, sig, to_verify):
        return False, "assinatura inválida"

    return True, ""


# ============================================================
# DEBUG / STATUS
# ============================================================
def status() -> dict:
    """Info útil para diagnóstico."""
    with _nonce_lock:
        n_nonces = len(_nonce_cache)
    return {
        "mode": _AUTH_MODE,
        "enabled": auth_enabled(),
        "required": auth_required(),
        "window_s": _AUTH_WINDOW,
        "nonce_cache_size": n_nonces,
        "nonce_cache_max": _NONCE_CACHE_SIZE,
        "has_priv": _node_id_priv is not None,
        "node_pub": _node_id_pub_hex[:16] + "..." if _node_id_pub_hex else "",
    }


def reset_nonce_cache() -> None:
    """Limpa o cache de nonces (útil para testes)."""
    with _nonce_lock:
        _nonce_cache.clear()
