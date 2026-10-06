# secure_store.py — BRN Chain v8.1
# Criptografia: Argon2id + ChaCha20-Poly1305
# Propósito: Armazenamento seguro de carteira e identidade de nó

import os
import json
import base64
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.exceptions import InvalidTag


# ⚙️ Configurações
SALT_SIZE = 16
KEY_SIZE = 32
NONCE_SIZE = 12

ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # 64 MiB
ARGON2_PARALLELISM = 4


def _derive_key(password: str, salt: bytes) -> bytes:
    """Deriva chave simétrica a partir da senha + salt usando Argon2id"""
    kdf = Argon2id(
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        length=KEY_SIZE,
    )
    return kdf.derive(password.encode("utf-8"))


def _encrypt(data: bytes, key: bytes) -> bytes:
    """Cifra com ChaCha20-Poly1305 (inclui nonce + tag)"""
    chacha = ChaCha20Poly1305(key)
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = chacha.encrypt(nonce, data, None)
    return nonce + ciphertext


def _decrypt(packed_data: bytes, key: bytes) -> bytes:
    """Decifra dados previamente cifrados"""
    nonce = packed_data[:NONCE_SIZE]
    ciphertext = packed_data[NONCE_SIZE:]
    chacha = ChaCha20Poly1305(key)
    return chacha.decrypt(nonce, ciphertext, None)


# ==========================================
# ✅ FUNÇÃO CORRIGIDA — save_wallet
# ==========================================
def save_wallet(wallet_data: dict, password: str, path: str = "wallet_encrypted.dat") -> bool:
    """
    Cifra e persiste dados da carteira.
    :param wallet_data: dicionário com mnemônico, chaves, endereços
    :param password: senha do usuário
    :param path: caminho do arquivo
    """
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(password, salt)

    data_bytes = json.dumps(wallet_data, ensure_ascii=False).encode("utf-8")
    encrypted = _encrypt(data_bytes, key)

    with open(path, "wb") as f:
        f.write(salt + encrypted)

    os.chmod(path, 0o600)  # Leitura/Escrita APENAS para o dono
    return True


def load_wallet(password: str, path: str = "wallet_encrypted.dat") -> dict | None:
    """Carrega e decifra a carteira salva"""
    if not os.path.exists(path):
        return None

    with open(path, "rb") as f:
        salt = f.read(SALT_SIZE)
        encrypted = f.read()

    key = _derive_key(password, salt)

    try:
        decrypted = _decrypt(encrypted, key)
        return json.loads(decrypted.decode("utf-8"))
    except InvalidTag:
        raise ValueError("Senha incorreta ou arquivo corrompido")


# ==========================================
# Identidade do Nó (node_identity.enc)
# ==========================================
def save_node_identity(identity_data: dict, password: str, path: str = "node_identity.enc") -> bool:
    """Salva identidade Ed25519 do nó"""
    salt = os.urandom(SALT_SIZE)
    key = _derive_key(password, salt)

    data_bytes = json.dumps(identity_data).encode("utf-8")
    encrypted = _encrypt(data_bytes, key)

    with open(path, "wb") as f:
        f.write(salt + encrypted)

    os.chmod(path, 0o600)
    return True


def load_node_identity(password: str, path: str = "node_identity.enc") -> dict | None:
    """Carrega identidade do nó"""
    if not os.path.exists(path):
        return None

    with open(path, "rb") as f:
        salt = f.read(SALT_SIZE)
        encrypted = f.read()

    key = _derive_key(password, salt)

    try:
        decrypted = _decrypt(encrypted, key)
        return json.loads(decrypted.decode("utf-8"))
    except InvalidTag:
        raise ValueError("Senha incorreta ou identidade corrompida")


def delete_identity(path: str = "node_identity.enc") -> bool:
    """Apaga identidade (usado para reiniciar com senha nova)"""
    if os.path.exists(path):
        os.remove(path)
        return True
    return False
