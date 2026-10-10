"""
main.py — Entrypoint unificado do nó BRN (v11)
================================================================
Herança v8.3: L2 BTC->BRN + Checkpoints assinados.
Herança v10.1: Audit, HTTPS opcional, verify de boot, shutdown limpo.

v11 (correções e adições):
  [FIX] BASE_DIR definido ANTES de _bootstrap_env (não depende do CWD)
  [FIX] load_wallet/save_wallet com ordem correta de argumentos
  [FIX] --password rejeitado (visível em ps)
  [FIX] _is_origin fail-safe: na dúvida, CLIENTE
  [FIX] WEB_HOST / EXPLORER_HOST configuráveis (default 127.0.0.1)
  [FIX] 2º Ctrl+C força os._exit(1)
  [FIX] _verify_chain tri-state: OK / INVALID / ERROR
  [FIX] _graceful_shutdown com timeout por serviço
  [FIX] Audit de boot registrado DEPOIS do genesis
  [FIX] Status loop loga erros (não engole)
  [FIX] Miner wallet em miner_wallet.enc via secure_store
  [FIX] SIGHUP tratado
  [NEW] threading.excepthook + _thread_wrap
  [NEW] _check_required_modules() no boot
  [NEW] Rendezvous discovery integrado (p2p.add_peer)
  [NEW] Relay E2E (p2p_relay v2.0) com hooks opcionais
  [NEW] Mongo health no status loop
  [NEW] AUTO_MINE opt-in (BRN_AUTO_MINE=1)
================================================================
"""
from __future__ import annotations

import os
import sys
import signal
import argparse
import threading
import time
import json
import urllib.request
from pathlib import Path


# ============================================================
# BASE_DIR — definido ANTES de qualquer import do projeto
# ============================================================
BASE_DIR = Path(__file__).resolve().parent


def _bootstrap_env() -> None:
    """Lê brn_network.env relativo ao MÓDULO (não ao CWD)."""
    env_file = BASE_DIR / "brn_network.env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as e:
        print(f"[boot] aviso: falha lendo {env_file}: {e}")


_bootstrap_env()


# ============================================================
# IMPORTS APÓS BOOTSTRAP
# ============================================================
from brn_config import Config
from brn_logger import setup_logger, get_logger
from version import VERSION, BUILD_DATE, GITHUB_USER, GITHUB_REPO

_shutdown = threading.Event()

NODE_ID_PATH        = BASE_DIR / "node_identity.enc"
CLIENT_BOOT_TIMEOUT = int(os.environ.get("BRN_CLIENT_BOOT_TIMEOUT", "120"))

# Hosts configuráveis (default seguro)
WEB_HOST      = os.environ.get("BRN_WEB_HOST",      "127.0.0.1")
EXPLORER_HOST = os.environ.get("BRN_EXPLORER_HOST", "127.0.0.1")

# Auto-mine opt-in
AUTO_MINE     = os.environ.get("BRN_AUTO_MINE", "0") == "1"

# Tracking de serviços para shutdown limpo
_running_services: list = []
_services_lock = threading.Lock()


def _register_service(name: str, svc) -> None:
    with _services_lock:
        _running_services.append((name, svc))


# ============================================================
# THREAD EXCEPTHOOK
# ============================================================
def _thread_excepthook(args):
    try:
        log = get_logger("thread")
        log.error(
            f"Thread '{args.thread.name if args.thread else '?'}' morreu: "
            f"{args.exc_value}",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
    except Exception:
        pass


threading.excepthook = _thread_excepthook


def _thread_wrap(name: str, fn):
    def runner():
        try:
            fn()
        except Exception as e:
            try:
                get_logger(name).exception(f"thread {name} caiu: {e}")
            except Exception:
                pass
    return runner


# ============================================================
# CHECAGEM DE MÓDULOS OBRIGATÓRIOS
# ============================================================
def _check_required_modules() -> list[str]:
    required = [
        "brn_config", "brn_logger", "version",
        "blockchain", "db", "crypto", "wallet",
        "p2p_auth", "p2p_unified", "discovery_v2",
    ]
    missing = []
    for m in required:
        try:
            __import__(m)
        except Exception:
            missing.append(m)
    return missing


# ============================================================
# L2 IMPORTS — fallback que NUNCA quebra o nó
# ============================================================
L2_ENABLED = False
btc_config = None
BTCWatcher = None
L2Manager = None

try:
    import btc_config
    from btc_watcher import BTCWatcher
    from l2_manager import L2Manager
    L2_ENABLED = True
    print("[L2] Módulos BTC carregados OK")
except Exception as _e:
    print(f"[AVISO] btc_watcher/l2_manager indisponível: {_e}")
    print("[AVISO] Ponte BTC e L2 DESATIVADAS — o nó continua normal.")


def _l2_disponivel() -> bool:
    return (L2_ENABLED and btc_config is not None
            and BTCWatcher is not None and L2Manager is not None)


# ============================================================
# AUDIT
# ============================================================
def _audit(event: str, data: dict | None = None) -> None:
    try:
        from security import audit_log
        audit_log(event, data or {})
    except Exception:
        pass


def _close_audit() -> None:
    try:
        from security import close_audit
        close_audit()
    except Exception:
        pass


# ============================================================
# ARGS
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(prog="main.py", description="BRN Node v11")
    # diagnósticos
    p.add_argument("--status", action="store_true")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--version", action="store_true")
    p.add_argument("--check-update", action="store_true")
    p.add_argument("--config", default="config.json")
    p.add_argument("--log-level", default=None)
    p.add_argument("--log-file", default=None)
    p.add_argument("--password", default=None,
                   help="(INSEGURO — visível em ps) prefira --password-file")
    p.add_argument("--password-file", default=None)
    p.add_argument("--rotate-node-id", action="store_true")
    p.add_argument("--client-mode", action="store_true")
    p.add_argument("--discover", action="store_true")
    # L2
    p.add_argument("--l2", action="store_true")
    p.add_argument("--l2-stats", action="store_true")
    # mineração
    p.add_argument("--mine", action="store_true")
    # P2P
    p.add_argument("--no-p2p", action="store_true")
    p.add_argument("--relay", dest="relay", action="store_true", default=None,
                   help="Força relay ligado")
    p.add_argument("--no-relay", dest="relay", action="store_false",
                   help="Força relay desligado")
    # verify
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--skip-verify", action="store_true")
    return p.parse_args()


# ============================================================
# SENHA / IDENTIDADE
# ============================================================
def _resolve_password(args) -> str:
    if args.password_file:
        p = Path(args.password_file)
        if not p.exists():
            raise SystemExit(f"--password-file não encontrado: {p}")
        return p.read_text(encoding="utf-8").rstrip("\r\n")
    if args.password:
        raise SystemExit(
            "[!] --password é inseguro (visível em `ps`).\n"
            "    Use --password-file, BRN_NODE_PASSWORD, ou rode interativo."
        )
    env = os.environ.get("BRN_NODE_PASSWORD")
    if env:
        return env
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("Senha do nó (identidade Ed25519): ")
    raise SystemExit("Senha do nó não informada.")


def load_or_create_node_identity(password: str, rotate: bool = False):
    """
    ATENÇÃO: assinatura de secure_store é (data, password, path) para save,
    e (password, path) para load.
    """
    from crypto import Ed25519PrivateKey
    from secure_store import save_wallet, load_wallet

    log = get_logger("identity")

    if rotate and NODE_ID_PATH.exists():
        backup = NODE_ID_PATH.with_suffix(".enc.bak")
        NODE_ID_PATH.replace(backup)
        log.warning(f"Identidade rotacionada. Backup em {backup}")

    if NODE_ID_PATH.exists():
        data = load_wallet(password, str(NODE_ID_PATH))
        if not data:
            raise SystemExit(f"Falha ao decifrar {NODE_ID_PATH}")
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
        return sk, data["pub"]

    sk = Ed25519PrivateKey.generate()
    pub_hex = sk.public_key().public_bytes_raw().hex()
    save_wallet(
        {"sk": sk.private_bytes_raw().hex(), "pub": pub_hex},
        password, str(NODE_ID_PATH),
    )
    log.info(f"Identidade nova criada: {pub_hex[:16]}... ({NODE_ID_PATH})")
    return sk, pub_hex


# ============================================================
# PAPEL DO NÓ — fail-safe: na dúvida, CLIENTE
# ============================================================
def _read_role() -> str:
    try:
        p = BASE_DIR / "brn_role.txt"
        if p.exists():
            return p.read_text(encoding="utf-8").strip().lower()
    except Exception:
        pass
    return "auto"


def _is_origin(client_mode: bool) -> bool:
    if client_mode:
        return False
    if os.environ.get("BRN_IS_ORIGIN", "0") == "1":
        return True
    return _read_role() in ("origin", "hub", "servidor")


# ============================================================
# VERIFY — tri-state
# ============================================================
VERIFY_OK      = "ok"
VERIFY_INVALID = "invalid"
VERIFY_ERROR   = "error"


def _verify_chain(chain) -> str:
    try:
        from chain_validator import verify_chain
    except ImportError:
        get_logger("verify").info("chain_validator.py não disponível")
        return VERIFY_ERROR

    log = get_logger("verify")
    log.info("Verificando integridade da cadeia...")
    t0 = time.time()
    try:
        r = verify_chain(chain)
    except Exception as e:
        log.exception(f"validador falhou: {e}")
        return VERIFY_ERROR

    dt = time.time() - t0
    if r.valid:
        log.info(f"✓ {r.summary()} ({dt:.2f}s, {r.blocks_checked} blocos, "
                 f"{r.checkpoints_checked} checkpoints)")
        for w in (r.warnings or [])[:5]:
            log.warning(f"  {w}")
        return VERIFY_OK

    log.error(f"✗ CADEIA INVÁLIDA ({len(r.errors)} erros)")
    for e in (r.errors or [])[:10]:
        log.error(f"  - {e}")
    return VERIFY_INVALID


# ============================================================
# VERSION / UPDATE
# ============================================================
def _parse_version(s):
    try:
        return tuple(int(p) for p in s.strip().lstrip("v").split(".")[:3])
    except Exception:
        return (0, 0, 0)


def check_for_update(timeout: int = 5):
    url = (f"https://raw.githubusercontent.com/"
           f"{GITHUB_USER}/{GITHUB_REPO}/main/version.json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-node"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        remote = data.get("version", "0.0.0")
        return {
            "ok": True, "local": VERSION, "remote": remote,
            "update_available": _parse_version(remote) > _parse_version(VERSION),
            "url": data.get("url", ""), "notes": data.get("notes", ""),
        }
    except Exception as e:
        return {"ok": False, "local": VERSION, "error": str(e)}


# ============================================================
# DIAGNÓSTICO
# ============================================================
def do_status(cfg):
    print("=" * 64)
    print(f" BRN Node — Diagnóstico (v{VERSION})")
    print("=" * 64)
    print(f" Versão : {VERSION} ({BUILD_DATE})")
    print(f" Config : {cfg.source}")
    print(f" Web    : {WEB_HOST}:{cfg['web_port']}")
    print(f" Expl.  : {EXPLORER_HOST}:{cfg['explorer_port']}")
    print(f" P2P    : {cfg['p2p_port']}")
    print()
    https = os.environ.get("BRN_HTTPS", "0") == "1"
    audit = os.environ.get("BRN_AUDIT_ENABLED", "1") == "1"
    rdv   = os.environ.get("BRN_RENDEZVOUS_URL", "").strip()
    print(f" HTTPS       : {'SIM' if https else 'não'}")
    print(f"   Audit log   : {'SIM' : if audit else 'não'}")
    print(f" Rendezvous {  : {rdv or '(nhão configurado)'}")
    print(f"}")
 Auto-mine   : {'SIM' if AUTO_M           INE else 'não'}")
    print()

    missing = _check_required_modules()
    if missing:
        print(f" ⚠️  Módulos faltando: {', '.join(missing)}")
    else:
        print(" Módulos     : OK")
    print()

    if _l2_disponivel():
        try:
            print(" L2 Enabled  : SIM")
            print(f" BTC Addr    : {getattr(btc_config, 'BTC_RECEIVE_ADDRESS', '?')}")
        except Exception as e:
            print(f" L2 Enabled  : SIM (erro: {e})")
    else:
        print(" L2 Enabled  : NÃO")

    try:
        from checkpoints import load_checkpoints
        cps = load_checkpoints()
        print(f" Checkpoints : {len(cps)} carregados")
    except Exception as e:
        print(f" Checkpoints : indisponível ({e})")

    print(f" Role (auto) : {'ORIGEM' if _is_origin(False) else 'CLIENTE'}")

    db_path = cfg["db_path"]
    print(f" DB path     : {db_path}")
    if os.path.exists(db_path):
        print(f" DB size     : {os.path.getsize(db_path)/1024:.1f} KB")
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            h = conn.execute("SELECT MAX(height) FROM blocks").fetchone()[0]
            print(f" DB height conn.close()
        except Exception as e:
            print(f" DB height   : erro ({e})")

    # Mongo health (best-effort)
    try:
        from mongo_client import mongo
        ok, err = mongo.ping()
        print(f" MongoDB     : {'OK' if ok else f'off ({err or \"não configurado\"})'}")
    except Exception:
        print(" MongoDB     : pymongo indisponível")

    print("=" * 64)


def _do_discover_diagnostic(cfg):
    print("=" * 64)
    print(" BRN Discover Diagnostic")
    print("=" * 64)
    try:
        from discovery_v2 import UDPDiscovery, get_all_local_ips
    except ImportError as e:
        print(f" discovery_v2 indisponível: {e}")
        return
    print(f"\n IPs locais: {get_all_local_ips()}")
    print("\n Escutando 15s...")
    found = []

    def on_peer(ip, port):
        found.append(f"{ip}:{port}")
        print(f" [achou] {ip}:{port}")

    stop = [False]
    try:
        udp = UDPDiscovery(cfg["p2p_port"], "diag", on_peer, lambda: not stop[0])
        udp.start()
        time.sleep(15)
        stop[0] = True
        udp.stop()
    except Exception as e:
        print(f" falha: {e}")
    print(f"\n Resultado: {len(found)} peer(s)")
    print("=" * 64)


# ============================================================
# THREADS DE SERVIÇO
# ============================================================
def run_http():
    """server.py — backend da carteira (endpoints destrutivos)."""
    from server import app as http_app
    log = get_logger("http")
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    ssl_ctx = None
    if os.environ.get("BRN_HTTPS", "0") == "1":
        try:
            from security import ensure_self_signed_cert
            cert, key = ensure_self_signed_cert()
            ssl_ctx = (cert, key)
            log.info(f"HTTPS https://{WEB_HOST}:{port}")
        except Exception as e:
            log.warning(f"Falha ao gerar cert HTTPS: {e}. Caindo para HTTP.")
            ssl_ctx = None
    if ssl_ctx is None:
        log.info(f"HTTP http://{WEB_HOST}:{port}")
    http_app.run(
        host=WEB_HOST, port=port, threaded=True,
        debug=False, use_reloader=False, ssl_context=ssl_ctx,
    )


def run_explorer():
    """explorer.py — API pública de consulta."""
    from explorer import app as explorer_app
    log = get_logger("explorer")
    port = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
    ssl_ctx = None
    if os.environ.get("BRN_HTTPS", "0") == "1":
        try:
            from security import ensure_self_signed_cert
            ssl_ctx = ensure_self_signed_cert()
        except Exception:
            ssl_ctx = None
    log.info(f"Explorer {'https' if ssl_ctx else 'http'}://"
             f"{EXPLORER_HOST}:{port}")
    explorer_app.run(
        host=EXPLORER_HOST, port=port, threaded=True,
        debug=False, use_reloader=False, ssl_context=ssl_ctx,
    )


def run_status_loop(chain, p2p, l2_manager=None):
    log = get_logger("status")
    if p2p is None:
        log.warning("P2P desabilitado — status loop sem peers")

    # Mongo: pega handle uma vez
    mongo = None
    try:
        from mongo_client import mongo as _m
        mongo = _m
    except Exception:
        pass

    while not _shutdown.is_set():
        time.sleep(30)
        try:
            peer_count = 0
            relay_routes = 0
            if p2p is not None:
                try:
                    st = p2p.get_status()
                    peer_count = st.get("peer_count", 0)
                    relay = getattr(p2p, "relay", None)
                    if relay:
                        relay_routes = len(relay.routes)
                except Exception as e:
                    log.warning(f"p2p.get_status: {e}")

            msg = (f"Altura={chain.db.height()} "
                   f"Peers={peer_count} "
                   f"Relay={relay_routes} "
                   f"Mempool={len(chain.db.all_mempool(limit=1000))} "
                   f"UTXOs={chain.db.count_utxos()}")

            if l2_manager is not None:
                try:
                    s = l2_manager.stats()
                    msg += (f" L2[OPEN={s.get('open', 0)} "
                            f"DETECTED={s.get('btc_detected', 0)} "
                            f"RELEASED={s.get('released', 0)}]")
                except Exception:
                    pass

            if mongo is not None and mongo.uri:
                ok, err = mongo.ping()
                msg += f" Mongo={'OK' if ok else 'off'}"

            log.info(msg)
        except Exception as e:
            log.warning(f"status loop: {e}")


def run_wallet_main_thread():
    """UI nativa (pywebview) — só roda em modo GUI."""
    try:
        if not os.environ.get("BRN_WEB_PASS"):
            get_logger("wallet").error("BRN_WEB_PASS não definida")
            return
        from app_wallet_v3 import WalletApi
        import webview
        log = get_logger("wallet")
        index_path = BASE_DIR / "index_wallet.html"
        if not index_path.exists():
            log.error(f"index_wallet.html não encontrado: {index_path}")
            return
        api = WalletApi()
        webview.create_window(
            "BRN RWA — Carteira Digital",
            url=index_path.resolve().as_uri(),
            js_api=api,
            width=1020, height=880,
            min_size=(820, 640),
            background_color="#0d1117",
        )
        webview.start(debug=False)
    except Exception as e:
        get_logger("wallet").error(f"Falha: {e}")


# ============================================================
# RELAY SETUP
# ============================================================
def _setup_relay(p2p, node_id_priv, enabled):
    """Integra p2p_relay se: (a) habilitado, (b) módulos disponíveis."""
    if enabled is False:
        return None
    if p2p is None:
        return None

    try:
        from p2p_relay import RelayManager
    except ImportError as e:
        get_logger("relay").info(f"p2p_relay indisponível: {e}")
        return None

    try:
        relay = RelayManager(p2p, node_id_priv)
    except Exception as e:
        get_logger("relay").warning(f"RelayManager falhou: {e}")
        return None

    srv = getattr(p2p, "server", None)
    if srv is None:
        return relay

    def _caps_handler(ip, peer_id, msg):
        if not peer_id:
            return
        verified = False
        try:
            from p2p_auth import verify_auth, auth_enabled
            if auth_enabled():
                a = msg.get("_auth")
                if a:
                    payload_sem = {k: v for k, v in msg.items() if k != "_auth"}
                    ok, _ = verify_auth(a, msg=payload_sem)
                    verified = ok
        except Exception:
            pass
        relay.register_capability(
            peer_id,
            {"can_relay": msg.get("can_relay", False),
             "bandwidth_tier": msg.get("bandwidth_tier", "unknown")},
            verified=verified,
        )

    if hasattr(srv, "on_relay_caps"):
        srv.on_relay_caps = _caps_handler
    if hasattr(srv, "on_relay_request"):
        srv.on_relay_request = relay.handle_relay_request
    if hasattr(srv, "on_relay_incoming"):
        srv.on_relay_incoming = relay.handle_incoming_relay
    if hasattr(srv, "on_relay_accepted"):
        srv.on_relay_accepted = lambda ip, pid, msg: relay.handle_route_accepted(msg)
    if hasattr(srv, "on_relay_data"):
        srv.on_relay_data = relay.handle_relay_data

    return relay


# ============================================================
# SHUTDOWN
# ============================================================
def _stop_with_timeout(name: str, svc, timeout: float = 5.0, log=None) -> None:
    try:
        if hasattr(svc, "stop"):
            fn = svc.stop
        elif callable(svc):
            fn = svc
        else:
            return
    except Exception:
        return

    t = threading.Thread(target=fn, daemon=True, name=f"stop-{name}")
    t.start()
    t.join(timeout)
    if t.is_alive() and log is not None:
        log.warning(f"Serviço {name} não parou em {timeout}s")


def _graceful_shutdown(log) -> None:
    log.info("Encerrando serviços...")
    with _services_lock:
        services = list(_running_services)

    # Para em ordem reversa (últimos a subir = primeiros a parar)
    for name, svc in reversed(services):
        _stop_with_timeout(name, svc, timeout=5.0, log=log)

    # Tranca a sessão da carteira
    try:
        from wallet import WalletManager
        WalletManager.lock_session()
    except Exception:
        pass

    # Fecha Mongo
    try:
        from mongo_client import mongo
        mongo.close()
    except Exception:
        pass

    _audit("node_stop", {})
    _close_audit()


# ============================================================
# MAIN
# ============================================================
def main():
    args = parse_args()
    cfg = Config(args.config)

    if args.read_only: cfg.data["read_only"] = True
    if args.headless:  cfg.data["headless"]  = True
    if args.log_level: cfg.data["log_level"] = args.log_level
    if args.log_file:  cfg.data["log_file"]  = args.log_file
    cfg.apply_to_env()

    log = setup_logger(level=cfg["log_level"],
                       log_file=cfg["log_file"] or None)

    is_origin = _is_origin(args.client_mode)

    log.info("=" * 60)
    log.info(f" BRN Node v{VERSION} ({BUILD_DATE})")
    l2_mode = "ON" if (args.l2 and _l2_disponivel()) else (
              "off" if not args.l2 else "INDISPONÍVEL")
    log.info(f" Config: {cfg.source} | "
             f"Modo: {'ORIGEM' if is_origin else 'CLIENTE'} | L2: {l2_mode}")
    log.info(f" HTTPS: {'ON' if os.environ.get('BRN_HTTPS') == '1' else 'off'} | "
             f"Rendezvous: {os.environ.get('BRN_RENDEZVOUS_URL', '—')} | "
             f"Auto-mine: {'ON' if AUTO_MINE else 'off'}")
    log.info("=" * 60)

    # ---- comandos que saem sozinhos ----
    if args.version:
        print(VERSION); return 0
    if args.status:
        do_status(cfg); return 0
    if args.discover:
        _do_discover_diagnostic(cfg); return 0
    if args.check_update:
        print(check_for_update()); return 0
    if args.l2_stats:
        if not _l2_disponivel():
            print("L2 não disponível"); return 0
        try:
            from db import ChainDB
            db = ChainDB(cfg["db_path"])
            print(json.dumps(L2Manager(db).stats(), indent=2))
        except Exception as e:
            print(f"Erro: {e}")
        return 0

    # ---- módulos obrigatórios ----
    missing = _check_required_modules()
    if missing:
        log.error(f"Módulos obrigatórios faltando: {', '.join(missing)}")
        return 1

    # ---- identidade ----
    node_password = _resolve_password(args)
    try:
        node_id_priv, node_id_pub = load_or_create_node_identity(
            node_password, rotate=args.rotate_node_id)
    except SystemExit:
        raise
    log.info(f"Nó ID: {node_id_pub}")

    # ---- blockchain ----
    from blockchain import Blockchain
    chain = Blockchain(cfg["db_path"], auto_genesis=not args.client_mode)
    log.info(f"Altura atual: {chain.db.height()}")

    # ---- checkpoints ----
    try:
        from checkpoints import load_checkpoints
        from blockchain import set_checkpoints, set_checkpoint_priv
        cps = load_checkpoints()
        set_checkpoints(cps)
        if is_origin and node_id_priv is not None:
            set_checkpoint_priv(node_id_priv)
            log.info(f"Checkpoints: {len(cps)} carregados (assinatura ATIVA)")
        else:
            log.info(f"Checkpoints: {len(cps)} carregados (só valida)")
    except Exception as e:
        log.warning(f"Checkpoints desativados: {e}")

    # ---- verify ----
    if args.verify_only:
        st = _verify_chain(chain)
        try: chain.db.close()
        except Exception: pass
        return 0 if st == VERIFY_OK else 2

    if not args.skip_verify and os.environ.get("BRN_SKIP_VERIFY", "0") != "1":
        strict = os.environ.get("BRN_VERIFY_STRICT", "0") == "1"
        st = _verify_chain(chain)
        if st == VERIFY_INVALID and strict:
            log.error("Abortando boot: cadeia inválida (BRN_VERIFY_STRICT=1)")
            try: chain.db.close()
            except Exception: pass
            return 2
        if st == VERIFY_ERROR:
            log.warning("Verificação não conclusiva — seguindo com cautela")

    # ---- P2P ----
    p2p = None
    if not args.no_p2p:
        try:
            from p2p_unified import P2PManager
            p2p = P2PManager(chain, node_id_priv,
                             tcp_port=cfg["p2p_port"],
                             enable_upnp=cfg["upnp"])
            p2p.start()
            _register_service("p2p", p2p)
            log.info("P2P iniciado")
        except Exception as e:
            log.error(f"Falha ao iniciar P2P: {e}")
            p2p = None

    # ---- Relay ----
    relay = None
    if p2p is not None:
        relay = _setup_relay(p2p, node_id_priv, args.relay)
        if relay:
            try:
                p2p.relay = relay
            except Exception:
                pass
            _register_service("relay", relay)
            log.info("Relay E2E pronto")

    # ---- Rendezvous ----
    rdv = None
    rdv_url = os.environ.get("BRN_RENDEZVOUS_URL", "").strip()
    if rdv_url and p2p is not None:
        try:
            from rendezvous_discovery import RendezvousDiscovery
            rdv = RendezvousDiscovery(
                tcp_port=cfg["p2p_port"],
                node_uuid=node_id_pub,
                on_peer=p2p.add_peer,
                running_flag=lambda: not _shutdown.is_set(),
                sign_fn=node_id_priv.sign,
            )
            rdv.start()
            _register_service("rendezvous", rdv)
            log.info(f"Rendezvous → {rdv_url}")
        except Exception as e:
            log.warning(f"Rendezvous falhou: {e}")

    # ---- L2 ----
    l2_manager = None
    btc_watcher = None
    l2_ativo = False

    if _l2_disponivel():
        try:
            l2_manager = L2Manager(chain.db, chain)
            l2_ativo = True
            log.info("L2Manager carregado")
        except Exception as e:
            log.warning(f"L2Manager falhou: {e}")

    if args.l2 and l2_ativo:
        try:
            btc_addr = getattr(btc_config, "BTC_RECEIVE_ADDRESS", "")
            if not btc_addr or btc_addr == "bc1qSEU_ENDERECO_AQUI_TROQUE_ISSO":
                log.error("Configure BTC_RECEIVE_ADDRESS em btc_config.py")
            else:
                btc_watcher = BTCWatcher(chain.db, chain)
                btc_watcher.start()
                _register_service("btc_watcher", btc_watcher)
                log.info(f"L2 Watcher RPC iniciado em {btc_addr}")
        except Exception as e:
            log.warning(f"Falha ao iniciar BTCWatcher: {e}")
            btc_watcher = None
    elif args.l2:
        log.warning("--l2 solicitado, mas L2 não disponível. Ignorando.")

    # ---- HTTP + Explorer ----
    threading.Thread(target=_thread_wrap("HTTP", run_http),
                     daemon=True, name="HTTP").start()
    threading.Thread(target=_thread_wrap("Explorer", run_explorer),
                     daemon=True, name="Explorer").start()

    # ---- Miner ----
    if args.mine or AUTO_MINE:
        try:
            from miner_loop import get_miner
            from wallet import Wallet
            from secure_store import save_wallet, load_wallet

            miner = get_miner(chain)
            miner_wallet_path = BASE_DIR / "miner_wallet.enc"

            mw = None
            try:
                data = load_wallet(node_password, str(miner_wallet_path))
                if data and hasattr(Wallet, "from_dict"):
                    mw = Wallet.from_dict(data)
            except Exception:
                mw = None

            if mw is None:
                mw = Wallet.create()
                try:
                    if hasattr(mw, "to_dict"):
                        save_wallet(mw.to_dict(), node_password,
                                    str(miner_wallet_path))
                        log.info(f"Miner wallet criada em {miner_wallet_path}")
                    else:
                        log.warning("Wallet sem to_dict() — não salvei (inseguro)")
                except Exception as e:
                    log.warning(f"Falha ao salvar miner wallet: {e}")

            miner.start(mw.address, mw.pubkey_hex)
            _register_service("miner", miner)
            log.info(f"Miner iniciado com {mw.address}")
        except Exception as e:
            log.warning(f"Miner falhou: {e}")

    # ---- Status loop ----
    threading.Thread(
        target=_thread_wrap(
            "StatusLoop",
            lambda: run_status_loop(chain, p2p, l2_manager),
        ),
        daemon=True, name="StatusLoop",
    ).start()

    # ---- Cliente aguarda genesis ----
    if args.client_mode and chain.db.height() < 0:
        log.info(f"Aguardando genesis (timeout {CLIENT_BOOT_TIMEOUT}s)...")
        t0 = time.time()
        while chain.db.height() < 0 and (time.time() - t0) < CLIENT_BOOT_TIMEOUT:
            if _shutdown.is_set():
                break
            time.sleep(1)

    # ---- AUDIT: só depois do genesis ----
    _audit("node_start", {
        "mode":       "origin" if is_origin else "client",
        "l2":         bool(args.l2 and l2_ativo),
        "mine":       bool(args.mine or AUTO_MINE),
        "https":      os.environ.get("BRN_HTTPS") == "1",
        "height":     chain.db.height(),
        "node_id":    node_id_pub[:16],
        "rendezvous": bool(rdv),
        "relay":      bool(relay),
    })

    log.info("Nó pronto. Ctrl+C para encerrar.")

    # ---- UI / loop principal ----
    try:
        if not cfg["headless"]:
            run_wallet_main_thread()
        else:
            while not _shutdown.is_set():
                time.sleep(1)
    except KeyboardInterrupt:
        pass

    # ---- shutdown ----
    log.info("Encerrando...")
    _shutdown.set()
    _graceful_shutdown(log)

    try:
        chain.db.close()
    except Exception:
        pass

    # best-effort: limpa senha
    try:
        del node_password
    except Exception:
        pass

    return 0


# ============================================================
# SIGNAL HANDLERS — 2º Ctrl+C força saída
# ============================================================
_signal_count = 0


def _on_signal(signum, frame):
    global _signal_count
    _signal_count += 1
    if _signal_count >= 2:
        print("\n[!] Forçando saída.", file=sys.stderr)
        os._exit(1)
    _shutdown.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _on_signal)

    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        try:
            log = get_logger("main")
            log.exception(f"Erro fatal: {e}")
            try:
                _graceful_shutdown(log)
            except Exception:
                pass
        except Exception:
            print(f"Erro fatal: {e}", file=sys.stderr)
        sys.exit(1)