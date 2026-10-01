# contracts/factory.py - Cria contratos prontos BTC->BRN
from.escrow_btc_brn import EscrowBTCtoBRN
import btc_config

class ContractFactory:
    def __init__(self, db):
        """
        db: sua ChainDB v6 com l2_escrows
        """
        self.db = db

    def create_btc_to_brn(self, creator_wallet, btc_amount_sats, buyer_wallet=None):
        """
        Cria contrato BTC->BRN

        creator_wallet: quem tem BRN (você, ex: "BRN_TREASURY")
        btc_amount_sats: quanto BTC o usuário vai enviar (ex: 100000 = 0.001 BTC)
        buyer_wallet: carteira BRN que vai receber (brn1q...)

        Retorna: EscrowBTCtoBRN
        """
        # Calcula quanto BRN liberar
        # Fórmula: brn = (btc_sats / 1e8) * BRN_PER_BTC * 1e8
        # Ex: 0.001 BTC * 10000 BRN/BTC = 10 BRN
        brn_per_btc = btc_config.BRN_PER_BTC
        brn_sats = int((btc_amount_sats / 100_000_000) * brn_per_btc * 100_000_000)

        # Aplica taxa de 2% que vai pra você
        fee = int(brn_sats * (btc_config.FEE_PERCENT / 100))
        brn_net = brn_sats - fee

        # Cria escrow
        escrow = EscrowBTCtoBRN(
            creator_wallet=creator_wallet,
            brn_amount_sats=brn_net,
            btc_expected_sats=btc_amount_sats,
            btc_address=btc_config.BTC_RECEIVE_ADDRESS,
            buyer_wallet=buyer_wallet
        )

        # Salva no L2 (sua ChainDB v6)
        self.db.save_l2_escrow(escrow.to_dict())

        print(f"[Factory] Escrow criado: {escrow.escrow_id}")
        print(f" BTC esperado: {btc_amount_sats/1e8} BTC")
        print(f" BRN bruto: {brn_sats/1e8} BRN")
        print(f" Taxa {btc_config.FEE_PERCENT}%: {fee/1e8} BRN")
        print(f" BRN líquido pro comprador: {brn_net/1e8} BRN")
        print(f" Comprador: {buyer_wallet}")
        print(f" Enviar BTC para: {btc_config.BTC_RECEIVE_ADDRESS}")

        return escrow

    def list_open(self):
        """Lista todos escrows OPEN"""
        return self.db.get_l2_escrows(status="OPEN")

    def list_by_buyer(self, buyer_wallet):
        """Lista escrows de um comprador"""
        all_escrows = self.db.get_l2_escrows()
        return [e for e in all_escrows if e.get('buyer') == buyer_wallet]

    def get_by_id(self, escrow_id):
        """Pega escrow por ID"""
        return self.db.get_l2_escrow_by_id(escrow_id)

    def cancel_expired(self):
        """Cancela todos expirados (roda a cada 1h)"""
        import time
        expired = []
        for escrow in self.db.get_l2_escrows(status="OPEN"):
            if escrow['expires_at'] < time.time():
                self.db.update_l2_escrow_status(escrow['escrow_id'], "EXPIRED")
                expired.append(escrow['escrow_id'])
        if expired:
            print(f"[Factory] {len(expired)} escrows expirados cancelados")
        return expired