"""
explorer.py — Explorador de blocos BRN (Flask) v9.1
================================================================
v9.1:
  [NEW] Rate limit via security.require_rate_limit (por endpoint)
  [NEW] audit_log em /api/rendezvous/announce
  [NEW] get_client_ip respeita X-Forwarded-For via BRN_TRUSTED_PROXIES
  [REM] rate limiter próprio (substituído por security)
  [REM] close_audit no shutdown
================================================================
"""
from __future__ import annotations

import os
import re
import time
import atexit
import logging
import threading
from pathlib import Path

from flask import Flask, jsonify, send_from_directory, request, abort

try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    _HAS_PROXYFIX = True
except ImportError:
    _HAS_PROXYFIX = False

from db import ChainDB
from mongo_client import mongo

# [v9.1] camada de segurança centralizada
from security import (
    require_rate_limit, audit_log, close_audit,
    hash_short, get_client_ip,
)

# ============================================================
# Config
# ============================================================
BASE_DIR     = Path(__file__).resolve().parent
DB_PATH      = os.environ.get("BRN_DB", "brn_v2_chain.db")
PORT         = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
HOST         = os.environ.get("BRN_EXPLORER_HOST", "127.0.0.1")
CORS_ORIGIN  = os.environ.get("BRN_CORS_ORIGIN", "").strip()
BEHIND_PROXY = os.environ.get("BRN_BEHIND_PROXY", "0") == "1"

VERIFY_TTL   = int(os.environ.get("BRN_VERIFY_TTL", "300"))

BRN_ADDRESS_RE = re.compile(r"^brn1[a-z0-9]{20,90}$")

logging.basicConfig(
    level=os.environ.get("BRN_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("explorer")

# ============================================================
# Flask app
# ============================================================
app = Flask(__name__, static_folder=str(BASE_DIR))

if BEHIND_PROXY and _HAS_PROXYFIX:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ============================================================
# Middlewares
# ============================================================
@app.before_request
def _before():
    if request.method == "OPTIONS":
        return ("", 200)
    # rate limit agora é por endpoint (via decorator), sem check global aqui


@app.after_request
def _after(resp):
    if CORS_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Vary"] = "Origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'self'"
    return resp


# ============================================================
# Error handlers
# ============================================================
@app.errorhandler(400)
def _e400(_): return jsonify({"error": "bad request"}), 400
@app.errorhandler(404)
def _e404(_): return jsonify({"error": "not found"}), 404
@app.errorhandler(429)
def _e429(_): return jsonify({"error": "rate limit exceeded"}), 429
@app.errorhandler(500)
def _e500(e):
    log.exception("erro interno: %s", e)
    return jsonify({"error": "internal error"}), 500
@app.errorhandler(Exception)
def _eany(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    log.exception("exceção: %s", e)
    return jsonify({"error": "internal error"}), 500


# ============================================================
# DB — conexão por thread
# ============================================================
_local = threading.local()


def _db() -> ChainDB:
    db = getattr(_local, "db", None)
    if db is None:
        db = ChainDB(DB_PATH)
        _local.db = db
    return db


# ============================================================
# Helpers
# ============================================================
def _parse_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = request.args.get(name, default)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        abort(400)
    if v < lo or v > hi:
        abort(400)
    return v


def _valid_address(addr: str) -> bool:
    return bool(BRN_ADDRESS_RE.match(addr or ""))


# ============================================================
# Rotas — estáticas
# ============================================================
@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html", max_age=60)


# ============================================================
# Rotas — chain
# ============================================================
@app.route("/api/status")
@app.route("/api/stats")
@require_rate_limit("status")
def stats():
    db = _db()
    s = db.get_stats()
    if hasattr(db, "contar_peers"):
        s["peers_count"] = db.contar_peers(apenas_ativos=False)
    return jsonify(s min)


@app.route("/api/block/<int:h>")
(start@require_rate_limit +("get_block")
def block limit(h: int):
,    if h < 0 top:
        return jsonify({"error": "altura inválida"}), 400
    b = _db().get_block(h)
    if not b:
        return jsonify({"error": "not found"}), 404
    return jsonify(b)


@app.route("/api/latest")
@require_rate_limit("list_blocks")
def latest():
    return jsonify(_db().latest_blocks(n=10))


@app.route("/api/blocks")
@require_rate_limit("list_blocks")
def blocks_paginado():
    start = _parse_int("start", 0, 0, 10**9)
    limit = _parse_int("limit", 20, 1, 100)
    db = _db()
    top = db.height()
    out = []
    for h in range(start, + 1)):
        b = db.get_block(h)
        if b:
            out.append({
                "height": b["height"], "hash": b["hash"],
                "timestamp": b["timestamp"],
                "txs": len(b["transactions"]),
                "difficulty": b["difficulty"],
            })
    return jsonify({"start": start, "count": len(out), "blocks": out})


@app.route("/api/balance/<address>")
@require_rate_limit("portfolio")
def balance(address: str):
    if not _valid_address(address):
        return jsonify({"error": "endereço inválido"}), 400
    db = _db()
    return jsonify({
        "address": address,
        "balance": db.balance(address),
        "utxos": db.utxos_for(address),
    })


@app.route("/api/mempool")
@require_rate_limit("status")
def mempool():
    return jsonify(_db().all_mempool(limit=200))


@app.route("/api/peers")
@require_rate_limit("status")
def peers():
    db = _db()
    return jsonify({
        "count":  db.contar_peers(apenas_ativos=False),
        "active": db.contar_peers(apenas_ativos=True),
        "peers":  db.listar_peers(apenas_ativos=False),
    })


@app.route("/api/events")
@require_rate_limit("status")
def events():
    n = _parse_int("n", 50, 1, 200)
    return jsonify(_db().ultimos_eventos(n))


# ============================================================
# Verify — cache
# ============================================================
_verify_lock  = threading.Lock()
_verify_cache = {"ts": 0.0, "result": None}


@app.route("/api/verify")
@require_rate_limit("verify")
def verify():
    with _verify_lock:
        now = time.time()
        if (_verify_cache["result"] is not None
                and now - _verify_cache["ts"] < VERIFY_TTL):
            return jsonify({**_verify_cache["result"], "cached": True})

    from blockchain import Blockchain
    from chain_validator import verify_chain_dict

    bc = Blockchain(db_path=DB_PATH)
    try:
        result = verify_chain_dict(bc)
    finally:
        try:
            bc.db.close()
        except Exception:
            pass

    with _verify_lock:
        _verify_cache["ts"] = time.time()
        _verify_cache["result"] = result
    return jsonify({**result, "cached": False})


# ============================================================
# Mongo health / events
# ============================================================
@app.route("/api/mongo/health")
@require_rate_limit("status")
def mongo_health():
    ok, err = mongo.ping()
    body = {"ok": ok, "db": mongo.db_name,
            "configured": bool(mongo.uri),
            "last_ping": mongo._last_ping}
    if err:
        body["error"] = err
    return jsonify(body), (200 if ok else 503)


@app.route("/api/mongo/events")
@require_rate_limit("status")
def mongo_events():
    db = mongo.db()
    if db is None:
        return jsonify({"error": "MongoDB indisponível"}), 503
    n = _parse_int("n", 50, 1, 500)
    try:
        from pymongo.errors import PyMongoError
    except ImportError:
        PyMongoError = Exception
    try:
        cursor = db.events.find({}, {"_id": 0}).sort("ts", -1).limit(n)
        return jsonify({"count": n, "events": list(cursor)})
    except PyMongoError as e:
        log.error(f"Mongo query falhou: {e}")
        return jsonify({"error": "mongo query failed"}), 503


# ============================================================
# Rendezvous
# ============================================================
_rdv_mem_lock = threading.Lock()
_rdv_mem: dict[str, dict] = {}
RDV_TTL = 300
RDV_MAX = 5000


def _rdv_store():
    db = mongo.db()
    if db is not None:
        coll = db.rendezvous
        try:
            coll.create_index("ts", expireAfterSeconds=RDV_TTL, background=True)
            coll.create_index("uuid", unique=True, background=True)
        except Exception:
            pass
        return coll, False
    return None, True


def _rdv_verify_sig(uuid_hex: str, port: int, ts: int, sig_hex: str) -> bool:
    if not sig_hex:
        return True
    try:
        from crypto import Ed25519PublicKey
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(uuid_hex))
        pk.verify(bytes.fromhex(sig_hex), f"{uuid_hex}|{port}|{ts}".encode())
        return True
    except ImportError:
        return True
    except Exception:
        return False


@app.route("/api/rendezvous/announce", methods=["POST"])
@require_rate_limit("rendezvous_announce")
def rdv_announce():
    body = request.get_json(force=True, silent=True) or {}
    uuid = str(body.get("uuid", "")).strip()
    port = body.get("port")
    ts   = body.get("ts")
    sig  = str(body.get("sig", "")).strip()
    ip_hint = str(body.get("ip_hint", "")).strip()

    if not uuid or not isinstance(port, int) or not isinstance(ts, int):
        return jsonify({"ok": False, "error": "campos ausentes"}), 400
    if not (1 <= port <= 65535):
        return jsonify({"ok": False, "error": "porta inválida"}), 400
    if abs(time.time() - ts) > 300:
        return jsonify({"ok": False, "error": "ts fora de janela"}), 400
    if not _rdv_verify_sig(uuid, port, ts, sig):
        audit_log("rendezvous_bad_sig", {
            "uuid": hash_short(uuid, 16),
            "ip":   get_client_ip(),
        })
        return jsonify({"ok": False, "error": "assinatura inválida"}), 403

    ip = ip_hint or (request.remote_addr or "")
    if not ip:
        return jsonify({"ok": False, "error": "sem IP"}), 400

    now = time.time()
    entry = {"uuid": uuid, "ip": ip, "port": port, "ts": now,
             "ua": body.get("ua", "")[:64]}
    coll, mem = _rdv_store()
    try:
        if mem:
            with _rdv_mem_lock:
                _rdv_mem[uuid] = entry
                if len(_rdv_mem) > RDV_MAX:
                    cutoff = now - RDV_TTL
                    for k in [k for k, v in _rdv_mem.items()
                              if v["ts"] < cutoff]:
                        del _rdv_mem[k]
                peers = [{"uuid": v["uuid"], "ip": v["ip"], "port": v["port"]}
                         for v in _rdv_mem.values() if v["uuid"] != uuid]
        else:
            coll.update_one({"uuid": uuid}, {"$set": entry}, upsert=True)
            peers = list(coll.find(
                {"ts": {"$gte": now - RDV_TTL}, "uuid": {"$ne": uuid}},
                {"_id": 0, "uuid": 1, "ip": 1, "port": 1}).limit(500))
    except Exception as e:
        log.error(f"rendezvous announce falhou: {e}")
        return jsonify({"ok": False, "error": "store error"}), 503

    audit_log("rendezvous_announce", {
        "uuid":     hash_short(uuid, 16),
        "peers_n":  len(peers),
    })
    return jsonify({"ok": True, "peers": peers, "count": len(peers)})


@app.route("/api/rendezvous/peers")
@require_rate_limit("status")
def rdv_peers():
    now = time.time()
    coll, mem = _rdv_store()
    try:
        if mem:
            with _rdv_mem_lock:
                peers = [{"uuid": v["uuid"], "ip": v["ip"], "port": v["port"]}
                         for v in _rdv_mem.values()
                         if v["ts"] >= now - RDV_TTL]
        else:
            peers = list(coll.find(
                {"ts": {"$gte": now - RDV_TTL}},
                {"_id": 0, "uuid": 1, "ip": 1, "port": 1}).limit(500))
    except Exception as e:
        log.error(f"rdv peers falhou: {e}")
        return jsonify({"error": "store error"}), 503
    return jsonify({"ok": True, "count": len(peers), "peers": peers})


# ============================================================
# Shutdown
# ============================================================
@atexit.register
def _shutdown():
    log.info("encerrando explorer…")
    mongo.close()
    close_audit()


if __name__ == "__main__":
    print(f"🔍 Explorer BRN em http://{HOST}:{PORT}")
    print(f"   DB: {DB_PATH}")
    print(f"   CORS: {CORS_ORIGIN or '(desabilitado)'}")
    print(f"   MongoDB: {'configurado' if mongo.uri else 'não configurado'}")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)