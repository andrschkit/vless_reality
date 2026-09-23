#!/usr/bin/env bash
# Закрытие панели НОДЫ от посторонних: порт 2053/tcp доступен только с IP мастера.
# Мастер ходит на API ноды по этому порту (синхронизация инбаундов, heartbeat).
# Запуск на ноде: bash scripts/node-hardening.sh <IP_МАСТЕРА>
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "[x] Запусти от root: sudo bash $0 <IP_МАСТЕРА>" >&2; exit 1; }
MASTER_IP="${1:?Укажи IP мастера: bash $0 <IP_МАСТЕРА>}"
[[ "$MASTER_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "[x] Похоже, это не IPv4: $MASTER_IP" >&2; exit 1; }

ufw delete allow 2053/tcp >/dev/null 2>&1 || true
ufw allow from "$MASTER_IP" to any port 2053 proto tcp comment '3x-ui panel from master' >/dev/null

echo "[+] Панель ноды теперь доступна только с $MASTER_IP (порт 2053)."
ufw status numbered
