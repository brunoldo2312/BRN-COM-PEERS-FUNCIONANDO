# BRN — BrunoCoin: Blockchain Layer 1 Completa em Python

# ⛓️ BRN — BrunoCoin \| Blockchain Layer 1

> **Uma blockchain completa desenvolvida do zero em Python\. Inclui rede P2P, carteira, mineração, explorador de blocos e API REST\.**
> 
> 

---

## 📋 Índice

- \[Sobre o Projeto\]\(\#\-sobre\-o\-projeto\)

- \[Principais Funcionalidades\]\(\#\-principais\-funcionalidades\)

- \[Arquitetura\]\(\#\-arquitetura\)

- \[Pré\-requisitos\]\(\#\-pré\-requisitos\)

- \[Instalação e Execução\]\(\#\-instalação\-e\-execução\)

- \[Portas e Endpoints\]\(\#\-portas\-e\-endpoints\)

- \[Como Usar\]\(\#\-como\-usar\)

- \[Estrutura do Projeto\]\(\#\-estrutura\-do\-projeto\)

- \[Contribuindo\]\(\#\-contribuindo\)

- \[Licença\]\(\#\-licença\)

---

## 🚀 Sobre o Projeto

O **BRN** é uma implementação completa de uma blockchain Layer 1 desenvolvida em Python, sem dependência de frameworks ou blockchains existentes\. O projeto foi criado com o objetivo de aprofundar o entendimento sobre como funciona uma blockchain real, desde o bloco gênese até a sincronização entre nós em uma rede P2P descentralizada\.

---

## ✨ Principais Funcionalidades

|\#|Funcionalidade|Descrição|
|---|---|---|
|🔗|**Blockchain Core**|Blocos, hashes SHA\-256, validação de cadeia e regra de fork choice baseada em trabalho acumulado|
|🌐|**Rede P2P**|Descoberta automática de nós via multicast, conexões TCP, sincronização de cadeia e reconexão automática|
|💰|**Carteira HD**|Compatível com padrões BIP39/BIP44 — geração de chaves hierárquicas e endereços bech32|
|⛏️|**Mineração \(PoW\)**|Prova de Trabalho com dificuldade ajustável e sistema de recompensa por bloco|
|📊|**Explorador de Blocos**|Interface web local para visualizar blocos, transações e saldos — acessível via navegador|
|🔌|**API REST**|Endpoints para consulta de blocos, transações, peers conectados e estimativa de taxas|
|🔒|**Segurança**|Dados sensíveis criptografados, backup automático e autenticação entre pares|
|📦|**Auto\-inicialização**|Cria automaticamente o banco de dados, bloco gênese e identidade do nó na primeira execução|

---

## 🏗️ Arquitetura

```Plain Text
┌─────────────────────────────────────────────────────────────┐
│                  EXPLORADOR WEB / CARTEIRA                    │
│              (http://localhost:8080 / index_wallet.html)     │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTP/REST
┌──────────────────────▼──────────────────────────────────────┐
│              SERVIDOR API & EXPLORADOR (Flask)                │
│                   ├─ /api/blocks       ├─ /api/transactions   │
│                   ├─ /api/peers        ├─ /api/work          │
└──────────────────────┬──────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        │              │              │
┌───────▼────────┐ ┌──▼───────────┐ ┌─▼──────────────┐
│   BLOCKCHAIN   │ │   REDE P2P   │ │   CARTEIRA     │
│ - Blocos       │ │ - Descoberta │ │ - Chaves BIP39 │
│ - Validação    │ │ - Sincroniza │ │ - Assinaturas  │
│ - Consenso     │ │ - Peers      │ │ - Transações   │
└────────────────┘ └──────────────┘ └───────────────┘
        │              │              │
        └──────────────┼──────────────┘
                       │
          ┌────────────▼────────────┐
          │   BANCO DE DADOS (SQLite)│
          │  brn_v2_chain.db         │
          └─────────────────────────┘
```

---

## 💻 Pré\-requisitos

- **Sistema Operacional:** Windows 10/11 ou Linux/macOS

- **Python:** Versão 3\.10 ou superior

- **Memória:** Mínimo 200 MB de espaço em disco

- **Rede:** Portas 5000, 6001 e 8080 liberadas \(ou configuráveis\)

### Dependências Python

```Plain Text
flask>=3.0.0
flask-cors>=4.0.0
requests>=2.31.0
cryptography>=41.0.0
pypdf>=3.0.0
```

---

## 🔧 Instalação e Execução

### Opção 1 — Usando o arquivo em lote \(Windows\)

```bash
# 1. Clone o repositório
git clone https://github.com/brunoldo2312/BRN-COM-PEERS-FUNCIONANDO.git
cd BRN-COM-PEERS-FUNCIONANDO

# 2. Execute o inicializador
iniciar.bat
```

### Opção 2 — Manual \(qualquer sistema operacional\)

```bash
# 1. Clone o repositório
git clone https://github.com/brunoldo2312/BRN-COM-PEERS-FUNCIONANDO.git
cd BRN-COM-PEERS-FUNCIONANDO

# 2. Instale as dependências
pip install -r requirements.txt

# 3. Execute o nó
python main.py
```

> ✅ Na **primeira execução**, o sistema irá:
> 
> - Criar automaticamente o banco de dados com o bloco gênese
> 
> - Gerar uma identidade única para o seu nó
> 
> - Conectar aos nós conhecidos automaticamente
> 
> 

---

## 🌐 Portas e Endpoints

|Serviço|Protocolo|Porta|Acesso|
|---|---|---|---|
|**API REST**|HTTP|5000|[http://127\.0\.0\.1:5000](http://127.0.0.1:5000)|
|**Explorador de Blocos**|HTTP|8080|[http://127\.0\.0\.1:8080](http://127.0.0.1:8080)|
|**Rede P2P \(TCP\)**|TCP|6001|Conexão entre nós|
|**Descoberta Multicast**|UDP|50007|Localização de peers na rede local|

### Principais Endpoints da API

|Endpoint|Método|Descrição|
|---|---|---|
|`/api/work`|GET|Retorna o trabalho acumulado da cadeia|
|`/api/peers`|GET|Lista de peers conectados|
|`/api/peers/score`|GET|Pontuação de confiabilidade dos peers|
|`/api/fee-estimate`|GET|Estimativa de taxa de transação|

---

## 📖 Como Usar

### 🚀 Iniciando a rede

```Plain Text
1. Execute iniciar.bat ou python main.py
        ⬇️
2. ✅ Nó inicializado — veja no terminal:
   [Chain] Gênese carregada
   [P2P] Escutando na porta 6001
   [HTTP] API em 5000
   [Explorer] Explorador em 8080
        ⬇️
3. Abra no navegador:
   • Explorador → http://localhost:8080
   • Carteira  → abra index_wallet.html no navegador
```

### 🔗 Conectando com outros nós

- O nó automaticamente baixa a lista de peers conhecidos na inicialização

- Se não houver internet, usa a lista local \(`bootstrap_peers.json`\)

- Descobre nós na rede local via multicast

- Tenta reconectar a cada 5 minutos se desconectar

---

## 📁 Estrutura do Projeto

```Plain Text
BRN-COM-PEERS-FUNCIONANDO/
├── main.py                  # Ponto de entrada — inicializa todos os subsistemas
├── blockchain.py            # Núcleo da blockchain — blocos, validação, consenso
├── chain_validator.py       # Validação de integridade da cadeia
├── p2p_unified.py           # Rede P2P — servidor, cliente e sincronização
├── discovery_v2.py          # Descoberta automática de nós na rede
├── sync_manager.py          # Sincronização de cadeia entre peers
├── wallet.py                # Lógica da carteira — chaves, endereços, transações
├── crypto.py                # Funções criptográficas — hashes, assinaturas
├── bech32.py                # Codificação de endereços
├── db_v3.py                 # Banco de dados SQLite — persistência da cadeia
├── server.py                # API REST — endpoints HTTP
├── explorer.py              # Servidor do explorador de blocos
├── miner_loop.py            # Loop de mineração PoW
├── backup_auto.py           # Backup automático da base de dados
├── secure_store.py          # Armazenamento criptografado de credenciais
├── brn_config.py            # Configurações gerais da rede
├── brn_logger.py            # Sistema de logs
├── bootstrap_peers.json     # Nós conhecidos inicializados
├── index.html               # Interface do explorador web
├── index_wallet.html        # Interface da carteira web
├── iniciar.bat              # Script de inicialização (Windows)
├── atualizar_v4.cmd         # Script de atualização do nó
├── requirements.txt         # Dependências do Python
├── Dockerfile               # Containerização (opcional)
└── README.md                # Este arquivo
```

---

## 🤝 Contribuindo

Contribuições são bem\-vindas\! Sinta\-se à vontade para:

- ⭐ Dar uma estrela no repositório

- 🐛 Reportar problemas e bugs

- 💡 Sugerir novas funcionalidades

- 🔀 Enviar Pull Requests

### Padrões de Código

- Código em Python seguindo boas práticas de legibilidade

- Comentários em português

- Testar funcionalidades antes de enviar alterações

---

## ⚠️ Avisos Importantes

> ⚠️ **Este é um projeto de desenvolvimento e aprendizado\.** Não foi auditado profissionalmente\. Não recomendado para uso em produção com valores reais sem uma revisão completa de segurança\.
> 
> 

> 🔒 **Arquivos sensíveis:** O banco de dados, identidade do nó e arquivos de estado são gerados automaticamente na primeira execução e **não devem ser compartilhados ou versionados no Git**\. Consulte o arquivo `.gitignore` para mais detalhes\.
> 
> 

---

## 📄 Licença

Este projeto está distribuído sob a **Licença MIT** — veja o arquivo LICENSE para mais detalhes\.

---

⛓️ BRN — BrunoCoin

  Desenvolvido por Bruno Labanca de Oliveira

  Construído do zero em Python • Desde 2026

  