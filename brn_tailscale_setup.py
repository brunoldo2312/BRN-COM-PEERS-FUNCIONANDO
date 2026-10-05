"""
brn_tailscale_setup.py — Configura rede Tailscale para o BRN
=============================================================
Resolve o problema de VLANs separadas criando uma VPN mesh
entre as maquinas. Depois de configurado, funciona de qualquer
rede (VLAN, internet, celular, etc).

Uso:
    python brn_tailscale_setup.py
"""
import os
import sys
import json
import shutil
import subprocess
import platform
from pathlib import Path

BOOTSTRAP_FILE = Path(__file__).parent / "bootstrap_peers.json"
P2P_PORT = 6001


# ============================================================
# HELPERS
# ============================================================
def banner(txt):
    print()
    print("=" * 60)
    print(f"  {txt}")
    print("=" * 60)


def run(cmd, capture=True):
    try:
        r = subprocess.run(
            cmd, shell=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            text=True,
        )
        return r.returncode, (r.stdout or "").strip()
    except Exception as e:
        return -1, str(e)


def is_windows():
    return platform.system().lower().startswith("win")


def is_linux():
    return platform.system().lower() == "linux"


# ============================================================
# 1) DETECTA SE TAILSCALE JA ESTA INSTALADO
# ============================================================
def tailscale_installed():
    rc, _ = run("tailscale version")
    return rc == 0


def tailscale_running():
    rc, out = run("tailscale status")
    if rc != 0:
        return False
    return "Logged out" not in out and "not logged in" not in out.lower()


def tailscale_ip():
    rc, out = run("tailscale ip -4")
    if rc != 0:
        return ""
    # Pode retornar vários IPs (um por linha). Pega o primeiro 100.x
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("100."):
            return line
    return ""


# ============================================================
# 2) INSTALA TAILSCALE
# ============================================================
def install_tailscale():
    banner("INSTALANDO TAILSCALE")

    if is_linux():
        print("[*] Ubuntu/Linux detectado")
        print("[*] Rodando instalador oficial...")
        print()
        rc, out = run(
            "curl -fsSL https://tailscale.com/install.sh | sh",
            capture=False,
        )
        if rc != 0:
            print()
            print("[X] Falha na instalacao.")
            print("    Se pediu sudo, rode manualmente:")
            print("      curl -fsSL https://tailscale.com/install.sh | sh")
            return False
        print()
        print("[ok] Tailscale instalado")
        return True

    if is_windows():
        print("[!] Windows detectado")
        print()
        print("    Instale manualmente:")
        print("      1. Abra https://tailscale.com/download/windows")
        print("      2. Baixe e instale o MSI")
        print("      3. Faca login com a mesma conta do Ubuntu")
        print("      4. Rode este script de novo")
        print()
        return False

    print("[X] Sistema nao suportado automaticamente.")
    return False


# ============================================================
# 3) SOBE TAILSCALE
# ============================================================
def start_tailscale():
    banner("AUTENTICANDO TAILSCALE")

    if is_linux():
        print("[*] Rodando 'sudo tailscale up'...")
        print()
        print("    IMPORTANTE:")
        print("    1. Vai abrir uma URL no terminal.")
        print("    2. Copie e cole no navegador.")
        print("    3. Faca login com sua conta (Google/GitHub/etc).")
        print("    4. Autorize o dispositivo.")
        print()
        rc, _ = run("sudo tailscale up", capture=False)
        if rc != 0:
            print("[X] Falha. Rode manualmente: sudo tailscale up")
            return False
        return True

    if is_windows():
        print("[i] No Windows, abra o Tailscale no menu Iniciar e faca login.")
        print("    Depois rode este script de novo.")
        return False

    return False


# ============================================================
# 4) ATUALIZA BOOTSTRAP_PEERS.JSON
# ============================================================
def update_bootstrap(own_ip):
    banner("ATUALIZANDO BOOTSTRAP_PEERS.JSON")

    # Le existente
    existing = []
    if BOOTSTRAP_FILE.exists():
        try:
            existing = json.loads(BOOTSTRAP_FILE.read_text())
        except Exception:
            existing = []

    print(f"IP Tailscale desta maquina: {own_ip}")
    print()
    print("Cole os IPs Tailscale das OUTRAS maquinas (separados por espaco).")
    print("Exemplo: 100.64.1.42 100.64.1.43")
    print("(deixe em branco para manter o bootstrap atual)")
    print()
    entry = input("IPs: ").strip()

    if not entry:
        print("[i] Nada a fazer.")
        return

    others = [ip.strip() for ip in entry.split() if ip.strip().startswith("100.")]

    new_entries = set(existing)
    for ip in others:
        new_entries.add(f"{ip}:{P2P_PORT}")

    # Adiciona proprio IP se nao estiver (para descoberta local)
    if own_ip:
        new_entries.add(f"{own_ip}:{P2P_PORT}")

    new_list = sorted(new_entries)

    print()
    print("Novo bootstrap_peers.json:")
    for e in new_list:
        print(f"  - {e}")

    try:
        BOOTSTRAP_FILE.write_text(
            json.dumps(new_list, indent=2),
            encoding="utf-8",
        )
        print()
        print("[ok] Salvo. Reinicie o no BRN para aplicar.")
    except Exception as e:
        print(f"[X] Erro: {e}")


# ============================================================
# MAIN
# ============================================================
def main():
    banner("BRN — CONFIGURACAO DE TAILSCALE")

    print("Este script resolve VLANs separadas usando Tailscale.")
    print("Tailscale cria uma VPN mesh entre suas maquinas.")
    print()

    # 1. Instalado?
    if not tailscale_installed():
        print("[!] Tailscale NAO instalado")
        if not install_tailscale():
            print()
            print("Apos instalar, rode este script novamente.")
            return 1
        print()
        print("Rode este script de novo para continuar.")
        return 0

    print("[ok] Tailscale instalado")

    # 2. Autenticado?
    if not tailscale_running():
        print("[!] Tailscale nao esta autenticado")
        if not start_tailscale():
            return 1
        print()
        print("Apos autorizar, rode este script novamente.")
        return 0

    print("[ok] Tailscale autenticado")

    # 3. Pega o IP
    own_ip = tailscale_ip()
    if not own_ip:
        print("[X] Nao consegui obter o IP Tailscale.")
        print("    Rode manualmente: tailscale ip -4")
        return 1

    banner("SUCESSO")
    print(f"IP Tailscale desta maquina: {own_ip}")
    print()
    print("INSTRUCOES:")
    print()
    print("1. No OUTRO computador, rode este script tambem.")
    print(f"   Ele vai mostrar um IP Tailscale parecido (100.x.x.x).")
    print()
    print(f"2. Volte aqui e adicione o IP do outro no bootstrap_peers.json")
    print()
    print("3. Reinicie o no BRN nas duas maquinas")

    # 4. Atualiza bootstrap
    print()
    resp = input("Deseja atualizar o bootstrap_peers.json agora? (S/n): ").strip().lower()
    if resp != "n":
        update_bootstrap(own_ip)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n[!] Cancelado pelo usuario.")
        sys.exit(130)