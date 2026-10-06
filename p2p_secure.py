"""
p2p_secure.py — Canal seguro P2P do BRN
============================================================
Fornece:
  * NodeIdentity      — chave Ed25519 persistente do nó (cifrada em disco)
  * SecureChannel     — canal TCP autenticado com AES-256-GCM
  * SecureClient      — cliente one-shot (handshake + 1 request/resposta)
  * accept_secure()   — helper para o servidor aceitar conexões seguras

Esquema criptográfico:
  - Handshake autenticado por Ed25519 (identidade do nó)
  - Troca efêmera X25519 (ECDH) => forward secrecy
  - Derivação de chaves HKDF-SHA256 (2 chaves: C2S e S2C)
  - Cifra simétrica AES-256-GCM com contador de sequência (anti-replay)

Formato do HELLO/ACK (em claro, 165 bytes):
  MAGIC(4) | VER(1) | EPH_PUB(32) | NODE_ID(32) | ED_PUB(32) | SIG(64)

Formato dos frames de dados (cifrados):
  MAGIC(4) | VER(1) | SEQ(8) | LEN(4) | CIPHERTEXT+TAG(LEN bytes)
"""
from __future__ import annotations

import os
import json
import time
import base64
import struct
import socket
import hashlib
import threading
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.fernet import Fernet


# ============================================================
# CONSTANTES
# ============================================================
MAGIC_HANDSHAKE = b"BRNH"
MAGIC_DATA      = b"BRND"
PROTO_VERSION   = 1

HANDSHAKE_DOMAIN = b"BRN-P2P-HS-v1|"
HKDF_INFO_C2S    = b"BRN-P2P-C2S-v1"
HKDF_INFO_S2C    = b"BRN-P2P-S2C-v1"

MAX_FRAME         = 8 * 1024 * 1024
HANDSHAKE_TIMEOUT = 10.0
RECV_TIMEOUT      = 15.0
NONCE_LEN         = 12
TAG_LEN           = 16

DEFAULT_IDENTITY_FILE = "node_identity.enc"
DEFAULT_PASSWORD_ENV  = "BRN_NODE_PASSWORD"

# Header de cada frame de dados: magic(4) + ver(1) + seq(8) + len(4) = 17 bytes
_HEADER       = struct.Struct(">4sBQI")
# Header do handshake: magic(4) + ver(1) + eph(32) + node(32) + ed_pub(32) + sig(64)
_HS_HEADER    = struct.Struct(">4sB")
_HS_BODY_SIZE = 32 + 32 + 32 + 64   # 160 bytes


class SecureChannelError(Exception):
    pass


# ============================================================
# HELPERS
# ============================================================
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SecureChannelError("conexao fechada")
        buf.extend(chunk)
    return bytes(buf)


def _raw_pub(priv: Ed25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _raw_pub_x(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _derive_keys(shared: bytes, salt: bytes):
    k_c2s = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                 info=HKDF_INFO_C2S).derive(shared)
    k_s2c = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                 info=HKDF_INFO_S2C).derive(shared)
    return k_c2s, k_s2c


# ============================================================
# IDENTIDADE DO NÓ (Ed25519 persistente, cifrada em disco)
# ============================================================
class NodeIdentity:
    """Chave Ed25519 do nó + node_id (32 bytes estáveis)."""

    def __init__(self, priv: Ed25519PrivateKey, node_id: bytes):
        if len(node_id) != 32:
            raise ValueError("node_id deve ter 32 bytes")
        self.priv = priv
        self.pub = _raw_pub(priv)
        self.node_id = node_id

    # -------- Serialização cifrada --------
    def _to_payload(self) -> bytes:
        return json.dumps({
            "priv": self.priv.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ).hex(),
            "node_id": self.node_id.hex(),
        }).encode()

    @staticmethod
    def _from_payload(payload: bytes) -> "NodeIdentity":
        d = json.loads(payload.decode())
        priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(d["priv"]))
        node_id = bytes.fromhex(d["node_id"])
        return NodeIdentity(priv, node_id)

    def save(self, path: str, password: str) -> bool:
        try:
            salt = os.urandom(16)
            kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                             salt=salt, iterations=600_000)
            key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
            token = Fernet(key).encrypt(self._to_payload())
            Path(path).write_bytes(base64.b64encode(salt + token))
            return True
        except Exception as e:
            print(f"[NodeIdentity] erro ao salvar: {e}")
            return False

    @classmethod
    def load(cls, path: str, password: str) -> "NodeIdentity | None":
        try:
            blob = base64.b64decode(Path(path).read_bytes())
            salt, token = blob[:16], blob[16:]
            kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                             salt=salt, iterations=600_000)
            key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
            payload = Fernet(key).decrypt(token)
            return cls._from_payload(payload)
        except Exception as e:
            print(f"[NodeIdentity] falha ao carregar: {e}")
            return None

    @classmethod
    def load_or_create(cls, path: str = DEFAULT_IDENTITY_FILE,
                       password_env: str = DEFAULT_PASSWORD_ENV,
                       ) -> "NodeIdentity":
        """
        Carrega do disco se existir e a senha bater; senão gera nova
        e salva. Se a senha não estiver setada, gera efêmera (não salva).
        """
        pw = os.environ.get(password_env, "").strip()
        if pw and Path(path).exists():
            id_ = cls.load(path, pw)
            if id_ is not None:
                return id_

        # Gera nova
        priv = Ed25519PrivateKey.generate()
        node_id = hashlib.sha256(
            b"BRN-NODE-ID-v1|" + _raw_pub(priv) +
            os.urandom(16)
        ).digest()[:32]
        id_ = cls(priv, node_id)

        if pw:
            id_.save(path, pw)
            print(f"[NodeIdentity] nova identidade salva em {path}")
        else:
            print(f"[NodeIdentity] AVISO: {password_env} vazio — identidade efêmera")
        return id_

    def sign(self, data: bytes) -> bytes:
        return self.priv.sign(data)


# ============================================================
# CANAL SEGURO
# ============================================================
class SecureChannel:
    """Canal TCP com handshake autenticado + AES-256-GCM."""

    def __init__(self, sock: socket.socket, is_client: bool, identity: NodeIdentity):
        self.sock = sock
        self.is_client = is_client
        self.identity = identity
        self._eph = X25519PrivateKey.generate()
        self._send_key: bytes | None = None
        self._recv_key: bytes | None = None
        self._send_seq = 0
        self._recv_seq = -1
        self._established = False
        self.peer_node_id: bytes | None = None
        self.peer_pub: bytes | None = None

    # -------- Handshake --------
    def _build_hs(self) -> bytes:
        eph_pub = _raw_pub_x(self._eph)
        payload = HANDSHAKE_DOMAIN + bytes([PROTO_VERSION]) + eph_pub + self.identity.node_id
        sig = self.identity.sign(payload)
        return (_HS_HEADER.pack(MAGIC_HANDSHAKE, PROTO_VERSION) +
                eph_pub + self.identity.node_id + self.identity.pub + sig)

    def _parse_hs(self, head: bytes, body: bytes) -> tuple:
        magic, ver = _HS_HEADER.unpack(head)
        if magic != MAGIC_HANDSHAKE:
            raise SecureChannelError("magic invalido no handshake")
        if ver != PROTO_VERSION:
            raise SecureChannelError(f"versao incompativel: {ver}")
        eph  = body[0:32]
        nid  = body[32:64]
        ed   = body[64:96]
        sig  = body[96:160]
        payload = HANDSHAKE_DOMAIN + bytes([PROTO_VERSION]) + eph + nid
        try:
            Ed25519PublicKey.from_public_bytes(ed).verify(sig, payload)
        except Exception:
            raise SecureChannelError("assinatura Ed25519 invalida")
        return eph, nid, ed

    def handshake(self) -> bytes:
        self.sock.settimeout(HANDSHAKE_TIMEOUT)
        if self.is_client:
            self._hs_client()
        else:
            self._hs_server()
        self._established = True
        self.sock.settimeout(RECV_TIMEOUT)
        return self.peer_node_id

    def _hs_client(self):
        self.sock.sendall(self._build_hs())
        head = _recv_exact(self.sock, _HS_HEADER.size)
        body = _recv_exact(self.sock, _HS_BODY_SIZE)
        s_eph, s_nid, s_ed = self._parse_hs(head, body)
        shared = self._eph.exchange(X25519PublicKey.from_public_bytes(s_eph))
        salt   = _raw_pub_x(self._eph) + s_eph
        self._send_key, self._recv_key = _derive_keys(shared, salt)
        self.peer_node_id = s_nid
        self.peer_pub = s_ed

    def _hs_server(self):
        head = _recv_exact(self.sock, _HS_HEADER.size)
        body = _recv_exact(self.sock, _HS_BODY_SIZE)
        c_eph, c_nid, c_ed = self._parse_hs(head, body)
        self.sock.sendall(self._build_hs())
        shared = self._eph.exchange(X25519PublicKey.from_public_bytes(c_eph))
        salt   = c_eph + _raw_pub_x(self._eph)
        k_c2s, k_s2c = _derive_keys(shared, salt)
        # Server recebe C2S, envia S2C
        self._send_key, self._recv_key = k_s2c, k_c2s
        self.peer_node_id = c_nid
        self.peer_pub = c_ed

    # -------- Frames --------
    def send(self, payload: bytes):
        if not self._established:
            raise SecureChannelError("handshake nao feito")
        if len(payload) > MAX_FRAME:
            raise SecureChannelError("frame muito grande")
        seq = self._send_seq
        self._send_seq += 1
        nonce = seq.to_bytes(NONCE_LEN, "big")
        aad = MAGIC_DATA + bytes([PROTO_VERSION]) + struct.pack(">Q", seq)
        ct = AESGCM(self._send_key).encrypt(nonce, payload, aad)
        header = _HEADER.pack(MAGIC_DATA, PROTO_VERSION, seq, len(ct))
        self.sock.sendall(header + ct)

    def recv(self) -> bytes:
        if not self._established:
            raise SecureChannelError("handshake nao feito")
        head = _recv_exact(self.sock, _HEADER.size)
        magic, ver, seq, clen = _HEADER.unpack(head)
        if magic != MAGIC_DATA:
            raise SecureChannelError("magic invalido no frame")
        if ver != PROTO_VERSION:
            raise SecureChannelError("versao incompativel no frame")
        if seq <= self._recv_seq:
            raise SecureChannelError(f"replay/out-of-order seq={seq}")
        if clen > MAX_FRAME + TAG_LEN:
            raise SecureChannelError("frame grande demais")
        ct = _recv_exact(self.sock, clen)
        nonce = seq.to_bytes(NONCE_LEN, "big")
        aad = MAGIC_DATA + bytes([PROTO_VERSION]) + struct.pack(">Q", seq)
        try:
            pt = AESGCM(self._recv_key).decrypt(nonce, ct, aad)
        except Exception:
            raise SecureChannelError("falha ao decifrar (tag invalida)")
        self._recv_seq = seq
        return pt

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ============================================================
# CLIENTE ONE-SHOT
# ============================================================
class SecureClient:
    """Connect → handshake → 1 request → 1 response → close."""

    @staticmethod
    def request(host: str, port: int, payload: bytes,
                identity: NodeIdentity,
                timeout: float = 5.0,
                expected_pub: bytes | None = None) -> bytes | None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            ch = SecureChannel(s, is_client=True, identity=identity)
            ch.handshake()
            if expected_pub and ch.peer_pub != expected_pub:
                ch.close()
                return None
            ch.send(payload)
            resp = ch.recv()
            ch.close()
            return resp
        except Exception:
            return None


# ============================================================
# SERVIDOR — helper para o accept
# ============================================================
def accept_secure(conn: socket.socket, identity: NodeIdentity,
                  expected_pub: bytes | None = None) -> SecureChannel | None:
    """
    Envolve uma conexão já aceita em um SecureChannel autenticado.
    Retorna None se o handshake falhar.
    """
    try:
        ch = SecureChannel(conn, is_client=False, identity=identity)
        ch.handshake()
        if expected_pub and ch.peer_pub != expected_pub:
            ch.close()
            return None
        return ch
    except Exception as e:
        try: conn.close()
        except Exception: pass
        return None
