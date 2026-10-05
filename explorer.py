"""
explorer.py — Explorador de blocos BRN (Flask) | v2
============================================================
v2: usa apenas metodos que existem no ChainDB atual.
    Corrige get_stats, latest_blocks, balance, utxos_for, ultimos_eventos
    para usar height/get_block/get_utxos/all_mempool.
"""

import os
import time
import threading
from collections import defaultdict
from flask import Flask, jsonify, send_from_directory, request, abort

from db import ChainDB

app = Flask(__name__, static_folder=".")
DB_PATH = os.environ.get("BRN_DB", "brn_v2_chain.db")
PORT = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
CORS_ORIGIN = os.environ.get("BRN_CORS_ORIGIN", "*")

RATE_LIMIT = 60
_rate_window = defaultdict(list)
_rate_lock = threading.Lock()

# Conexao singleton (evita abrir/fechar a cada request)
_db_singleton = None
_db_lock = threading.Lock()


def _db() -> ChainDB:
    global _db_singleton
    with _db_lock:
        if _db_singleton is None:
            _db_singleton = ChainDB(DB_PATH)
        return _db_singleton


def _rate_limit_ok(ip: str) -> bool:
    agora = time.time()
    with _rate_lock:
        janela = _rate_window[ip]
        janela[:] = [t for t in janela if agora - t < 60]
        if len(janela) >= RATE_LIMIT:
            return False
        janela.append(agora)
        return True


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


@app.before_request
def check_rate_limit():
    if request.method == "OPTIONS":
        return ("", 200)
    ip = request.remote_addr or "0.0.0.0"
    if not _rate_limit_ok(ip):
        abort(429)


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(429)
def too_many(e):
    return jsonify({"error": "rate limit exceeded"}), 429


# ==================== HELPERS ====================
def _get_stats(db):
    """Substitui db.get_stats() que provavelmente nao existe."""
    try:
        h = db.height()
        tip = db.tip_hash() if h >= 0 else ""
        utxos = db.count_utxos() if hasattr(db, "count_utxos") else 0
        mempool = len(db.all_mempool(limit=10000))
        peers_ativo = db.contar_peers(apenas_ativos=True)
        peers_total = db.contar_peers(apenas_ativos=False)
    except Exception:
        h = -1
        tip = ""
        utxos = 0
        mempool = 0
        peers_ativo = 0
        peers_total = 0

    # Difficuldade
    try:
        diff = 0
        if h >= 0:
            from blockchain import Blockchain
            diff = Blockchain(DB_PATH, auto_genesis=False).current_difficulty()
    except Exception:
        diff = 0

    return {
        "height": h,
        "tip": tip,
        "tip_short": tip[:16] + "..." if tip else "",
        "utxos": utxos,
        "mempool": mempool,
        "peers": peers_ativo,
        "peers_total": peers_total,
        "difficulty": diff,
    }


def _latest_blocks(db, n=10):
    """Substitui db.latest_blocks(n=10)."""
    try:
        top = db.height()
    except Exception:
        return []
    out = []
    for h in range(max(0, top - n + 1), top + 1):
        b = db.get_block(h)
        if b:
            out.append(b)
    out.reverse()
    return out


def _balance(db, addr):
    """Substitui db.balance(address)."""
    try:
        utxos = db.get_utxos(addr)
        return sum(u["amount"] for u in utxos)
    except Exception:
        return 0


def _utxos_for(db, addr):
    """Substitui db.utxos_for(address)."""
    try:
        return db.get_utxos(addr)
    except Exception:
        return []


# ==================== ROTAS ====================
@app.route("/")
def index():
    try:
        return send_from_directory(".", "index.html")
    except Exception:
        return jsonify({"ok": True, "msg": "BRN Explorer API"})


@app.route("/api/status")
def status():
    return jsonify(_get_stats(_db()))


@app.route("/api/stats")
def stats():
    return jsonify(_get_stats(_db()))


@app.route("/api/block/<int:h>")
def block(h):
    b = _db().get_block(h)
    return jsonify(b) if b else (jsonify({"error": "not found"}), 404)


@app.route("/api/latest")
def latest():
    return jsonify(_latest_blocks(_db(), n=10))


@app.route("/api/blocks")
def blocks_paginado():
    try:
        start = int(request.args.get("start", 0))
        limit = min(int(request.args.get("limit", 20)), 100)
    except ValueError:
        return jsonify({"error": "parametros invalidos"}), 400

    db = _db()
    try:
        top = db.height()
        out = []
        for h in range(start, min(start + limit, top + 1)):
            b = db.get_block(h)
            if b:
                out.append({
                    "height": b["height"],
                    "hash": b["hash"],
                    "timestamp": b["timestamp"],
                    "txs": len(b["transactions"]),
                    "difficulty": b["difficulty"],
                })
        return jsonify({"start": start, "count": len(out), "blocks": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/balance/<address>")
def balance(address):
    db = _db()
    return jsonify({
        "address": address,
        "balance": _balance(db, address),
        "utxos": _utxos_for(db, address),
    })


@app.route("/api/mempool")
def mempool():
    return jsonify(_db().all_mempool(limit=200))


@app.route("/api/peers")
def peers():
    db = _db()
    try:
        return jsonify({
            "count": db.contar_peers(apenas_ativos=False),
            "active": db.contar_peers(apenas_ativos=True),
            "peers": db.listar_peers(apenas_ativos=False),
        })
    except Exception as e:
        return jsonify({"error": str(e), "count": 0, "active": 0, "peers": []})


@app.route("/api/events")
def events():
    """
    v2: 'eventos' viraram as ultimas N txs mineradas.
    Se db.ultimos_eventos() existir, usa. Senao, faz fallback.
    """
    try:
        n = min(int(request.args.get("n", 50)), 200)
    except ValueError:
        n = 50

    db = _db()

    if hasattr(db, "ultimos_eventos"):
        try:
            return jsonify(db.ultimos_eventos(n))
        except Exception:
            pass

    # Fallback: coleta txs dos ultimos blocos
    try:
        top = db.height()
        evs = []
        for h in range(max(0, top - 10), top + 1):
            b = db.get_block(h)
            if not b:
                continue
            for tx in b.get("transactions", []):
                evs.append({
                    "type": "tx",
                    "block_height": h,
                    "txid": tx.get("txid"),
                    "timestamp": tx.get("timestamp", 0),
                    "n_inputs": len(tx.get("inputs", [])),
                    "n_outputs": len(tx.get("outputs", [])),
                })
        evs.sort(key=lambda x: x["timestamp"], reverse=True)
        return jsonify(evs[:n])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/verify")
def verify():
    """Verifica integridade. Passa network_secret para auth_tag funcionar."""
    try:
        from blockchain import Blockchain
        from chain_validator import verify_chain_dict

        secret = os.environ.get("BRN_NETWORK_SECRET", "").strip()
        bc = Blockchain(
            db_path=DB_PATH,
            auto_genesis=False,
            network_secret=secret,
        )
        try:
            return jsonify(verify_chain_dict(bc))
        finally:
            try:
                bc.db.close()
            except Exception:
                pass
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print(f"🔍 Explorer BRN rodando em http://0.0.0.0:{PORT}")
    print(f"   DB: {DB_PATH}")
    print(f"   CORS: {CORS_ORIGIN}")
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
