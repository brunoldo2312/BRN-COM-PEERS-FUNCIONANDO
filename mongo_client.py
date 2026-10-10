"""
mongo_client.py — Cliente MongoDB compartilhado (explorer + nó)
================================================================
Substitui o script original (que tinha senha hardcoded).
Usa env vars + URL-encoding automático + lazy connect.
"""
from __future__ import annotations

import os
import time
import logging
import threading
from typing import Optional
from urllib.parse import quote_plus

log = logging.getLogger("mongo")

try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
    _HAS_PYMONGO = True
except ImportError:
    _HAS_PYMONGO = False
    MongoClient = None
    PyMongoError = Exception


class Mongo:
    """Cliente lazy. Falha não derruba o processo."""

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()
        self._last_error: Optional[str] = None
        self._last_ping: float = 0.0
        self.uri: Optional[str] = None
        self.db_name: str = os.environ.get("MONGO_DB", "brn_analytics")

        if not _HAS_PYMONGO:
            log.warning("pymongo não instalado — Mongo desabilitado")
            return

        self.uri = self._build_uri()
        if not self.uri:
            log.warning("Mongo não configurado (defina MONGO_URI ou MONGO_USER/PASS/HOST)")

    @staticmethod
    def _build_uri() -> Optional[str]:
        uri = os.environ.get("MONGO_URI", "").strip()
        if uri:
            return uri
        user = os.environ.get("MONGO_USER", "").strip()
        pwd  = os.environ.get("MONGO_PASS", "").strip()
        host = os.environ.get("MONGO_HOST", "").strip()
        if not (user and pwd and host):
            return None
        return (f"mongodb+srv://{quote_plus(user)}:{quote_plus(pwd)}"
                f"@{host}/?retryWrites=true&w=majority")

    def client(self):
        if not _HAS_PYMONGO or not self.uri:
            return None
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                try:
                    self._client = MongoClient(
                        self.uri,
                        serverSelectionTimeoutMS=3000,
                        connectTimeoutMS=3000,
                        socketTimeoutMS=5000,
                        maxPoolSize=20,
                        appname="brn-node",
                    )
                    log.info("Mongo client criado (db=%s)", self.db_name)
                except Exception as e:
                    self._last_error = str(e)
                    log.error("Falha criando client Mongo: %s", e)
                    return None
        return self._client

    def ping(self) -> tuple[bool, Optional[str]]:
        if not _HAS_PYMONGO:
            return False, "pymongo não instalado"
        if not self.uri:
            return False, "não configurado"
        c = self.client()
        if c is None:
            return False, self._last_error or "client indisponível"
        try:
            c.admin.command("ping")
            self._last_ping = time.time()
            self._last_error = None
            return True, None
        except PyMongoError as e:
            self._last_error = str(e)
            return False, str(e)

    def db(self):
        c = self.client()
        return c[self.db_name] if c else None

    def close(self) -> None:
        if self._client:
            try: self._client.close()
            except Exception: pass
            self._client = None


# Singleton
mongo = Mongo()