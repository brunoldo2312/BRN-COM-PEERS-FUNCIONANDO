# l2_manager.py - Gerencia ordens L2 BTC->BRN
import time
import btc_config
from contracts.factory import ContractFactory

class L2Manager:
    def __init__(self, db, blockchain=None):
        self.db = db
        self.blockchain = blockchain
        self.factory = ContractFactory(db)

    def create_order(self, btc_amount_sats, buyer_brn_address, creator_wallet="BRN_TREASURY"):
        """
        Cria ordem BTC->BRN
        btc_amount_sats: quanto BTC usuário vai mandar (ex: 100000 = 0.001 BTC)
        buyer_brn_address: carteira brn1q... que vai receber BRN
        """
        if btc_amount_sats < btc_config.MIN_BTC_SATS:
            raise Exception(f"BTC mínimo: {btc_config.MIN_BTC_SATS/1e8} BTC")
        if btc_amount_sats > btc_config.MAX_BTC_SATS:
            raise Exception(f"BTC máximo: {btc_config.MAX_BTC_SATS/1e8} BTC")
        if not buyer_brn_address.startswith("brn1"):
            raise Exception("Endereço BRN inválido, tem que começar com brn1")

        escrow = self.factory.create_btc_to_brn(
            creator_wallet=creator_wallet,
            btc_amount_sats=btc_amount_sats,
            buyer_wallet=buyer_brn_address
        )
        return {
            "escrow_id": escrow.escrow_id,
            "btc_address": btc_config.BTC_RECEIVE_ADDRESS,
            "btc_amount": btc_amount_sats / 1e8,
            "btc_amount_sats": btc_amount_sats,
            "brn_amount": escrow.brn_amount / 1e8,
            "brn_amount_sats": escrow.brn_amount,
            "buyer": buyer_brn_address,
            "status": "OPEN",
            "expires_at": escrow.expires_at,
            "l2_hash": escrow.l2_hash,
            "instructions": f"Envie {btc_amount_sats/1e8} BTC para {btc_config.BTC_RECEIVE_ADDRESS} em até 24h"
        }

    def get_order(self, escrow_id):
        escrow = self.db.get_l2_escrow_by_id(escrow_id)
        if not escrow:
            return None
        return {
            "escrow_id": escrow['escrow_id'],
            "status": escrow['status'],
            "btc_txid": escrow.get('btc_txid'),
            "btc_amount": escrow['btc_expected_sats'] / 1e8,
            "brn_amount": escrow['brn_amount'] / 1e8,
            "buyer": escrow['buyer'],
            "btc_address": escrow['btc_address'],
            "created_at": escrow['created_at'],
            "expires_at": escrow['expires_at'],
            "mined": escrow['status'] == "RELEASED"
        }

    def list_orders(self, buyer=None, status="OPEN"):
        if buyer:
            escrows = self.factory.list_by_buyer(buyer)
        else:
            escrows = self.db.get_l2_escrows(status=status)
        return escrows

    def quote(self, btc_amount_sats):
        """Calcula cotação sem criar ordem"""
        brn_sats = int((btc_amount_sats / 100_000_000) * btc_config.BRN_PER_BTC * 100_000_000)
        fee = int(brn_sats * (btc_config.FEE_PERCENT / 100))
        net = brn_sats - fee
        return {
            "btc_amount": btc_amount_sats / 1e8,
            "brn_gross": brn_sats / 1e8,
            "fee_percent": btc_config.FEE_PERCENT,
            "fee_brn": fee / 1e8,
            "brn_net": net / 1e8,
            "rate": f"1 BTC = {btc_config.BRN_PER_BTC} BRN"
        }

    def stats(self):
        try:
            s = self.db.get_stats() if hasattr(self.db, 'get_stats') else {}
            return {
                "open": len(self.db.get_l2_escrows(status="OPEN")),
                "btc_detected": len(self.db.get_l2_escrows(status="BTC_DETECTED")),
                "released": len(self.db.get_l2_escrows(status="RELEASED")),
                "expired": len(self.db.get_l2_escrows(status="EXPIRED")),
                "total": len(self.db.get_l2_escrows()),
                "btc_address": btc_config.BTC_RECEIVE_ADDRESS,
                "rate": btc_config.BRN_PER_BTC
            }
        except Exception as e:
            return {"error": str(e)}