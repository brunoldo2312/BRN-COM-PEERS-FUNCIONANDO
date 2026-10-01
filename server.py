"""
server.py — API HTTP da BRN Chain v8.1 L2 MINERADO

- Endpoints BRN originais: /api/status, /api/balance, /api/tx, /api/blocks
- Endpoints L2 novos: /api/l2/quote, /api/l2/create, /api/l2/order/<id>, /api/l2/stats, /api/l2/list
- Validação por mineração: L2 só libera após bloco minerado
"""

import os
import time
import json
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from db import ChainDB
from blockchain import Blockchain, txid, GENESIS_ADDRESS
import btc_config

# L2
try:
    from l2_manager import L2Manager
    L2_ENABLED = True
except ImportError:
    L2_ENABLED = False
    L2Manager = None

# Config
DB_PATH = os.environ.get("BRN_DB_PATH", "brn_v2_chain.db")
WEB_PORT = int(os.environ.get("BRN_WEB_PORT", "5000"))

app = Flask(__name__)
CORS(app)

# DB e Chain - lazy load pra não quebrar no import
db = None
chain = None
l2_mgr = None

def get_db():
    global db, chain, l2_mgr
    if db is None:
        db = ChainDB(DB_PATH)
        chain = Blockchain(DB_PATH)
        if L2_ENABLED:
            try:
                l2_mgr = L2Manager(db, chain)
            except Exception as e:
                print(f"[server] L2Manager erro: {e}")
                l2_mgr = None
    return db, chain, l2_mgr

# ============================================================
# ENDPOINTS BRN CORE
# ============================================================

@app.route('/api/status')
def api_status():
    db, chain, l2_mgr = get_db()
    try:
        tip = db.tip_hash()
        height = db.height()
        mempool = len(db.all_mempool(limit=10000))
        utxos = db.count_utxos()
        stats = l2_mgr.stats() if l2_mgr else {}
        return jsonify({
            "ok": True,
            "height": height,
            "tip": tip,
            "tip_short": tip[:16] + "..." if tip else "",
            "mempool": mempool,
            "utxos": utxos,
            "peers": 0, # p2p pega via /api/status completo do main.py
            "difficulty": chain.current_difficulty() if chain else 0,
            "l2": stats,
            "version": "8.1 L2 MINERADO",
            "btc_address": btc_config.BTC_RECEIVE_ADDRESS if L2_ENABLED else "",
            "rate": f"1 BTC = {btc_config.BRN_PER_BTC} BRN" if L2_ENABLED else ""
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/height')
def api_height():
    db, _, _ = get_db()
    return jsonify({"height": db.height()})

@app.route('/api/block/<int:height>')
def api_block(height):
    db, _, _ = get_db()
    block = db.get_block(height)
    if not block:
        return jsonify({"error": "block not found"}), 404
    return jsonify(block)

@app.route('/api/block/hash/<hash>')
def api_block_by_hash(hash):
    db, _, _ = get_db()
    block = db.get_block_by_hash(hash)
    if not block:
        return jsonify({"error": "block not found"}), 404
    return jsonify(block)

@app.route('/api/blocks/latest')
def api_blocks_latest():
    db, _, _ = get_db()
    limit = int(request.args.get('limit', 10))
    try:
        blocks = db.conn.execute("SELECT * FROM blocks ORDER BY height DESC LIMIT?", (limit,)).fetchall()
        result = []
        for r in blocks:
            result.append({
                "height": r["height"],
                "hash": r["hash"],
                "prev_hash": r["prev_hash"],
                "timestamp": r["timestamp"],
                "nonce": r["nonce"],
                "difficulty": r["difficulty"],
                "merkle": r["merkle"]
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/balance/<address>')
def api_balance(address):
    db, _, _ = get_db()
    try:
        bal = db.get_balance(address)
        utxos = db.get_utxos_by_address(address)
        return jsonify({
            "address": address,
            "balance_sats": bal,
            "balance_brn": bal / 1e8,
            "utxos": len(utxos)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/utxos/<address>')
def api_utxos(address):
    db, _, _ = get_db()
    try:
        utxos = db.get_utxos_by_address(address)
        return jsonify(utxos)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/mempool')
def api_mempool():
    db, _, _ = get_db()
    try:
        mps = db.all_mempool(limit=100)
        return jsonify([{
            "txid": t["txid"],
            "timestamp": t.get("timestamp", 0),
            "is_l2": bool(t.get("data", {}).get("l2")),
            "type": t.get("data", {}).get("type", "normal")
        } for t in mps])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/tx/<txid>')
def api_tx(txid):
    db, _, _ = get_db()
    try:
        row = db.conn.execute("SELECT * FROM transactions WHERE txid=?", (txid,)).fetchone()
        if not row:
            # tenta mempool
            mp = db.get_mempool_tx(txid)
            if mp:
                return jsonify({"in_mempool": True, "tx": mp})
            return jsonify({"error": "tx not found"}), 404
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/tx/send', methods=['POST'])
def api_send_tx():
    db, chain, _ = get_db()
    try:
        data = request.json
        tx = data.get('tx')
        if not tx:
            return jsonify({"error": "tx required"}), 400

        # Validação básica
        ok, msg = chain.validate_tx(tx, db.height() + 1, is_coinbase=False)
        if not ok:
            return jsonify({"error": f"tx invalid: {msg}"}), 400

        db.add_mempool(tx, fee=data.get('fee', 0))
        return jsonify({"ok": True, "txid": tx["txid"], "in_mempool": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# ENDPOINTS L2 BTC -> BRN - VALIDADOS POR MINERAÇÃO
# ============================================================

@app.route('/api/l2/quote', methods=['GET'])
def l2_quote():
    """GET /api/l2/quote?btc_sats=100000 -> cotação sem criar ordem"""
    _, _, l2_mgr = get_db()
    if not L2_ENABLED or not l2_mgr:
        return jsonify({"error": "L2 desabilitado - configure btc_config.py"}), 400
    try:
        sats = int(request.args.get('btc_sats', 100000))
        q = l2_mgr.quote(sats)
        q["btc_address"] = btc_config.BTC_RECEIVE_ADDRESS
        q["network"] = btc_config.BTC_NETWORK
        q["min_confirmations"] = btc_config.BTC_MIN_CONFIRMATIONS
        q["flow"] = "BTC RPC -> mempool -> mineração PoW -> BRN liberado"
        return jsonify(q)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/l2/create', methods=['POST'])
def l2_create():
    """POST /api/l2/create {"btc_sats":100000,"buyer_brn":"brn1q..."} -> cria escrow OPEN"""
    _, _, l2_mgr = get_db()
    if not L2_ENABLED or not l2_mgr:
        return jsonify({"error": "L2 desabilitado"}), 400
    try:
        data = request.json
        if not data:
            return jsonify({"error": "json required"}), 400
        btc_sats = int(data.get('btc_sats', 0))
        buyer = data.get('buyer_brn', '').strip()
        if not buyer:
            return jsonify({"error": "buyer_brn obrigatório (brn1q...)"}), 400

        order = l2_mgr.create_order(btc_sats, buyer)
        return jsonify(order)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/l2/order/<escrow_id>', methods=['GET'])
def l2_order(escrow_id):
    """GET /api/l2/order/escrow_abc123 -> status OPEN / BTC_DETECTED / RELEASED"""
    _, _, l2_mgr = get_db()
    if not L2_ENABLED or not l2_mgr:
        return jsonify({"error": "L2 desabilitado"}), 400
    try:
        order = l2_mgr.get_order(escrow_id)
        if not order:
            return jsonify({"error": "escrow not found"}), 404
        # explica fluxo
        status_map = {
            "OPEN": "Aguardando BTC no endereço",
            "BTC_DETECTED": "BTC confirmado via RPC, na mempool aguardando mineração PoW",
            "RELEASED": "Minerado! BRN creditado na carteira",
            "EXPIRED": "Expirado (24h)",
            "CANCELLED": "Cancelado"
        }
        order["status_desc"] = status_map.get(order["status"], order["status"])
        return jsonify(order)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/l2/list', methods=['GET'])
def l2_list():
    """GET /api/l2/list?buyer=brn1q...&status=OPEN"""
    db, _, l2_mgr = get_db()
    if not L2_ENABLED or not l2_mgr:
        return jsonify({"error": "L2 desabilitado"}), 400
    try:
        buyer = request.args.get('buyer')
        status = request.args.get('status', 'OPEN')
        if buyer:
            escrows = [e for e in db.get_l2_escrows() if e.get('buyer') == buyer]
        else:
            escrows = db.get_l2_escrows(status=status) if status!= 'ALL' else db.get_l2_escrows()
        return jsonify(escrows[:100]) # limite 100
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/l2/stats')
def l2_stats():
    db, chain, l2_mgr = get_db()
    if not L2_ENABLED:
        return jsonify({"error": "L2 desabilitado"}), 400
    try:
        if l2_mgr:
            s = l2_mgr.stats()
        else:
            s = {"open": 0, "btc_detected": 0, "released": 0}

        # stats da chain L2
        if chain:
            l2_chain_stats = chain.get_l2_stats()
            s.update(l2_chain_stats)

        s["btc_address"] = btc_config.BTC_RECEIVE_ADDRESS
        s["network"] = btc_config.BTC_NETWORK
        s["rate"] = f"1 BTC = {btc_config.BRN_PER_BTC} BRN"
        s["flow"] = "BTC RPC (Blockstream) -> BTC_DETECTED -> mempool -> mine_block() -> RELEASED"
        s["validation"] = "PoW - BRN liberado só após bloco minerado"
        return jsonify(s)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/l2/cancel/<escrow_id>', methods=['POST'])
def l2_cancel(escrow_id):
    db, _, _ = get_db()
    if not L2_ENABLED:
        return jsonify({"error": "L2 desabilitado"}), 400
    try:
        escrow = db.get_l2_escrow_by_id(escrow_id)
        if not escrow:
            return jsonify({"error": "escrow not found"}), 404
        if escrow["status"]!= "OPEN":
            return jsonify({"error": f"só pode cancelar OPEN, status atual {escrow['status']}"}), 400
        db.update_l2_escrow_status(escrow_id, "CANCELLED")
        return jsonify({"ok": True, "escrow_id": escrow_id, "status": "CANCELLED"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# EXPLORER / HEALTH
# ============================================================

@app.route('/')
def index():
    return jsonify({
        "name": "BRN Node API v8.1 L2 MINERADO",
        "version": "8.1.0",
        "endpoints": [
            "GET /api/status",
            "GET /api/balance/<address>",
            "GET /api/block/<height>",
            "GET /api/blocks/latest?limit=10",
            "GET /api/mempool",
            "POST /api/tx/send",
            "GET /api/l2/quote?btc_sats=100000",
            "POST /api/l2/create",
            "GET /api/l2/order/<escrow_id>",
            "GET /api/l2/list?buyer=brn1q...&status=OPEN",
            "GET /api/l2/stats"
        ],
        "l2_flow": "BTC RPC -> mempool -> PoW -> BRN liberado",
        "btc_address": btc_config.BTC_RECEIVE_ADDRESS if L2_ENABLED else "configure btc_config.py"
    })

@app.route('/health')
def health():
    db, _, _ = get_db()
    return jsonify({"ok": True, "height": db.height(), "l2_enabled": L2_ENABLED})

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    get_db() # init
    print(f"=== BRN API v8.1 L2 MINERADO ===")
    print(f"DB: {DB_PATH}")
    if L2_ENABLED:
        print(f"BTC Address: {btc_config.BTC_RECEIVE_ADDRESS}")
        print(f"Rate: 1 BTC = {btc_config.BRN_PER_BTC} BRN")
        print(f"Flow: BTC RPC -> mempool -> mine_block() PoW -> RELEASED")
    print(f"HTTP: http://0.0.0.0:{WEB_PORT}")
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, debug=False, use_reloader=False)