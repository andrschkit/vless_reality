#!/usr/bin/env bash
# Установка НОДЫ: VPN-сервер под управлением мастер-панели.
# Готовая нода = базовый стек + закрытая панель + RU-фильтр + API-токен для мастера.
# Дальше ноду нужно только подключить к мастеру: scripts/attach-node.sh (на мастере).
#
# Запуск на ЧИСТОМ сервере: bash scripts/install-node.sh <IP_МАСТЕРА>
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "[x] Запусти от root" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MASTER_IP="${1:?Укажи IP мастера: bash $0 <IP_МАСТЕРА>}"
[[ "$MASTER_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "[x] Похоже, не IPv4: $MASTER_IP" >&2; exit 1; }

# 1. Базовый стек: Docker + 3x-ui + BBR + UFW + cron-обновление
bash "$SCRIPT_DIR/install.sh"

# 2. Панель ноды — только с IP мастера
bash "$SCRIPT_DIR/node-hardening.sh" "$MASTER_IP" >/dev/null

# 3. Свои логин/пароль панели (bcrypt в sqlite, как в AGENTS.md)
apt-get install -y -qq sqlite3 apache2-utils >/dev/null 2>&1
NEWUSER="adm$(openssl rand -hex 3)"
NEWPASS="$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c 20)"
HASH="$(htpasswd -bnBC 10 "" "$NEWPASS" | tr -d ':\n' | sed 's/^\$2y/\$2a/')"
sqlite3 /opt/3x-ui/db/x-ui.db "update users set username='$NEWUSER', password='$HASH' where id=1;"
docker restart 3xui_app >/dev/null

# 4. Ждём панель, логинимся, создаём API-токен для мастера, вливаем RU-фильтр
for i in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:2053/ && break; sleep 2; done

source "$SCRIPT_DIR/lib/xui-api.sh"
xui_init "http://127.0.0.1:2053" "/" "$NEWUSER" "$NEWPASS"

TOK_RESP=$(xui_api POST /setting/apiTokens/create '{"name":"master","scope":"admin","expiresAt":0}')
TOKEN=$(printf '%s' "$TOK_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin).get('obj') or {}; print(d.get('token') or d.get('plaintextToken') or '')" 2>/dev/null || true)
[[ -n "$TOKEN" ]] || { echo "[x] токен не создан: $TOK_RESP" >&2; exit 1; }

FILTER_RESP=$(xui_apply_ru_filter "$SCRIPT_DIR/ru-filter-rules.json")
printf '%s' "$FILTER_RESP" | grep -q '"success":true' || { echo "[x] RU-фильтр не применён: ${FILTER_RESP:0:200}" >&2; exit 1; }

NODE_IP="$(curl -fsSL --max-time 5 -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"

# 5. Карточка ноды — для мастера и для AGENTS.md
cat > /root/.node-info <<EOF
NODE_IP=$NODE_IP
PANEL_USER=$NEWUSER
PANEL_PASS=$NEWPASS
API_TOKEN=$TOKEN
EOF
chmod 600 /root/.node-info

cat <<EOF

[+] Нода готова: $NODE_IP
    Панель ноды: http://$NODE_IP:2053/ (снаружи закрыта, только $MASTER_IP)
    Логин/пароль панели: $NEWUSER / $NEWPASS
    API-токен для мастера: $TOKEN
    Всё записано в /root/.node-info (chmod 600).

    Подключение к мастеру (запускается НА МАСТЕРЕ):
      bash /root/vless_reality/scripts/attach-node.sh $NODE_IP $TOKEN

EOF
