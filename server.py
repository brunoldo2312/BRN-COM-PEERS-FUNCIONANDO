"""
server.py — Backend HTTP do nó BRN (v9.0)
Mudanças v9.0:
  [FIX] bind default 127.0.0.1 (BRN_WEB_HOST=0.0.0.0 explícito)
  [FIX] admin token (BRN_ADMIN_TOKEN) em /api/mine, /api/miner/*, /api/faucet
  [FIX] caches (_ip_history, _cache_saldos, _faucet_history) com cleanup
  [FIX] salvar_wallets atômico + lock
  [FIX] auto-mine opt-in (BRN_AUTO_MINE=1)
  [FIX] ProxyFix opcional (BRN_BEHIND_PROXY=1)
  [FIX] CORS restrito se BRN_CORS_ORIGIN setado
  [FIX] `_find_tx_in_chain` usa db.get_tx_by_txid se existir
  [FIX] /api/l2/* retorna 503 (não 400) quando L2 desabilitado
  [FIX] mnemonic nunca logado + Cache-Control: no-store
"""
from __future__ import annotations

import os
import time
import json
import hmac
import logging
import threading
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS

from wallet import Wallet, WalletManager, HDWalletManager
from blockchain import Blockchain, make_coinbase, txid as calc_txid, signing_hash

log = logging.getLogger("server")

# ============================================================
# CONFIG
# ============================================================
WEB_HOST         = os.environ.get("BRN_WEB_HOST", "127.0.0.1")
BEHIND_PROXY     = os.environ.get("BRN_BEHIND_PROXY", "0") == "1"
CORS_ORIGINS     = [o.strip() for o in
                    os.environ.get("BRN_CORS_ORIGIN", "").split(",")
                    if o.strip()]
ADMIN_TOKEN      = os.environ.get("BRN_ADMIN_TOKEN", "").strip()

CHAIN            = Blockchain("brn_v2_chain.db")
CHAIN_LOCK       = threading.RLock()

WALLETS_FILE         = "user_wallets.json"
CURRENT_WALLET_FILE  = "current_wallet.json"

FAUCET_AMOUNT_BRN    = 10
FAUCET_MAX_PER_ADDRESS = 3
FAUCET_COOLDOWN_S    = 60 * 60

RATE_LIMIT      = 30
RATE_WINDOW_S   = 60

CACHE_TTL_S     = 5

# ---- L2 (opcional) ----
try:
    from l2_manager import L2Manager, L2Error
    import btc_config
    L2_ENABLED = True
except ImportError:
    L2_ENABLED = False
    L2Manager = None
    L2Error = Exception
    btc_config = None

_l2_mgr = None
_l2_lock = threading.Lock()


def _get_l2():
    global _l2_mgr
    if not L2_ENABLED:
        return None
    with _l2_lock:
        if _l2_mgr is None:
            try:
                _l2_mgr = L2Manager(CHAIN.db, CHAIN)
                log.info("L2Manager inicializado")
            except Exception as e:
                log.warning(f"L2Manager erro: {e}")
                _l2_mgr = None
        return _l2_mgr


# ============================================================
# App
# ============================================================
app = Flask(__name__)

if BEHIND_PROXY:
    try:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    except ImportError:
        pass

if CORS_ORIGINS:
    CORS(app, origins=CORS_ORIGINS)
else:
    # sem CORS por padrão — browser bloqueia cross-origin
    pass


# ============================================================
# Admin guard
# ============================================================
def _require_admin() -> bool:
    if not ADMIN_TOKEN:
        return True    # dev
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        return False
    return hmac.compare_digest(hdr[7:], ADMIN_TOKEN)


def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _require_admin():
            return jsonify({"ok": False, "msg": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# Rate limit + caches
# ============================================================
_ip_history: dict[str, list] = {}
_rate_lock = threading.Lock()

_cache_saldos: dict[str, tuple] = {}
_cache_lock = threading.Lock()

_faucet_history: dict[str, list] = {}


def _rate_limit(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        ip = request.remote_addr or "?"
        agora = time.time()
        with _rate_lock:
            hist = _ip_history.setdefault(ip, [])
            hist[:] = [t for t in hist if agora - t < RATE_WINDOW_S]
            if len(hist) >= RATE_LIMIT:
                return jsonify({"success": False,
                                "error": "Muitas requisicoes."}), 429
            hist.append(agora)
        return f(*args, **kwargs)
    return wrapper


def _saldo_cache_get(addr):
    with _cache_lock:
        if addr in _cache_saldos:
            ts, val = _cache_saldos[addr]
            if time.time() - ts < CACHE_TTL_S:
                return val
    return None


def _saldo_cache_set(addr, val):
    with _cache_lock:
        _cache_saldos[addr] = (time.time(), val)


def _saldo_cache_invalidate(addr):
    with _cache_lock:
        _cache_saldos.pop(addr, None)


# ============================================================
# Wallets JSON (atômico + lock)
# ============================================================
_wallets_lock = threading.Lock()


def carregar_wallets() -> dict:
    if not os.path.exists(WALLETS_FILE):
        return {}
    try:
        with open(WALLETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def salvar_wallets(w: dict) -> None:
    with _wallets_lock:
        tmp = f"{WALLETS_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(w, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, WALLETS_FILE)


def _registrar_pubkey(addr, pk):
    if not addr or not pk:
        return
    try:
        wallets = carregar_wallets()
        if wallets.get(addr, {}).get("public_key") != pk:
            wallets[addr] = {"public_key": pk}
            salvar_wallets(wallets)
    except Exception:
        pass


# ============================================================
# Pubkey resolution
# ============================================================
def _pubkey_from_db_or_payload(addr: str, payload_pubkey: str = "",
                               payload_sk: str = "") -> str:
    if payload_pubkey:
        return payload_pubkey.strip()

    if payload_sk:
        try:
            _w = Wallet(private_key_hex=payload_sk.strip())
            if _w.address == addr:
                pk = _w.pub_hex
                _registrar_pubkey(addr, pk)
                return pk
        except Exception:
            pass

    try:
        if os.path.exists(CURRENT_WALLET_FILE):
            with open(CURRENT_WALLET_FILE, "r", encoding="utf-8") as f:
                cw = json.load(f)
            if (cw.get("address") or "").strip() == addr:
                pk = (cw.get("public_key") or cw.get("pubkey") or "").strip()
                if pk:
                    _registrar_pubkey(addr, pk)
                    return pk
    except Exception:
        pass

    try:
        wallets = carregar_wallets()
        entry = wallets.get(addr) or {}
        pk = (entry.get("public_key") or entry.get("pubkey") or "").strip()
        if pk:
            return pk
    except Exception:
        pass

    try:
        for u in CHAIN.db.get_utxos(addr):
            pk = (u.get("pubkey") or "").strip()
            if pk:
                return pk
    except Exception:
        pass

    return ""


# ============================================================
# Tx lookup (usa índice se existir)
# ============================================================
def _find_tx_in_mempool(txid_str: str):
    try:
        if hasattr(CHAIN.db, "get_mempool_tx"):
            return CHAIN.db.get_mempool_tx(txid_str)
        for t in CHAIN.db.all_mempool(limit=10000):
            if t.get("txid") == txid_str:
                return t
    except Exception:
        pass
    return None


def _find_tx_in_chain(txid_str: str):
    try:
        if hasattr(CHAIN.db, "get_tx_by_txid"):
            return CHAIN.db.get_tx_by_txid(txid_str)
    except Exception:
        pass

    # fallback: varredura (lento — migre para o índice)
    try:
        altura = CHAIN.db.height()
        for h in range(altura + 1):
            try:
                block = CHAIN.db.get_block(h)
            except Exception:
                continue
            if not block:
                continue
            for tx in block.get("transactions", []):
                if tx.get("txid") == txid_str:
                    return tx, h
    except Exception:
        pass
    return None, None


# ============================================================
# Cleanup thread
# ============================================================
def _cleanup_loop():
    while True:
        time.sleep(300)
        agora = time.time()
        try:
            with _rate_lock:
                for ip in [k for k, v in _ip_history.items()
                           if not v or agora - v[-1] > RATE_WINDOW_S]:
                    del _ip_history[ip]
            with _cache_lock:
                for a in [k for k, (ts, _) in _cache_saldos.items()
                          if agora - ts > CACHE_TTL_S * 10]:
                    del _cache_saldos[a]
            for a in [k for k, v in _faucet_history.items()
                      if not v or agora - v[-1] > FAUCET_COOLDOWN_S]:
                del _faucet_history[a]
        except Exception as e:
            log.warning(f"cleanup: {e}")


threading.Thread(target=_cleanup_loop, daemon=True,
                 name="CacheCleanup").start()


# ============================================================
# CARTEIRA
# ============================================================
@app.route("/api/nova-carteira", methods=["POST"])
@_rate_limit
def nova_carteira():
    try:
        w = Wallet()
        dados = {
            "success": True,
            "address": w.address,
            "private_key": w.priv_hex,
            "public_key": w.pub_hex,
            "warning": "Guarde a chave privada."
        }
        wallets = carregar_wallets()
        wallets[w.address] = {"public_key": w.pub_hex}
        salvar_wallets(wallets)
        return jsonify(dados)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/saldo/<address>", methods=["GET"])
def saldo(address):
    try:
        cached = _saldo_cache_get(address)
        if cached is None:
            utxos = CHAIN.db.get_utxos(address)
            cached = sum(u["amount"] for u in utxos)
            _saldo_cache_set(address, cached)
        return jsonify({
            "success": True, "address": address,
            "balance_sats": cached, "balance_brn": cached / 10**8,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/portfolio/<address>", methods=["GET"])
def portfolio(address):
    try:
        cached = _saldo_cache_get(address)
        if cached is None:
            utxos = CHAIN.db.get_utxos(address)
            cached = sum(u["amount"] for u in utxos)
            _saldo_cache_set(address, cached)
        return jsonify({"portfolio": {"BRN": cached / 10**8, "KYC": 0}})
    except Exception as e:
        return jsonify({"portfolio": {}, "error": str(e)}), 500


# ============================================================
# Tx status
# ============================================================
@app.route("/api/tx-status/<txid_str>", methods=["GET"])
def tx_status(txid_str):
    try:
        if not txid_str or len(txid_str) != 64:
            return jsonify({"ok": False, "msg": "TXID invalido"}), 400

        mem_tx = _find_tx_in_mempool(txid_str)
        if mem_tx:
            return jsonify({
                "ok": True, "txid": txid_str, "status": "pending",
                "block_height": None, "confirmations": 0,
            })

        chain_tx, block_height = _find_tx_in_chain(txid_str)
        if chain_tx is not None:
            altura = CHAIN.db.height()
            confs = max(0, altura - block_height)
            return jsonify({
                "ok": True, "txid": txid_str, "status": "confirmed",
                "block_height": block_height, "confirmations": confs,
                "timestamp": chain_tx.get("timestamp", 0),
            })

        return jsonify({
            "ok": True, "txid": txid_str, "status": "not_found",
            "block_height": None, "confirmations": 0,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/verificar-recebimento/<address>/<txid_str>", methods=["GET"])
def verificar_recebimento(address, txid_str):
    try:
        if not WalletManager.validate_address(address):
            return jsonify({"ok": False, "msg": "Endereco invalido"}), 400
        if not txid_str or len(txid_str) != 64:
            return jsonify({"ok": False, "msg": "TXID invalido"}), 400

        mem_tx = _find_tx_in_mempool(txid_str)
        if mem_tx:
            valor_mem = sum(
                o.get("amount", 0) for o in mem_tx.get("outputs", [])
                if o.get("address") == address
            )
            return jsonify({
                "ok": True, "chegou": False, "status": "pending",
                "valor": valor_mem, "confirmacoes": 0, "block_height": None,
                "msg": "Transacao esta na mempool." if valor_mem
                       else "Endereco nao esta nos outputs.",
            })

        chain_tx, block_height = _find_tx_in_chain(txid_str)
        if chain_tx is None:
            return jsonify({
                "ok": True, "chegou": False, "status": "not_found",
                "valor": 0, "confirmacoes": 0, "block_height": None,
                "msg": "TXID nao encontrado.",
            })

        valor = sum(
            o.get("amount", 0) for o in chain_tx.get("outputs", [])
            if o.get("address") == address
        )
        altura = CHAIN.db.height()
        confs = max(0, altura - block_height)
        chegou = valor > 0
        msg = (f"Confirmado no bloco #{block_height} com {confs} confirmacoes."
               if chegou else "Endereco nao esta nos outputs.")

        return jsonify({
            "ok": True, "chegou": chegou, "status": "confirmed",
            "valor": valor, "confirmacoes": confs, "block_height": block_height,
            "msg": msg,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# Transfer
# ============================================================
@app.route("/api/transfer", methods=["POST"])
@app.route("/api/send", methods=["POST"])
@app.route("/api/enviar", methods=["POST"])
@_rate_limit
def transfer():
    try:
        data = request.get_json(force=True) or {}
        sender = data.get("from", "").strip()
        to = data.get("to", "").strip()
        asset_id = data.get("asset_id", "BRN")
        amount = data.get("amount")
        # chave privada pode vir via header (não logada) ou body
        sk = (request.headers.get("X-BRN-Sk", "")
              or data.get("private_key", ""))

        if asset_id != "BRN":
            return jsonify({"ok": False, "msg": "So BRN."}), 400
        if not WalletManager.validate_address(sender):
            return jsonify({"ok": False, "msg": "Remetente invalido."}), 400
        if not WalletManager.validate_address(to):
            return jsonify({"ok": False, "msg": "Destinatario invalido."}), 400
        if sender == to:
            return jsonify({"ok": False, "msg": "Nao pode enviar para si."}), 400

        try:
            amount_sats = int(float(amount) * 10**8)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "msg": "Valor invalido."}), 400
        if amount_sats <= 0:
            return jsonify({"ok": False, "msg": "Valor deve ser > 0."}), 400

        try:
            w = Wallet(private_key_hex=sk)
        except Exception:
            return jsonify({"ok": False, "msg": "Chave privada invalida."}), 400
        if w.address != sender:
            return jsonify({"ok": False, "msg": "Chave privada nao corresponde."}), 400

        with CHAIN_LOCK:
            utxos = CHAIN.db.get_utxos(sender)
            utxos.sort(key=lambda u: u["amount"], reverse=True)
            total, escolhidos = 0, []
            for u in utxos:
                escolhidos.append(u)
                total += u["amount"]
                if total >= amount_sats + 1000:
                    break
            if total < amount_sats:
                return jsonify({"ok": False,
                                "msg": f"Saldo insuficiente ({total/1e8:.8f} BRN)."}), 400

            FEE = 1000
            troco = total - amount_sats - FEE
            outputs = [{"address": to, "amount": amount_sats, "pubkey": ""}]
            if troco > 0:
                outputs.append({"address": sender, "amount": troco, "pubkey": ""})

            inputs = [{"txid": u["txid"], "vout": u["vout"],
                       "pubkey": w.pub_hex, "signature": ""}
                      for u in escolhidos]

            nonce = CHAIN.db.get_nonce_for_pubkey(w.pub_hex)
            tx = {"txid": "", "inputs": inputs, "outputs": outputs,
                  "timestamp": int(time.time()), "locktime": 0, "nonce": nonce}

            h = signing_hash(tx)
            for inp in tx["inputs"]:
                inp["signature"] = w.sign(h)
            tx["txid"] = calc_txid(tx)

            ok, msg = CHAIN.submit_tx(tx)
        if not ok:
            return jsonify({"ok": False, "msg": msg}), 400

        _saldo_cache_invalidate(sender)
        _saldo_cache_invalidate(to)
        return jsonify({"ok": True, "txid": tx["txid"], "nonce": nonce,
                        "msg": "Aceita na mempool."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# Miner endpoints (admin)
# ============================================================
@app.route("/api/mine", methods=["POST"])
@_rate_limit
@_admin_required
def mine():
    try:
        data = request.get_json(force=True) or {}
        miner = (data.get("validator_address")
                 or data.get("address") or "").strip()
        if not WalletManager.validate_address(miner):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400

        miner_pubkey = _pubkey_from_db_or_payload(
            miner,
            data.get("miner_pubkey", "") or data.get("pubkey", ""),
            data.get("private_key", "") or data.get("privatekey", ""),
        )
        if not miner_pubkey:
            return jsonify({"ok": False,
                            "msg": "Pubkey desconhecida."}), 400

        with CHAIN_LOCK:
            block = CHAIN.mine_block(miner, miner_pubkey)
        if not block:
            return jsonify({"ok": False, "msg": "Falha ao minerar."}), 500
        _saldo_cache_invalidate(miner)
        return jsonify({"ok": True,
                        "msg": f"Bloco #{block['height']} minerado!",
                        "block": {"height": block["height"],
                                  "hash": block["hash"],
                                  "txs": len(block["transactions"]),
                                  "difficulty": block["difficulty"],
                                  "nonce": block["nonce"]}})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/miner/start", methods=["POST"])
@_rate_limit
@_admin_required
def miner_start():
    try:
        data = request.get_json(force=True) or {}
        addr = (data.get("validator_address")
                or data.get("address") or "").strip()
        pubkey_payload = (data.get("miner_pubkey")
                          or data.get("pubkey")
                          or data.get("public_key")
                          or "").strip()
        sk_payload = (data.get("private_key")
                      or data.get("privatekey")
                      or "").strip()

        if not WalletManager.validate_address(addr):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400

        pubkey = _pubkey_from_db_or_payload(addr, pubkey_payload, sk_payload)
        if not pubkey:
            return jsonify({
                "ok": False,
                "msg": "Pubkey desconhecida."
            }), 400

        from miner_loop import get_miner
        m = get_miner(CHAIN)
        ok, msg = m.start(addr, pubkey)
        if not ok:
            return jsonify({"ok": False, "msg": msg, **m.status()}), 400
        return jsonify({"ok": True, "msg": "Minerador iniciado.", **m.status()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/miner/stop", methods=["POST"])
@_rate_limit
@_admin_required
def miner_stop():
    try:
        from miner_loop import get_miner
        m = get_miner(CHAIN)
        ok, msg = m.stop()
        return jsonify({"ok": ok, "msg": msg, **m.status()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/miner/on", methods=["POST"])
@app.route("/api/start-mining", methods=["POST"])
def miner_start_alias():
    return miner_start()


@app.route("/api/miner/off", methods=["POST"])
@app.route("/api/stop-mining", methods=["POST"])
def miner_stop_alias():
    return miner_stop()


@app.route("/api/miner/status", methods=["GET"])
def miner_status():
    try:
        from miner_loop import get_miner
        m = get_miner(CHAIN)
        st = m.status()
        return jsonify({
            "running": st.get("running", False),
            "address": st.get("address", ""),
            "pubkey": st.get("pubkey", ""),
            "blocks_mined": st.get("blocks_mined", 0),
            "count": st.get("count", st.get("blocks_mined", 0)),
            "last_height": st.get("last_height"),
            "uptime": st.get("uptime", 0),
            "consecutive_failures": st.get("consecutive_failures", 0),
            "hashrate": st.get("hashrate", 0.0),
            "hashes_done": st.get("hashes_done", 0),
            "eta_seconds": st.get("eta_seconds"),
            "height": CHAIN.db.height(),
            "difficulty": CHAIN.current_difficulty(),
            "last_error": st.get("last_error", ""),
        })
    except Exception as e:
        return jsonify({"running": False, "error": str(e)}), 200


# ============================================================
# Faucet (admin)
# ============================================================
@app.route("/api/faucet", methods=["POST"])
@_rate_limit
@_admin_required
def faucet():
    try:
        data = request.get_json(force=True) or {}
        addr = (data.get("address") or data.get("validator_address")
                or "").strip()
        if not WalletManager.validate_address(addr):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400

        agora = time.time()
        hist = _faucet_history.setdefault(addr, [])
        hist[:] = [t for t in hist if agora - t < FAUCET_COOLDOWN_S]
        if len(hist) >= FAUCET_MAX_PER_ADDRESS:
            return jsonify({"ok": False, "msg": "Limite atingido."}), 429
        if hist and agora - hist[-1] < FAUCET_COOLDOWN_S:
            falta = int(FAUCET_COOLDOWN_S - (agora - hist[-1]))
            return jsonify({"ok": False, "msg": f"Aguarde {falta}s."}), 429

        miner_pubkey = _pubkey_from_db_or_payload(
            addr,
            data.get("miner_pubkey", "") or data.get("pubkey", ""),
            data.get("private_key", "") or data.get("privatekey", ""),
        )
        if not miner_pubkey:
            return jsonify({"ok": False,
                            "msg": "Pubkey desconhecida."}), 400

        with CHAIN_LOCK:
            block = CHAIN.mine_block(addr, miner_pubkey)
        if not block:
            return jsonify({"ok": False, "msg": "Falha ao minerar."}), 500
        hist.append(agora)
        _saldo_cache_invalidate(addr)

        reward_sats = 0
        try:
            cb = block["transactions"][0]
            reward_sats = sum(o.get("amount", 0) for o in cb.get("outputs", [])
                              if o.get("address") == addr)
        except Exception:
            reward_sats = 0

        return jsonify({"ok": True,
                        "msg": f"Faucet enviado! +{reward_sats/1e8} BRN",
                        "txid": block["transactions"][0]["txid"],
                        "amount": reward_sats / 1e8})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# Chain info
# ============================================================
@app.route("/api/chain-info", methods=["GET"])
def chain_info():
    try:
        with CHAIN_LOCK:
            return jsonify({
                "success": True, "name": "BrunoCoin", "ticker": "BRN",
                "height": CHAIN.db.height(),
                "tip_hash": CHAIN.db.tip_hash(),
                "reward": CHAIN.current_reward(CHAIN.db.height() + 1),
                "difficulty": CHAIN.current_difficulty(),
            })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def status():
    try:
        with CHAIN_LOCK:
            return jsonify({
                "name": "BrunoCoin", "ticker": "BRN",
                "height": CHAIN.db.height(),
                "tip_hash": CHAIN.db.tip_hash(),
                "utxos": CHAIN.db.count_utxos(),
                "mempool": len(CHAIN.db.all_mempool(limit=10000)),
                "peers": CHAIN.db.contar_peers(apenas_ativos=True),
                "reward": CHAIN.current_reward(CHAIN.db.height() + 1),
                "difficulty": CHAIN.current_difficulty(),
                "contracts": (CHAIN.db.contract_count()
                              if hasattr(CHAIN.db, "contract_count") else 0),
                "l2_enabled": L2_ENABLED,
            })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/fee-estimate", methods=["GET"])
def fee_estimate():
    try:
        return jsonify({"success": True,
                        "low": CHAIN.estimate_fee("low"),
                        "medium": CHAIN.estimate_fee("medium"),
                        "high": CHAIN.estimate_fee("high"),
                        "min_relay_fee": 1000})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/work", methods=["GET"])
def work():
    try:
        return jsonify({"success": True, "height": CHAIN.db.height(),
                        "cumulative_work": CHAIN.cumulative_work()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/nonce/<pubkey>", methods=["GET"])
def get_nonce(pubkey):
    try:
        return jsonify({"success": True, "pubkey": pubkey,
                        "next_nonce": CHAIN.db.get_nonce_for_pubkey(pubkey)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# HD wallet
# ============================================================
@app.route("/api/hd/create", methods=["POST"])
@_rate_limit
def hd_create():
    try:
        data = request.get_json(force=True) or {}
        strength = int(data.get("strength", 128))
        if strength not in (128, 160, 192, 224, 256):
            return jsonify({"ok": False, "msg": "strength invalido"}), 400
        result = HDWalletManager.create(strength=strength)
        _registrar_pubkey(result["address"], result.get("public_key", ""))
        resp = jsonify({"ok": True, "warning": "GUARDE o mnemonico.", **result})
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hd/derive", methods=["POST"])
@_rate_limit
def hd_derive():
    try:
        data = request.get_json(force=True) or {}
        mn = data.get("mnemonic", "").strip()
        index = int(data.get("index", 0))
        if not HDWalletManager.validate_mnemonic(mn):
            return jsonify({"ok": False, "msg": "Mnemonico invalido"}), 400
        result = HDWalletManager.from_mnemonic(mn, index=index)
        _registrar_pubkey(result["address"], result.get("public_key", ""))
        resp = jsonify({"ok": True, **result})
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return resp
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# Peers
# ============================================================
@app.route("/api/peers/score", methods=["GET"])
def peers_score():
    try:
        return jsonify({"success": True,
                        "peers": CHAIN.db.listar_peers(apenas_ativos=False),
                        "banned": CHAIN.db.listar_peers_maus(score_min=-100)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# Sync info
# ============================================================
@app.route("/api/sync-info", methods=["GET"])
def sync_info():
    result = {
        "ok": True,
        "sync": {"percent": 100.0, "height": 0, "target": 0,
                 "peers": 0, "work": 0},
        "miner_target": "",
        "bridge": {"onramp_ativo": False, "offramp_ativo": False,
                   "taxa": 0, "btc_cofre": ""},
        "bridge_onramp": {"processados": 0, "ultima_sync": None},
    }

    try:
        local_h = CHAIN.db.height()
        result["sync"]["height"] = local_h
    except Exception as e:
        result["ok"] = False
        result["erro"] = f"height: {e}"
        return jsonify(result), 200

    try:
        result["sync"]["peers"] = CHAIN.db.contar_peers(apenas_ativos=True)
    except Exception as e:
        log.warning(f"[sync-info] contar_peers: {e}")

    target_h = local_h
    try:
        todos = CHAIN.db.listar_peers(apenas_ativos=False)
        heights = []
        for p in todos or []:
            try:
                h = p["height"] if isinstance(p, dict) else None
                if h:
                    heights.append(int(h))
            except Exception:
                continue
        if heights:
            target_h = max([target_h] + heights)
    except Exception as e:
        log.warning(f"[sync-info] listar_peers: {e}")

    result["sync"]["target"] = target_h

    try:
        if target_h > 0:
            result["sync"]["percent"] = min(100.0,
                                             round(local_h / target_h * 100, 2))
    except Exception:
        pass

    try:
        result["miner_target"] = CHAIN.db.get_meta("miner_address") or ""
    except Exception:
        pass

    try:
        if L2_ENABLED and btc_config is not None:
            result["bridge"]["onramp_ativo"] = True
            result["bridge"]["taxa"] = int(getattr(btc_config,
                                                    "BRN_PER_BTC", 0) or 0)
            result["bridge"]["btc_cofre"] = getattr(btc_config,
                                                     "BTC_RECEIVE_ADDRESS",
                                                     "") or ""
    except Exception as e:
        log.warning(f"[sync-info] bridge: {e}")

    return jsonify(result), 200


# ============================================================
# Contracts
# ============================================================
@app.route("/api/contract/deploy", methods=["POST"])
@_rate_limit
def contract_deploy():
    try:
        data = request.get_json(force=True) or {}
        owner = (data.get("owner") or "").strip()
        code = data.get("code")
        metadata = data.get("metadata")

        if not WalletManager.validate_address(owner):
            return jsonify({"ok": False, "msg": "owner invalido"}), 400
        if not isinstance(code, dict):
            return jsonify({"ok": False, "msg": "code deve ser dict"}), 400

        try:
            from contracts import ContractVM, deploy
        except ImportError as e:
            return jsonify({"ok": False,
                            "msg": f"contracts.py nao encontrado: {e}"}), 500

        ok, msg = ContractVM.validate(code)
        if not ok:
            return jsonify({"ok": False, "msg": msg}), 400

        r = deploy(CHAIN, owner, code, metadata)
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/contract/call", methods=["POST"])
@_rate_limit
def contract_call():
    try:
        data = request.get_json(force=True) or {}
        contract_id = (data.get("contract_id") or "").strip()
        caller = (data.get("caller") or "").strip()
        args = data.get("args") or {}

        if not contract_id:
            return jsonify({"ok": False,
                            "msg": "contract_id obrigatorio"}), 400

        try:
            from contracts import call as _call
        except ImportError as e:
            return jsonify({"ok": False,
                            "msg": f"contracts.py nao encontrado: {e}"}), 500

        r = _call(CHAIN, contract_id, caller, args)
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/contract/<contract_id>", methods=["GET"])
def contract_info(contract_id):
    try:
        c = CHAIN.db.contract_get(contract_id)
        if not c:
            return jsonify({"ok": False, "msg": "contrato nao existe"}), 404
        return jsonify({
            "ok": True,
            "contract": c,
            "state": CHAIN.db.contract_get_state(contract_id),
            "events": CHAIN.db.contract_get_events(contract_id, limit=20),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/contracts", methods=["GET"])
def contracts_list():
    try:
        owner = (request.args.get("owner") or "").strip() or None
        return jsonify({
            "ok": True,
            "contracts": CHAIN.db.contract_list(owner=owner, limit=100),
            "total": CHAIN.db.contract_count(),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# L2
# ============================================================
@app.route("/api/l2/quote", methods=["GET"])
def l2_quote():
    l2 = _get_l2()
    if not l2:
        return jsonify({"error": "L2 desabilitado"}), 503
    try:
        sats = int(request.args.get("btc_sats", 100000))
        q = l2.quote(sats)
        q["btc_address"] = btc_config.BTC_RECEIVE_ADDRESS
        q["network"] = btc_config.BTC_NETWORK
        q["min_confirmations"] = btc_config.BTC_MIN_CONFIRMATIONS
        return jsonify(q)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/l2/create", methods=["POST"])
@_rate_limit
def l2_create():
    l2 = _get_l2()
    if not l2:
        return jsonify({"error": "L2 desabilitado"}), 503
    try:
        data = request.get_json(force=True) or {}
        btc_sats = int(data.get("btc_sats", 0))
        buyer = (data.get("buyer_brn") or "").strip()
        if not buyer:
            return jsonify({"error": "buyer_brn obrigatorio"}), 400
        if not WalletManager.validate_address(buyer):
            return jsonify({"error": "buyer_brn invalido"}), 400
        if btc_sats <= 0:
            return jsonify({"error": "btc_sats deve ser > 0"}), 400

        order = l2.create_order(btc_sats, buyer)
        return jsonify(order)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/l2/order/<escrow_id>", methods=["GET"])
def l2_order(escrow_id):
    l2 = _get_l2()
    if not l2:
        return jsonify({"error": "L2 desabilitado"}), 503
    try:
        order = l2.get_order(escrow_id)
        if not order:
            return jsonify({"error": "escrow not found"}), 404
        status_map = {
            "OPEN": "Aguardando BTC no endereco",
            "BTC_DETECTED": "BTC confirmado via RPC",
            "RELEASED": "Minerado! BRN creditado na carteira",
            "EXPIRED": "Expirado (24h)",
            "CANCELLED": "Cancelado",
        }
        order["status_desc"] = status_map.get(order["status"], order["status"])
        return jsonify(order)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/l2/list", methods=["GET"])
def l2_list():
    l2 = _get_l2()
    if not l2:
        return jsonify({"error": "L2 desabilitado"}), 503
    try:
        buyer = request.args.get("buyer")
        status = request.args.get("status", "OPEN")

        if not hasattr(CHAIN.db, "get_l2_escrows"):
            return jsonify({"error": "db.py nao tem get_l2_escrows"}), 500

        if buyer:
            escrows = [e for e in CHAIN.db.get_l2_escrows()
                       if e.get("buyer") == buyer]
        elif status != "ALL":
            escrows = CHAIN.db.get_l2_escrows(status=status)
        else:
            escrows = CHAIN.db.get_l2_escrows()

        return jsonify(escrows[:100])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/l2/stats", methods=["GET"])
def l2_stats():
    try:
        if not L2_ENABLED:
            return jsonify({"error": "L2 desabilitado"}), 503

        l2 = _get_l2()
        if l2:
            s = l2.stats()
        else:
            s = {"open": 0, "btc_detected": 0, "released": 0}

        s["btc_address"] = getattr(btc_config, "BTC_RECEIVE_ADDRESS", "")
        s["network"] = getattr(btc_config, "BTC_NETWORK", "")
        s["rate"] = f"1 BTC = {getattr(btc_config, 'BRN_PER_BTC', 0)} BRN"
        return jsonify(s)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/l2/cancel/<escrow_id>", methods=["POST"])
@_rate_limit
def l2_cancel(escrow_id):
    if not L2_ENABLED:
        return jsonify({"error": "L2 desabilitado"}), 503
    try:
        if not hasattr(CHAIN.db, "get_l2_escrow_by_id"):
            return jsonify({"error": "db.py nao tem get_l2_escrow_by_id"}), 500

        escrow = CHAIN.db.get_l2_escrow_by_id(escrow_id)
        if not escrow:
            return jsonify({"error": "escrow not found"}), 404
        if escrow["status"] != "OPEN":
            return jsonify({"error":
                            f"so pode cancelar OPEN, status atual {escrow['status']}"}), 400

        CHAIN.db.update_l2_escrow_status(escrow_id, "CANCELLED")
        return jsonify({"ok": True, "escrow_id": escrow_id,
                        "status": "CANCELLED"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# Health / index
# ============================================================
@app.route("/health", methods=["GET"])
def health():
    try:
        return jsonify({
            "ok": True,
            "height": CHAIN.db.height(),
            "l2_enabled": L2_ENABLED,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "BRN Node API",
        "version": "9.0",
        "l2_enabled": L2_ENABLED,
        "admin_protected": bool(ADMIN_TOKEN),
    })


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    print(f"BRN Server v9.0 — http://{WEB_HOST}:{port}")
    print(f"  L2: {'ATIVO' if L2_ENABLED else 'DESABILITADO'}")
    print(f"  Admin token: {'SIM' if ADMIN_TOKEN else 'NÃO (dev mode)'}")

    if os.environ.get("BRN_AUTO_MINE", "0") == "1":
        try:
            from miner_loop import iniciar_mineracao
            iniciar_mineracao(CHAIN)
            print("[server] auto-miner iniciado")
        except Exception as e:
            print(f"[server] auto-miner falhou: {e}")

    app.run(host=WEB_HOST, port=port, debug=False, threaded=True)