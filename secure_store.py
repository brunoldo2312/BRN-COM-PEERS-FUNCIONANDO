"""
secure_store.py — Armazenamento local criptografado do BRN (v8)
================================================================
Primitivas criptográficas:
  * KDF: Argon2id (fallback PBKDF2-SHA256 com 1M iterações)
  * Cifra: ChaCha20-Poly1305 (AEAD)
  * Formato: MAGIC(4) | VER(1) | SALT(16) | NONCE(12) | CIPHERTEXT+TAG

v8 (ADIÇÕES):
  * WalletSession       — carteira em RAM com auto-lock (15 min)
  * save_current_wallet — grava current_wallet.enc (substitui .json texto puro)
  * load_current_wallet — carrega carteira ativa cifrada
  * migrate_current_wallet — migra current_wallet.json legado
  * constant_time_eq    — comparação resistente a timing attacks
  * _atomic_write_bytes — escrita atômica com permissões 0600
================================================================
"""
import os
import json
import time
import hmac
import secrets
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


# ============================================================
# CONSTANTES
# ============================================================
MAGIC   = b"BRNS"
VERSION = 1

SALT_LEN  = 16
NONCE_LEN = 12

# KDF
ARGON2_TIME_COST   = 3
ARGON2_MEMORY_COST = 64 * 1024   # 64 MB
ARGON2_PARALLELISM = 4
PBKDF2_ITERATIONS  = 1_000_000

# current_wallet
CURRENT_WALLET_FILE = "current_wallet.enc"
LEGACY_CURRENT_FILE = "current_wallet.json"


# ============================================================
# KDF
# ============================================================
def _argon2_available() -> bool:
    try:
        import argon2  # noqa: F401
        return True
    except ImportError:
        return False


def _derive_key(password: str, salt: bytes) -> bytes:
    """Deriva chave de 32 bytes. Prefere Argon2id; fallback PBKDF2-SHA256."""
    if _argon2_available():
        from argon2.low_level import hash_secret_raw, Type
        return hash_secret_raw(
            secret=password.encode("utf-8"),
            salt=salt,
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST,
            parallelism=ARGON2_PARALLELISM,
            hash_len=32,
            type=Type.ID,
        )
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32,
        salt=salt, iterations=PBKDF2_ITERATIONS,
    ).derive(password.encode("utf-8"))


# ============================================================
# AEAD (ChaCha20-Poly1305)
# ============================================================
def encrypt_blob(plaintext: bytes, password: str, aad: bytes = b"") -> bytes:
    """
    Formato do blob:
        MAGIC(4) | VERSION(1) | SALT(16) | NONCE(12) | CIPHERTEXT+TAG
    """
    salt  = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key   = _derive_key(password, salt)
    ct    = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return MAGIC + bytes([VERSION]) + salt + nonce + ct


def decrypt_blob(blob: bytes, password: str, aad: bytes = b"") -> bytes:
    min_size = 4 + 1 + SALT_LEN + NONCE_LEN + 16
    if len(blob) < min_size:
        raise ValueError("blob muito curto")
    if blob[:4] != MAGIC:
        raise ValueError("magic invalido (arquivo nao e do BRN)")
    if blob[4] != VERSION:
        raise ValueError(f"versao desconhecida: {blob[4]}")

    off   = 5
    salt  = blob[off:off + SALT_LEN];   off += SALT_LEN
    nonce = blob[off:off + NONCE_LEN];  off += NONCE_LEN
    ct    = blob[off:]

    key = _derive_key(password, salt)
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)
    except Exception:
        raise ValueError("senha incorreta ou arquivo corrompido")


# ============================================================
# HELPERS
# ============================================================
def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Escrita atômica + permissões 0600."""
    tmp = str(path) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise
    os.replace(tmp, path)
    try: os.chmod(path, 0o600)
    except Exception: pass


def constant_time_eq(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


# ============================================================
# WALLET SESSION (RAM + auto-lock)
# ============================================================
class WalletSession:
    """
    Mantém a carteira descriptografada apenas em RAM.
    Auto-lock após TTL de inatividade.
    """

    def __init__(self, ttl: int = 15 * 60):
        self.ttl   = ttl
        self._data = None
        self._last = 0.0
        self._lock = threading.RLock()

    def unlock(self, wallet_dict: dict) -> None:
        with self._lock:
            self._data = dict(wallet_dict)
            self._last = time.time()

    def touch(self) -> None:
        with self._lock:
            if self._data:
                self._last = time.time()

    def lock(self) -> None:
        with self._lock:
            if self._data:
                self._data.clear()
            self._data = None
            self._last = 0.0

    def is_locked(self) -> bool:
        with self._lock:
            if not self._data:
                return True
            if (time.time() - self._last) > self.ttl:
                self.lock()
                return True
            return False

    def get(self):
        if self.is_locked():
            return None
        self.touch()
        return dict(self._data) if self._data else None

    def seconds_until_lock(self) -> int:
        with self._lock:
            if not self._data:
                return 0
            return max(0, int(self.ttl - (time.time() - self._last)))


# ============================================================
# CURRENT WALLET (cifrado)
# ============================================================
def save_current_wallet(password: str, wallet_dict: dict,
                        path: str = CURRENT_WALLET_FILE) -> dict:
    """Grava a carteira ativa CIFRADA. Remove o legado .json se existir."""
    try:
        payload = {
            "address":     wallet_dict.get("address", ""),
            "private_key": wallet_dict.get("private_key", ""),
            "public_key":  wallet_dict.get("public_key") or wallet_dict.get("pubkey", ""),
            "saved_at":    int(time.time()),
        }
        data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        blob = encrypt_blob(data, password)
        _atomic_write_bytes(path, blob)

        # Renomeia o legado para .bak (não apaga de cara)
        legacy = Path(LEGACY_CURRENT_FILE)
        if legacy.exists() and legacy.resolve() != Path(path).resolve():
            try:
                legacy.rename(legacy.with_suffix(".json.bak"))
            except Exception:
                pass
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def load_current_wallet(password: str,
                        path: str = CURRENT_WALLET_FILE) -> dict:
    """Carrega a carteira ativa cifrada."""
    try:
        p = Path(path)
        if not p.exists():
            if Path(LEGACY_CURRENT_FILE).exists():
                return {
                    "ok": False,
                    "msg": "Arquivo legado (texto puro) detectado. "
                           "Use migrate_current_wallet().",
                }
            return {"ok": False, "msg": "Nenhuma carteira ativa"}
        blob = p.read_bytes()
        data = json.loads(decrypt_blob(blob, password).decode())
        return {"ok": True, **data}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def migrate_current_wallet(password: str) -> dict:
    """
    Migra current_wallet.json (texto puro) → current_wallet.enc.
    Deve ser chamado UMA VEZ após atualizar o código.
    """
    legacy = Path(LEGACY_CURRENT_FILE)
    if not legacy.exists():
        return {"ok": False, "msg": "Nada para migrar"}
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "msg": f"Falha ao ler legado: {e}"}

    r = save_current_wallet(password, data)
    if not r.get("ok"):
        return r
    try:
        legacy.rename(legacy.with_suffix(".json.bak"))
    except Exception:
        pass
    return {"ok": True, "msg": "Migrado com sucesso", "path": r["path"]}
