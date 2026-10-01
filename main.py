"""
main.py — Entrypoint unificado do nó BRN (v8.1 L2 MINERADO)

v8.1 L2 MINERADO:
    - Merge v6.1 + L2 BTC->BRN validado por mineração
    - --l2: ativa watcher BTC RPC (só confirma, validação por PoW)
    - --mine: ativa mineração que prioriza TXs L2
    - Watcher envia para mempool, blockchain só libera após bloco minerado
    - btc_config.BTC_RECEIVE_ADDRESS = seu bc1q...

v6.1:
    - --client-mode: cliente NAO origina genesis, espera da rede.
    - Blockchain recebe auto_genesis=not args.client_mode.
    - Espera de sincronizacao no boot do cliente (timeout configuravel).
    - --discover: diagnostico de descoberta de peers.
    - Aviso se BRN_NETWORK_SECRET estiver no default.

"""
import os
import sys
import signal
import argparse
import hashlib
import threading
import time
import json
import socket
import urllib.request
import urllib.error
from pathlib import Path

from brn_config import Config
from brn_logger import setup_logger, get_logger
from version import VERSION, BUILD_DATE, GITHUB_USER, GITHUB_REPO

_shutdown = threading.Event()

BASE_DIR = Path(__file__).parent.resolve()
NODE_ID_PATH = BASE_DIR / "node_identity.enc"

CLIENT_BOOT_TIMEOUT = int(os.environ.get("BRN_CLIENT_BOOT_TIMEOUT", "120"))

# L2 IMPORTS
L2_ENABLED = False
try:
    import btc_config
    from btc_watcher import BTCWatcher
    from l2_manager import L2Manager
    L2_ENABLED = True
except ImportError as e:
    btc_config = None
    BTCWatcher = None
    L2Manager = None

def parse_args():
    p = argparse.ArgumentParser(prog="main.py", description="BRN Node v8.1 L2 Minerado")
    p.add_argument("--status", action="store_true", help="Diagnostico completo e sai")
    p.add_argument("--headless", action="store_true", help="Sem interface grafica")
    p.add_argument("--read-only", action="store_true", help="Nao minera nem publica")
    p.add_argument("--version", action="store_true", help="Mostra versao e sai")
    p.add_argument("--check-update", action="store_true", help="Verifica versao nova")
    p.add_argument("--config", default="config.json", help="Arquivo de configuracao")
    p.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    p.add_argument("--log-file", default=None, help="Log JSON neste arquivo")
    p.add_argument("--password", default=None, help="Senha do nó")
    p.add_argument("--password-file", default=None, help="Le a senha do nó deste arquivo")
    p.add_argument("--rotate-node-id", action="store_true", help="Gera nova identidade")
    p.add_argument("--client-mode", action="store_true", help="Nao origina genesis")
    p.add_argument("--discover", action="store_true", help="Diagnostico de peers")
    p.add_argument("--l2", action="store_true", help="Ativa watcher BTC->BRN (RPC-only)")
    p.add_argument("--mine", action="store_true", help="Ativa mineracao (prioriza L2)")
    p.add_argument("--l2-stats", action="store_true", help="Mostra stats L2 e sai")
    return p.parse_args()

def _resolve_password(args) -> str:
    if args.password_file:
        p = Path(args.password_file)
        if not p.exists():
            raise SystemExit(f"--password-file nao encontrado: {p}")
        return p.read_text(encoding="utf-8").rstrip("\r\n")
    if args.password:
        return args.password
    env = os.environ.get("BRN_NODE_PASSWORD")
    if env:
        return env
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("Senha do no (identidade Ed25519): ")
    raise SystemExit("Senha do no nao informada.")

def load_or_create_node_identity(password: str, rotate: bool = False):
    from crypto import Ed25519PrivateKey
    from secure_store import save_wallet, load_wallet
    log = get_logger("identity")
    if rotate and NODE_ID_PATH.exists():
        backup = NODE_ID_PATH.with_suffix(".enc.bak")
        NODE_ID_PATH.replace(backup)
        log.warning(f"Identidade rotacionada. Backup em {backup}")
    if NODE_ID_PATH.exists():
        try:
            data = load_wallet(str(NODE_ID_PATH), password)
        except Exception as e:
            raise SystemExit(f"Falha ao decifrar {NODE_ID_PATH}: {e}")
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
        pub_hex = data["pub"]
        log.info(f"Identidade carregada: {pub_hex[:16]}...")
        return sk, pub_hex
    sk = Ed25519PrivateKey.generate()
    pub_hex = sk.public_key().public_bytes_raw().hex()
    save_wallet(str(NODE_ID_PATH), {"sk": sk.private_bytes_raw().hex(), "pub": pub_hex}, password)
    log.info(f"Identidade nova criada: {pub_hex[:16]}... ({NODE_ID_PATH})")
    return sk, pub_hex

def _parse_version(s):
    try:
        return tuple(int(p) for p in s.strip().lstrip("v").split(".")[:3])
    except Exception:
        return (0, 0, 0)

def check_for_update(timeout=5):
    url = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/version.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-node"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        remote = data.get("version", "0.0.0")
        return {"ok": True, "local": VERSION, "remote": remote, "update_available": _parse_version(remote) > _parse_version(VERSION), "url": data.get("url", f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}"), "notes": data.get("notes", "")}
    except Exception as e:
        return {"ok": False, "local": VERSION, "error": str(e)}

def _try_local_api(port, path="/api/status", timeout=2):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"User-Agent": "brn-node"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

def do_status(cfg):
    print("=" * 64)
    print(f" BRN Node — Diagnostico (v{VERSION})")
    print("=" * 64)
    print()
    print(f" Versao : {VERSION} ({BUILD_DATE})")
    print(f" Config : {cfg.source}")
    print(f" Web port : {cfg['web_port']}")
    print(f" Explorer : {cfg['explorer_port']}")
    print(f" P2P port : {cfg['p2p_port']}")
    print()
    if L2_ENABLED and btc_config:
        print(f" L2 Enabled : SIM")
        print(f" BTC Addr : {btc_config.BTC_RECEIVE_ADDRESS}")
        print(f" Rate : 1 BTC = {btc_config.BRN_PER_BTC} BRN")
    else:
        print(f" L2 Enabled : NAO")
    print()
    db_path = cfg["db_path"]
    print(f" DB path : {db_path}")
    if os.path.exists(db_path):
        print(f" DB size : {os.path.getsize(db_path)/1024:.1f} KB")
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            h = conn.execute("SELECT MAX(height) FROM blocks").fetchone()[0]
            try:
                l2_open = conn.execute("SELECT COUNT(*) FROM l2_escrows WHERE status='OPEN'").fetchone()[0]
                l2_detected = conn.execute("SELECT COUNT(*) FROM l2_escrows WHERE status='BTC_DETECTED'").fetchone()[0]
                l2_released = conn.execute("SELECT COUNT(*) FROM l2_escrows WHERE status='RELEASED'").fetchone()[0]
                print(f" DB height : {h}")
                print(f" L2 OPEN : {l2_open}")
                print(f" L2 DETECTED : {l2_detected} (na mempool, aguardando mineracao)")
                print(f" L2 RELEASED : {l2_released}")
            except:
                print(f" DB height : {h}")
            conn.close()
        except Exception as e:
            print(f" DB height : erro ({e})")
    print("=" * 64)

def _do_discover_diagnostic(cfg):
    print("=" * 64)
    print(" BRN Discover Diagnostic")
    print("=" * 64)
    try:
        from discovery_v2 import get_all_local_ips, _load_manual_peers
    except ImportError as e:
        print(f" discovery_v2 nao disponivel: {e}")
        return
    ips = get_all_local_ips()
    print(f"\n IPs locais: {ips}")
    print(f"\n Escutando 15s...")
    found = []
    def on_peer(ip, port):
        found.append(f"{ip}:{port}")
        print(f" [achou] {ip}:{port}")
    stop_flag = [False]
    try:
        from discovery_v2 import UDPDiscovery
        udp = UDPDiscovery(cfg["p2p_port"], "diag", on_peer, lambda: not stop_flag[0])
        udp.start()
        time.sleep(15)
        stop_flag[0] = True
        udp.stop()
    except Exception as e:
        print(f" falha: {e}")
    print(f"\n Resultado: {len(found)} peer(s)")
    print("=" * 64)

def run_http():
    from server import app as http_app
    log = get_logger("http")
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    log.info(f"HTTP http://0.0.0.0:{port}")
    http_app.run(host="0.0.0.0", port=port, threaded=True, debug=False, use_reloader=False)

def run_explorer():
    from explorer import app as explorer_app
    log = get_logger("explorer")
    port = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
    log.info(f"Explorer http://0.0.0.0:{port}")
    explorer_app.run(host="0.0.0.0", port=port, threaded=True, debug=False, use_reloader=False)

def run_status_loop(chain, p2p, l2_manager=None):
    log = get_logger("status")
    while not _shutdown.is_set():
        time.sleep(30)
        try:
            peers = p2p.get_status()
            msg = f"Altura={chain.db.height()} Peers={peers.get('peer_count', 0)} Mempool={len(chain.db.all_mempool(limit=1000))} UTXOs={chain.db.count_utxos()}"
            if l2_manager:
                stats = l2_manager.stats()
                msg += f" L2[OPEN={stats.get('open',0)} DETECTED={stats.get('btc_detected',0)} RELEASED={stats.get('released',0)}]"
            log.info(msg)
        except Exception:
            pass

def run_wallet_main_thread():
    try:
        if not os.environ.get("BRN_WEB_PASS"):
            get_logger("wallet").error("BRN_WEB_PASS nao definida")
            return
        from app_wallet_v3 import WalletApi
        import webview
        log = get_logger("wallet")
        index_path = BASE_DIR / "index_wallet.html"
        if not index_path.exists():
            log.error(f"index_wallet.html nao encontrado: {index_path}")
            return
        api = WalletApi()
        webview.create_window("BRN RWA - Carteira Digital", url=index_path.resolve().as_uri(), js_api=api, width=1020, height=880, min_size=(820, 640), background_color="#0d1117")
        webview.start(debug=False)
    except Exception as e:
        get_logger("wallet").error(f"Falha: {e}")

def main():
    args = parse_args()
    cfg = Config(args.config)
    if args.read_only:
        cfg.data["read_only"] = True
    if args.headless:
        cfg.data["headless"] = True
    if args.log_level:
        cfg.data["log_level"] = args.log_level
    if args.log_file:
        cfg.data["log_file"] = args.log_file
    cfg.apply_to_env()
    log = setup_logger(level=cfg["log_level"], log_file=cfg["log_file"] or None)
    log.info("=" * 60)
    log.info(f" BRN Node v{VERSION} ({BUILD_DATE}) + L2 MINERADO")
    log.info(f" Config: {cfg.source} Modo: {'CLIENTE' if args.client_mode else 'ORIGEM'} L2: {'ON' if args.l2 else 'off'}")
    log.info("=" * 60)

    if args.version:
        print(VERSION)
        return
    if args.status:
        do_status(cfg)
        return
    if args.discover:
        _do_discover_diagnostic(cfg)
        return
    if args.l2_stats:
        if not L2_ENABLED:
            print("L2 nao habilitado")
            return
        from db import ChainDB
        db = ChainDB(cfg["db_path"])
        mgr = L2Manager(db)
        print(json.dumps(mgr.stats(), indent=2))
        return
    if args.check_update:
        r = check_for_update()
        print(r)
        return

    node_password = _resolve_password(args)
    try:
        node_id_priv, node_id_pub = load_or_create_node_identity(node_password, rotate=args.rotate_node_id)
    finally:
        try:
            del node_password
        except Exception:
            pass
    log.info(f"No ID: {node_id_pub}")

    from blockchain import Blockchain
    from p2p_unified import P2PManager
    db_path = cfg["db_path"]
    chain = Blockchain(db_path, auto_genesis=not args.client_mode)
    log.info(f"Altura atual: {chain.db.height()}")

    p2p = P2PManager(chain, node_id_priv, tcp_port=cfg["p2p_port"], enable_upnp=cfg["upnp"])
    p2p.start()

    l2_manager = None
    btc_watcher = None
    if L2_ENABLED:
        l2_manager = L2Manager(chain.db, chain)
        if args.l2:
            if btc_config.BTC_RECEIVE_ADDRESS == "bc1qSEU_ENDERECO_AQUI_TROQUE_ISSO":
                log.error("Configure seu endereço BTC em btc_config.py!")
            else:
                btc_watcher = BTCWatcher(chain.db, chain)
                btc_watcher.start()
                log.info(f"L2 Watcher RPC iniciado em {btc_config.BTC_RECEIVE_ADDRESS}")

    threading.Thread(target=run_http, daemon=True, name="HTTP").start()
    threading.Thread(target=run_explorer, daemon=True, name="Explorer").start()

    if chain.db.height() >= 0:
        try:
            from miner_loop import get_miner
            miner = get_miner(chain)
            if args.mine:
                from wallet import Wallet
                try:
                    mw = Wallet.load("miner_wallet.json")
                except:
                    mw = Wallet.create()
                    mw.save("miner_wallet.json")
                miner.start(mw.address, mw.pubkey_hex)
                log.info(f"Miner CLI iniciado com {mw.address}")
        except Exception as e:
            log.error(f"Miner erro: {e}")

    threading.Thread(target=run_status_loop, args=(chain, p2p, l2_manager), daemon=True, name="StatusLoop").start()
    log.info("No pronto. Ctrl+C para encerrar.")
    if args.l2:
        log.info("L2 ATIVO: BTC RPC -> mempool -> PoW -> BRN liberado")

    if args.client_mode and chain.db.height() < 0:
        log.info(f"Aguardando genesis (timeout {CLIENT_BOOT_TIMEOUT}s)...")
        t0 = time.time()
        while chain.db.height() < 0 and (time.time() - t0) < CLIENT_BOOT_TIMEOUT:
            if _shutdown.is_set():
                break
            time.sleep(1)

    use_wallet = not cfg["headless"]
    if use_wallet:
        try:
            run_wallet_main_thread()
        except KeyboardInterrupt:
            pass
    else:
        try:
            while not _shutdown.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    log.info("Encerrando...")
    _shutdown.set()
    try:
        if btc_watcher:
            btc_watcher.stop()
    except:
        pass
    try:
        p2p.stop()
    except:
        pass
    try:
        chain.db.close()
    except:
        pass

def _on_signal(signum, frame):
    _shutdown.set()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    main()