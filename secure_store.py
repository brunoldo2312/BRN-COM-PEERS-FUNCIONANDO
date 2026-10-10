# secure_store.py — BRN Chain v8.2
# Formato: BRNS | ver | salt(16) | nonce(12) | ciphertext || tag(16)
from __future__ import annotations

import os
import json
import logging
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.exceptions import InvalidTag

try:
    from argon2.low_level import hash_secret_raw, Type
    _ARGON2_IMPL = "cffi"
except ImportError:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    _ARGON2_IMPL = "cryptography"

log = logging.getLogger("secure_store")

SALT_SIZE  = 16
KEY_SIZE   = 32
NONCE_SIZE = 12
TAG_SIZE   = 16
MAGIC      = b"BRNS"
FORMAT_VERSION = 1

ARGON2_TIME_COST   = int(os.environ.get("BRN_ARGON2_TIME", "3"))
ARGON2_MEMORY_COST = int(os.environ.get("BRN_ARGON2_MEMORY", "65536"))
ARGON2_PARALLELISM = int(os.environ.get("BRN_ARGON2_PARALLEL", "4"))
MIN_PASSWORD_LEN = 8


def _derive_key(password: str, salt: bytes) -> bytes:
    if _ARGON2_IMPL == "cffi":
        return hash_secret_raw(
            secret=password.encode("utf-8"), salt=salt,
            time_cost=ARGON2_TIME_COST, memory_cost=ARGON2_MEMORY_COST,
            parallelism=ARGON2_PARALLELISM, hash_len=KEY_SIZE, type=Type.ID,
        )
    kdf = Argon2id(salt=salt, length=KEY_SIZE,
                   iterations=ARGON2_TIME_COST,
                   lanes=ARGON2_PARALLELISM,
                   memory_cost=ARGON2_MEMORY_COST)
    return kdf.derive(password.encode("utf-8"))


def _validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("senha deve ser string")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"senha muito curta (mínimo {MIN_PASSWORD_LEN} caracteres)")


def _encrypt(data: bytes, key: bytes, aad: bytes = b"") -> bytes:
    nonce = os.urandom(NONCE_SIZE)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, data, aad)


def _decrypt(packed: bytes, key: bytes, aad: bytes = b"") -> bytes:
    if len(packed) < NONCE_SIZE + TAG_SIZE:
        raise ValueError("blob cifrado muito curto")
    nonce, ct = packed[:NONCE_SIZE], packed[NONCE_SIZE:]
    return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)


def _atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    p = Path(path)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(str(tmp), path)
        try:
            dfd = os.open(str(p.parent) or ".", os.O_DIRECTORY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except OSError:
            pass
    except Exception:
        try: tmp.unlink()
        except FileNotFoundError: pass
        raise


def _save_blob(path: