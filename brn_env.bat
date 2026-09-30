@echo off
REM ==== BRN env vars — ajuste os valores abaixo ====
REM IP do PC que roda o tracker (se nao tiver tracker, deixe vazio)
set BRN_TRACKER=http://192.168.0.17:8000

REM Frase secreta da rede — TEM QUE SER IGUAL em todos os PCs
set BRN_NETWORK_SECRET=minha-frase-secreta-brn-2026-muito-longa

REM Senha do no (identidade Ed25519) — diferente por PC
set BRN_NODE_PASSWORD=senha-do-no-deste-pc-2026

REM Senha da carteira — usada pelo pywebview
set BRN_WEB_PASS=senha-da-carteira-2026

REM Intervalo de mineracao em segundos
set BRN_MINER_INTERVAL=30
set BRN_MINER_AUTO=1