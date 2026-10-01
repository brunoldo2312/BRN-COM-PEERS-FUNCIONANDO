# contracts/factory.py - Cria contratos prontos
from .escrow_btc_brn import EscrowBTCtoBRN
import btc_config

class ContractFactory:
    def __init__(self, db):
        self.db = db

    def create_btc_to_brn(self, creator_wallet, btc_amount_sats, buyer_wallet=None):
        brn_per_btc = btc_config.BRN_PER_BTC
        brn_sats = int((btc_amount_sats / 100_000_000) * brn_per_btc * 100_000_000)
        fee = int(brn_sats * (btc_config.FEE_PERCENT / 100))
        brn_net = brn_sats - fee

        escrow = EscrowBTCtoBRN(
            creator_wallet=creator_wallet,
            brn_amount_sats=brn_net,
            btc_expected_sats=btc_amount_sats,
            btc_address=btc_config.BTC_RECEIVE_ADDRESS,
            buyer_wallet=buyer_wallet
        )
        self.db.save_l2_escrow(escrow.to_dict())
        return escrow