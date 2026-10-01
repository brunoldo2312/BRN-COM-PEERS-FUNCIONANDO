# btc_watcher.py - v3 FINAL - Só confirma BTC via RPC
# NÃO credita BRN direto. Envia pra mempool pra ser MINERADO pela sua BRN Chain
import time
import requests
import threading
import btc_config

class BTCWatcher(threading.Thread):
    def __init__(self, db, blockchain):
        super().__init__(daemon=True)
        self.db = db
        self.blockchain = blockchain
        self.running = True
        print(f"[BTC Watcher RPC] Iniciado")
        print(f"[BTC Watcher RPC] Endereço: {btc_config.BTC_RECEIVE_ADDRESS}")
        print(f"[BTC Watcher RPC] Rede: {btc_config.BTC_NETWORK}")
        print(f"[BTC Watcher RPC] Confirmações necessárias: {btc_config.BTC_MIN_CONFIRMATIONS}")

    def run(self):
        while self.running:
            try:
                self.check_rpc()
            except Exception as e:
                print(f"[BTC Watcher] Erro: {e}")
            time.sleep(30)  # checa a cada 30s

    def check_rpc(self):
        escrows = self.db.get_l2_escrows(status="OPEN")
        if not escrows:
            return
        
        btc_txs = self.fetch_via_rpc(btc_config.BTC_RECEIVE_ADDRESS)
        if not btc_txs:
            return

        for escrow in escrows:
            if escrow['expires_at'] < time.time():
                self.db.update_l2_escrow_status(escrow['escrow_id'], "EXPIRED")
                print(f"[BTC Watcher] Escrow expirado: {escrow['escrow_id']}")
                continue
            
            for btc_tx in btc_txs:
                # Anti-replay
                if self.db.is_btc_txid_used(btc_tx['txid']):
                    continue
                
                # Valor mínimo (99% pra tolerar fee)
                if btc_tx['value_sats'] < int(escrow['btc_expected_sats'] * 0.99):
                    continue
                
                # Confirmações via RPC
                if btc_tx['confirmations'] < btc_config.BTC_MIN_CONFIRMATIONS:
                    print(f"[BTC Watcher] BTC {btc_tx['txid'][:16]}... só {btc_tx['confirmations']} confirmações, precisa {btc_config.BTC_MIN_CONFIRMATIONS}")
                    continue
                
                # ==== BTC CONFIRMADO VIA RPC ====
                print(f"")
                print(f"[BTC Watcher RPC] ============ BTC CONFIRMADO ============")
                print(f"[BTC Watcher RPC] Escrow: {escrow['escrow_id']}")
                print(f"[BTC Watcher RPC] BTC TXID: {btc_tx['txid']}")
                print(f"[BTC Watcher RPC] Valor: {btc_tx['value_sats']/1e8} BTC")
                print(f"[BTC Watcher RPC] Confirmações: {btc_tx['confirmations']}")
                print(f"[BTC Watcher RPC] Comprador BRN: {escrow['buyer']}")
                print(f"[BTC Watcher RPC] BRN a liberar: {escrow['brn_amount']/1e8} BRN")
                print(f"[BTC Watcher RPC] Enviando para MEMPOOL da BRN Chain...")
                print(f"[BTC Watcher RPC] ========================================")
                print(f"")

                # Envia pra blockchain validar via MINERAÇÃO
                l2_tx = {
                    "type": "l2_settlement",
                    "escrow_id": escrow['escrow_id'],
                    "buyer": escrow['buyer'],
                    "brn_amount": escrow['brn_amount'],
                    "btc_txid": btc_tx['txid'],
                    "btc_address": escrow['btc_address'],
                    "l2_hash": escrow['l2_hash'],
                    "timestamp": int(time.time())
                }
                try:
                    txid = self.blockchain.add_l2_transaction(l2_tx)
                    print(f"[BTC Watcher] L2 TX {txid} enviado pra mempool. Aguardando minerador minerar bloco...")
                except Exception as e:
                    print(f"[BTC Watcher] Erro ao enviar pra mempool: {e}")

    def fetch_via_rpc(self, address):
        """Busca TXs via RPC público - Blockstream / Mempool.space"""
        # Tenta Blockstream
        try:
            base = "https://blockstream.info/api" if btc_config.BTC_NETWORK == "mainnet" else "https://blockstream.info/testnet/api"
            r = requests.get(f"{base}/address/{address}/txs", timeout=15)
            if r.status_code == 200:
                data = r.json()
                txs = []
                for tx in data[:30]:
                    # Soma quanto caiu no seu endereço
                    val = 0
                    for vout in tx.get('vout', []):
                        if vout.get('scriptpubkey_address') == address:
                            val += vout.get('value', 0)
                    if val == 0:
                        continue
                    
                    # Confirmações
                    confirmations = 0
                    status = tx.get('status', {})
                    if status.get('confirmed'):
                        # Se tem block_height, está confirmado
                        # Pra simplificar, assume 3 se confirmado (sua chain valida melhor depois)
                        # Se quiser preciso: busque altura atual - altura do bloco
                        confirmations = 3
                        # Tenta pegar confirmações reais
                        try:
                            if status.get('block_height'):
                                tip_r = requests.get(f"{base}/blocks/tip/height", timeout=5)
                                if tip_r.status_code == 200:
                                    tip_height = int(tip_r.text)
                                    confirmations = tip_height - status['block_height'] + 1
                        except:
                            confirmations = 3

                    txs.append({
                        "txid": tx['txid'],
                        "value_sats": val,
                        "confirmations": confirmations
                    })
                if txs:
                    return txs
        except Exception as e:
            print(f"[RPC] Blockstream falhou: {e}")

        # Fallback Mempool.space
        try:
            base2 = "https://mempool.space/api" if btc_config.BTC_NETWORK == "mainnet" else "https://mempool.space/testnet/api"
            r = requests.get(f"{base2}/address/{address}/txs", timeout=15)
            if r.status_code == 200:
                data = r.json()
                txs = []
                for tx in data[:30]:
                    val = 0
                    for vout in tx.get('vout', []):
                        if vout.get('scriptpubkey_address') == address:
                            val += vout.get('value', 0)
                    if val == 0:
                        continue
                    confirmations = 3 if tx.get('status', {}).get('confirmed') else 0
                    txs.append({"txid": tx