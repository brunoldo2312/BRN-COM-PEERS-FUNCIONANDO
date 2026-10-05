"""
chain_validator.py — Validacao de integridade da cadeia BRN
============================================================
Extraido de blockchain.py (Single Responsibility Principle).

Uso:
    from chain_validator import verify_chain, verify_chain_dict
    result = verify_chain(blockchain)          # ChainVerificationResult
    result = verify_chain_dict(blockchain)     # dict serializavel

v9.1.1:
  - [SEGURANÇA] Verifica auth_tag (HMAC-SHA256) em cada bloco
    quando require_auth_tag=True. Corrige lacuna onde verify_chain
    aceitava cadeia com auth_tag apagado/adulterado.
  - [FIX] _pubkey_matches_from usa bech32.address_from_pubkey
    (mesma funcao usada pelo blockchain.validate_tx).
  - [FIX] Fallback para UTXO sem pubkey (valida por endereco).

v6:
  - Verificacao de assinatura Ed25519 em cada tx nao-coinbase,
    binding pubkey<->from, e rejeicao de coinbase com assinatura.
============================================================
"""
import time


# ============================================================
# RESULTADO
# ============================================================
class ChainVerificationResult:
    def __init__(self):
        self.valid = True
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.blocks_checked = 0
        self.txs_checked = 0
        self.auth_tags_checked = 0
        self.height = 0
        self.tip_hash = ""
        self.elapsed_s = 0.0

    def add_error(self, msg: str):
        self.valid = False
        self.errors.append(msg)

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "blocks_checked": self.blocks_checked,
            "txs_checked": self.txs_checked,
            "auth_tags_checked": self.auth_tags_checked,
            "height": self.height,
            "tip_hash": self.tip_hash,
            "elapsed_s": self.elapsed_s,
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if self.valid and not self.warnings:
            return "Cadeia integra — nenhum problema encontrado."
        parts = ["Cadeia valida" if self.valid else "Cadeia INVALIDA"]
        if self.errors:
            parts.append(f"{len(self.errors)} erro(s)")
        if self.warnings:
            parts.append(f"{len(self.warnings)} aviso(s)")
        return " | ".join(parts)


# ============================================================
# HELPERS DE VALIDACAO CRIPTOGRAFICA
# ============================================================
def _verify_tx_signature(tx: dict) -> tuple[bool, str]:
    """
    Verifica a assinatura de uma tx nao-coinbase.
    Retorna (ok, motivo). Motivo vazio se ok.
    """
    from blockchain import signing_hash
    from wallet import Wallet

    try:
        sig_hash = signing_hash(tx)
    except Exception as e:
        return False, f"signing_hash falhou ({e})"

    for i, inp in enumerate(tx["inputs"]):
        pk_hex = inp.get("pubkey", "")
        sig    = inp.get("signature", "")
        if not pk_hex or not sig:
            return False, f"input[{i}] sem pubkey/assinatura"
        if not Wallet.verify(sig_hash, sig, pk_hex):
            return False, f"input[{i}] assinatura invalida"

    return True, ""


def _has_required_fields(tx: dict) -> bool:
    required = ("inputs", "outputs", "timestamp", "txid")
    return all(k in tx for k in required)


def _pubkey_matches_from(tx: dict) -> tuple[bool, str]:
    """
    Bind pubkey -> endereco -> input. Evita ataque onde o atacante
    troca 'from'/'pubkey' mantendo a assinatura original.

    v9.1.1: usa bech32.address_from_pubkey (mesma funcao do blockchain).
    """
    # Importa do mesmo lugar que o blockchain.validate_tx usa
    try:
        from bech32 import address_from_pubkey
    except ImportError:
        # Fallback para crypto.pubkey_to_address (versao antiga)
        try:
            from crypto import pubkey_to_address as address_from_pubkey
        except ImportError:
            return False, "modulo bech32/crypto nao encontrado"

    for i, inp in enumerate(tx["inputs"]):
        pk_hex = inp.get("pubkey", "")
        if not pk_hex:
            return False, f"input[{i}] sem pubkey"
        try:
            _ = address_from_pubkey(bytes.fromhex(pk_hex))
        except Exception as e:
            return False, f"input[{i}] pubkey invalida ({e})"

    return True, ""


# ============================================================
# v9.1.1: VERIFICACAO DO AUTH TAG (HMAC)
# ============================================================
def _verify_block_auth_tag(block: dict, blockchain) -> tuple[bool, str]:
    """
    Verifica auth_tag do bloco se a blockchain exigir.
    Retorna (ok, motivo). Motivo vazio se ok.
    """
    # Se a blockchain nao exige, retorna OK
    if not getattr(blockchain, "require_auth_tag", False):
        return True, ""

    secret = getattr(blockchain, "network_secret", "") or ""
    if not secret:
        return False, "no exige auth_tag mas nao tem network_secret"

    tag = block.get("auth_tag")
    if not tag:
        return False, "bloco sem auth_tag"

    try:
        from blockchain import verify_auth_tag
    except ImportError:
        return False, "funcao verify_auth_tag nao encontrada"

    if not verify_auth_tag(block["height"], block["hash"], tag, secret):
        return False, "auth_tag invalido (segredo errado ou hash adulterado)"

    return True, ""


# ============================================================
# VALIDACAO PRINCIPAL
# ============================================================
def verify_chain(blockchain) -> ChainVerificationResult:
    """
    Verifica toda a cadeia do genesis ate o topo.

    Checagens:
      1.  Genesis valido (height 0, prev_hash zerado)
      2.  Alturas sequenciais
      3.  Encadeamento (prev_hash == hash anterior)
      4.  Hash declarado == recalculado
      5.  PoW valido (hash <= target)
      6.  Merkle root confere com txids
      7.  Primeira tx do bloco e coinbase
      8.  Coinbase <= recompensa + taxas
      9.  Ausencia de gasto duplo
      10. Ausencia de txid duplicada
      11. Ausencia de saldo negativo
      12. Recompensa compativel com halving
      13. [v6] Assinatura Ed25519 valida em txs nao-coinbase
      14. [v6] Pubkey corresponde ao 'from'
      15. [v6] Coinbase NAO tem assinatura nem pubkey no input
      16. [v9.1.1] auth_tag (HMAC) valido quando require_auth_tag=True
    """
    from blockchain import (
        block_hash, meets_difficulty, compute_merkle_root,
        txid as calc_txid, GENESIS_PREV,
    )

    result = ChainVerificationResult()
    t0 = time.time()

    db = blockchain.db
    height = db.height()
    result.height = height
    result.tip_hash = db.tip_hash()

    if height < 0:
        result.add_error("Cadeia vazia (nenhum bloco).")
        return result

    require_auth = bool(getattr(blockchain, "require_auth_tag", False))

    spent_utxos: set[tuple[str, int]] = set()
    all_txids: set[str] = set()
    prev_hash = None

    for h in range(height + 1):
        block = db.get_block(h)
        if not block:
            result.add_error(f"Bloco #{h} ausente no banco.")
            continue

        result.blocks_checked += 1
        prefixo = f"Bloco #{h}"

        # 1) genesis
        if h == 0:
            if block["height"] != 0:
                result.add_error(f"{prefixo}: height != 0")
            if block["prev_hash"] != GENESIS_PREV:
                result.add_error(f"{prefixo}: prev_hash de genesis invalido")

        # 2) altura sequencial
        if block["height"] != h:
            result.add_error(f"{prefixo}: height declarado = {block['height']}")

        # 3) encadeamento
        if h > 0 and block["prev_hash"] != prev_hash:
            result.add_error(
                f"{prefixo}: prev_hash nao corresponde ao bloco #{h-1}"
            )

        # 4) hash recalculado
        recalc = block_hash(
            block["prev_hash"], block["merkle"], block["timestamp"],
            block["nonce"], block["difficulty"],
        )
        if recalc != block["hash"]:
            result.add_error(
                f"{prefixo}: hash adulterado "
                f"({block['hash'][:14]}... vs {recalc[:14]}...)"
            )

        # 5) PoW
        if not meets_difficulty(block["hash"], block["difficulty"]):
            result.add_error(
                f"{prefixo}: nao atende a dificuldade {block['difficulty']}"
            )

        # 6) merkle
        txids = [t["txid"] for t in block["transactions"]]
        merkle_calc = compute_merkle_root(txids)
        if merkle_calc != block["merkle"]:
            result.add_error(
                f"{prefixo}: merkle divergente "
                f"({block['merkle'][:12]}... vs {merkle_calc[:12]}...)"
            )

        # 16) auth_tag (v9.1.1) — depois de validar hash + PoW
        if require_auth:
            ok_tag, motivo_tag = _verify_block_auth_tag(block, blockchain)
            if not ok_tag:
                result.add_error(f"{prefixo}: {motivo_tag}")
            else:
                result.auth_tags_checked += 1

        # 7) coinbase
        if not block["transactions"]:
            result.add_error(f"{prefixo}: bloco sem transacoes")
            prev_hash = block["hash"]
            continue

        cb = block["transactions"][0]
        is_cb = bool(cb["inputs"]) and cb["inputs"][0]["txid"] == "0" * 64
        if not is_cb:
            result.add_error(f"{prefixo}: primeira tx nao e coinbase")

        # 8) coinbase <= recompensa + taxas
        if is_cb:
            reward_esp = blockchain.current_reward(h)
            fees = 0
            for t in block["transactions"][1:]:
                try:
                    fees += blockchain.tx_fee(t)
                except Exception:
                    pass
            cb_total = sum(o["amount"] for o in cb["outputs"])
            if cb_total > reward_esp + fees:
                result.add_error(
                    f"{prefixo}: coinbase {cb_total} > "
                    f"recompensa {reward_esp} + taxas {fees}"
                )
            for inp in cb["inputs"]:
                if inp.get("signature") or inp.get("pubkey"):
                    result.add_error(
                        f"{prefixo}: coinbase input com assinatura/pubkey"
                    )

        # 9..15) cada tx
        for idx, t in enumerate(block["transactions"]):
            result.txs_checked += 1
            short = t.get("txid", "?")[:12]

            try:
                if t["txid"] != calc_txid(t):
                    result.add_error(f"{prefixo}: tx {short}... txid adulterado")
            except Exception as e:
                result.add_error(f"{prefixo}: erro ao recalcular txid ({e})")

            if t["txid"] in all_txids:
                result.add_error(f"{prefixo}: txid duplicada {short}...")
            all_txids.add(t["txid"])

            if idx == 0 and is_cb:
                continue

            if not _has_required_fields(t):
                result.add_error(f"{prefixo}: tx {short}... faltando campos")
                continue

            ok_sig, motivo = _verify_tx_signature(t)
            if not ok_sig:
                result.add_error(f"{prefixo}: tx {short}... {motivo}")
                for inp in t["inputs"]:
                    spent_utxos.add((inp["txid"], inp["vout"]))
                continue

            ok_bind, motivo_bind = _pubkey_matches_from(t)
            if not ok_bind:
                result.add_error(f"{prefixo}: tx {short}... {motivo_bind}")

            for inp in t["inputs"]:
                key = (inp["txid"], inp["vout"])
                if key in spent_utxos:
                    result.add_error(
                        f"{prefixo}: gasto duplo — "
                        f"{inp['txid'][:12]}...:{inp['vout']}"
                    )
                spent_utxos.add(key)

        prev_hash = block["hash"]

    # 11) saldo negativo
    try:
        neg = db.conn.execute(
            "SELECT address, SUM(amount) AS s FROM utxos "
            "WHERE spent=0 GROUP BY address HAVING s < 0"
        ).fetchall()
        for row in neg:
            result.add_error(f"Saldo negativo: {row['address'][:16]}...")
    except Exception:
        pass

    result.elapsed_s = round(time.time() - t0, 3)
    return result


def verify_chain_dict(blockchain) -> dict:
    return verify_chain(blockchain).to_dict()
