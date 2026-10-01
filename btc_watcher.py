# btc_watcher.py - Watcher que detecta BTC no seu endereço e libera BRN
import time, requests, threading
import btc_config

class BTCWatcher(threading.Thread):
    def __init__(self, db, blockchain, l2_manager):
        super().__init__(daemon=True)
        self.db = db
        self.blockchain = blockchain
        self.l2_manager = l2_manager
        self.running = True

    def run(self):
        print("[BTC Watcher] Iniciado - observando:", btc_config.BTC_RECEIVE_ADDRESS)
        while self.running:
            try:
                self.check_all_escrows()
            except Exception as e:
                print(f"[BTC Watcher] Erro: {e}")
            time.sleep(30)

    def check_all_escrows(self):
        escrows = self.db.get_l2_escrows(status="OPEN")
        if not escrows: return
        btc_txs = self.fetch_btc_txs(btc_config.BTC_RECEIVE_ADDRESS)
        for escrow in escrows:
            if escrow['expires_at'] < time.time():
                self.db.update_l2_escrow_status(escrow['escrow_id'], "EXPIRED")
                continue
            for btc_tx in btc_txs:
                if self.match_tx(escrow, btc_tx):
                    self.confirm_and_release(escrow, btc_tx)

    def fetch_btc_txs(self, address):
        try:
            url = f"https://blockstream.info/api/address/{address}/txs"
            if btc_config.BTC_NETWORK == "testnet":
                url = f"https://blockstream.info/testnet/api/address/{address}/txs"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                txs = []
                for tx in data[:20]:
                    val = sum([vout['value'] for vout in tx['vout'] if vout['scriptpubkey_address'] == address])
                    txs.append({"txid": tx['txid'], "value_sats": val, "confirmations": tx.get('status', {}).get('confirmations', 0) or 0})
                return txs
        except: pass
        return []

    def match_tx(self, escrow, btc_tx):
        if self.db.is_btc_txid_used(btc_tx['txid']): return False
        if btc_tx['value_sats'] < escrow['btc_expected_sats'] * 0.99: return False
        if btc_tx['confirmations'] < btc_config.BTC_MIN_CONFIRMATIONS: return False
        return True

    def confirm_and_release(self, escrow, btc_tx):
        self.db.update_l2_escrow_status(escrow['escrow_id'], "BTC_DETECTED", btc_txid=btc_tx['txid'])
        self.db.mark_btc_txid_used(btc_tx['txid'], escrow['escrow_id'])
        tx_l1 = {
            "type": "l2_settlement", "subtype": "btc_to_brn",
            "escrow_id": escrow['escrow_id'], "buyer": escrow['buyer'],
            "brn_amount": escrow['brn_amount'], "btc_txid": btc_tx['txid'],
            "btc_address": escrow['btc_address'], "l2_hash": escrow['l2_hash'],
            "timestamp": int(time.time())
        }
        try:
            self.blockchain.add_l2_transaction(tx_l1)
            self.db.update_l2_escrow_status(escrow['escrow_id'], "RELEASED")
        except:
            self.db.update_l2_escrow_status(escrow['escrow_id'], "OPEN")