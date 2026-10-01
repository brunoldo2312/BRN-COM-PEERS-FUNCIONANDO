"""
BRN Node - wallets.py SEGURO com função COPIAR
"""
import os, sys, requests, subprocess, platform
from dotenv import load_dotenv
from pathlib import Path
load_dotenv()
try:
    from bitcoinlib.wallets import Wallet
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "bitcoinlib", "--quiet"])
    from bitcoinlib.wallets import Wallet

REDE = os.getenv("REDE", "testnet").lower()
WALLET_NAME = os.getenv("BRN_WALLET_NAME", "brn_client_wallet")
Path("wallets").mkdir(exist_ok=True)

def copy_to_clipboard(texto: str) -> bool:
    texto = str(texto).strip()
    if not texto:
        return False
    try:
        import pyperclip
        pyperclip.copy(texto)
        print(f"[ok] Copiado: {texto[:12]}...{texto[-6:]}")
        return True
    except:
        pass
    try:
        sistema = platform.system()
        if sistema == "Windows":
            subprocess.run("clip", input=texto.encode('utf-8'), check=True, shell=True)
            print(f"[ok] Copiado (Windows)")
            return True
        elif sistema == "Darwin":
            subprocess.run("pbcopy", input=texto.encode('utf-8'), check=True)
            print(f"[ok] Copiado (Mac)")
            return True
        else:
            for cmd in ["xclip -selection clipboard", "xsel --clipboard --input", "wl-copy"]:
                try:
                    subprocess.run(cmd, input=texto.encode('utf-8'), check=True, shell=True)
                    print(f"[ok] Copiado (Linux)")
                    return True
                except:
                    continue
            import tkinter as tk
            r = tk.Tk(); r.withdraw(); r.clipboard_clear(); r.clipboard_append(texto); r.update(); r.destroy()
            print(f"[ok] Copiado (Tkinter)")
            return True
    except Exception as e:
        print(f"[!] Falha ao copiar: {e}")
        print(f"[i] Copie manualmente: {texto}")
        return False

def get_secret_infisical(name):
    token=os.getenv("INFISICAL_TOKEN"); pid=os.getenv("INFISICAL_PROJECT_ID"); env=os.getenv("INFISICAL_ENV","dev")
    if not token or not pid: return None
    try:
        url="https://app.infisical.com/api/v3/secrets/raw"
        params={"secretName":name,"workspaceId":pid,"environment":env,"secretPath":"/"}
        headers={"Authorization": f"Bearer {token}"}
        r=requests.get(url,params=params,headers=headers,timeout=10)
        if r.status_code==200:
            return r.json()["secret"]["secretValue"]
        return None
    except:
        return None

def get_secure_key():
    for n in ["BTC_WIF","BTC_SEED"]:
        v=get_secret_infisical(n)
        if v and len(v)>10:
            print(f"[ok] Chave {n} do Infisical")
            return v
    for n in ["BTC_WIF","BTC_SEED"]:
        v=os.getenv(n)
        if v and len(v)>10 and "SEU_" not in v:
            print(f"[ok] Chave {n} do .env local")
            return v
    return None

def get_destino():
    for n in ["BTC_ENDERECO","ENDERECO_DESTINO"]:
        v=get_secret_infisical(n)
        if v: return v
    return os.getenv("BTC_ENDERECO") or os.getenv("ENDERECO_DESTINO")

class SecureWallet:
    def __init__(self, network=REDE, wallet_name=WALLET_NAME):
        self.network=network; self.wallet_name=wallet_name; self.wallet=None; self._init_wallet()
    def _init_wallet(self):
        try:
            self.wallet=Wallet(self.wallet_name, network=self.network)
            print(f"[ok] Carteira aberta: {self.wallet_name}")
        except:
            chave=get_secure_key()
            if chave:
                try:
                    self.wallet=Wallet.create(self.wallet_name, keys=chave, network=self.network)
                except Exception as e:
                    print(f"[X] Chave invalida: {e}")
                    self.wallet=Wallet.create(self.wallet_name, network=self.network)
            else:
                print("[*] Gerando carteira NOVA para este cliente...")
                self.wallet=Wallet.create(self.wallet_name, network=self.network)
                print(f"[ok] Endereco: {self.wallet.get_key().address}")
            chave=None
        try: self.wallet.scan()
        except: pass
    def get_address(self): return self.wallet.get_key().address
    def get_balance(self):
        try:
            self.wallet.scan(); return self.wallet.balance()
        except: return 0
    def copy_address(self):
        addr=self.get_address(); copy_to_clipboard(addr); print(f"Endereco copiado: {addr}"); return addr
    def copy_txid(self, txid): return copy_to_clipboard(txid)
    def copy_seed(self):
        try:
            seed=str(self.wallet.main_key); print("[!] ATENCAO: Copiando SEED - Nao compartilhe!"); return copy_to_clipboard(seed)
        except Exception as e:
            print(f"[X] Nao foi possivel copiar seed: {e}"); return False
    def send(self, destino=None, valor_sats=10000, fee_sats=1000, auto_copy_txid=True):
        if destino is None: destino=get_destino()
        if not destino or "SEU_" in destino or len(destino)<10:
            print("[X] ENDERECO_DESTINO nao configurado"); return None
        saldo=self.get_balance()
        if saldo < valor_sats+fee_sats:
            print(f"[X] Saldo insuficiente: {saldo} sats"); print(f"[i] Seu endereco: {self.get_address()}"); self.copy_address(); return None
        try:
            print(f"[*] Enviando {valor_sats} sats para {destino[:10]}...{destino[-6:]}")
            tx=self.wallet.send_to(destino, valor_sats, fee=fee_sats, network=self.network)
            print(f"[ok] TXID: {tx.txid}")
            if auto_copy_txid: self.copy_txid(tx.txid); print("[ok] TXID copiado!")
            explorer="https://mempool.space/testnet/tx/" if self.network=="testnet" else "https://mempool.space/tx/"
            print(f"Veja em: {explorer}{tx.txid}"); return tx.txid
        except Exception as e:
            print(f"[X] Falha ao enviar: {e}"); return None

brn_wallet=None
def get_wallet():
    global brn_wallet
    if brn_wallet is None: brn_wallet=SecureWallet()
    return brn_wallet
def get_new_address(): return get_wallet().get_address()
def get_balance(): return get_wallet().get_balance()
def send_to_address(address, amount_sats, fee=1000): return get_wallet().send(address, amount_sats, fee)
def copy_address(): return get_wallet().copy_address()

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--copy", action="store_true", help="So copia o endereco e sai")
    parser.add_argument("--copy-txid", type=str, help="Copia um TXID")
    parser.add_argument("--copy-seed", action="store_true", help="Copia seed (cuidado)")
    args=parser.parse_args()
    w=get_wallet()
    print(f"Rede: {w.network}")
    print(f"Endereco: {w.get_address()}")
    print(f"Saldo: {w.get_balance()} sats")
    if args.copy: w.copy_address()
    elif args.copy_txid: w.copy_txid(args.copy_txid)
    elif args.copy_seed: w.copy_seed()
    else:
        w.copy_address()
        print("\nDicas: --copy | --copy-seed | --copy-txid <txid>")
