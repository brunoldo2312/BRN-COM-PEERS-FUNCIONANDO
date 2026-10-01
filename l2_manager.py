# l2_manager.py - Gerencia L2 e salva só hash na L1
import time, hashlib, json

class L2Manager:
    def __init__(self, db, blockchain):
        self.db = db
        self.blockchain = blockchain

    def create_swap_order(self, buyer_wallet, btc_amount_btc):
        btc_sats = int(float(btc_amount_btc) * 100_000_000)
        from contracts.factory import ContractFactory
        factory = ContractFactory(self.db)
        return factory.create_btc_to_brn("BRN_TREASURY", btc_sats, buyer_wallet)

    def get_order_status(self, escrow_id):
        return self.db.get_l2_escrow_by_id(escrow_id)

    def list_orders(self, status=None):
        return self.db.get_l2_escrows(status=status) if status else self.db.get_l2_escrows()