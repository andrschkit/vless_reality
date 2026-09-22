#!/usr/bin/env bash
# Первичная установка 3x-ui (Docker) на чистый Ubuntu/Debian.
# Запуск: sudo bash scripts/install.sh
set -euo pipefail

APP_DIR="/opt/3x-ui"

log()  { echo -e "\033[1;32m[+]\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*"; }
die()  { echo -e "\033[1;31m[x]\033[0m $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запусти от root: sudo bash $0"
command -v apt-get >/dev/null 2>&1 || die "Нужен Ubuntu/Debian (apt-get не найден)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
[[ -f "$PROJECT_DIR/docker-compose.yml" ]] || die "Рядом со скриптом не найден docker-compose.yml"

log "Ставлю зависимости..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ufw tar ca-certificates unattended-upgrades cron

if ! command -v docker >/dev/null 2>&1; then
  log "Ставлю Docker (официальный скрипт get.docker.com)..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null 2>&1 || die "Плагин docker compose не найден"

log "Раскладываю проект в $APP_DIR..."
mkdir -p "$APP_DIR/db" "$APP_DIR/cert" "$APP_DIR/backups" "$APP_DIR/scripts"
cp "$PROJECT_DIR/docker-compose.yml" "$APP_DIR/docker-compose.yml"
cp "$SCRIPT_DIR"/*.sh "$APP_DIR/scripts/"
chmod +x "$APP_DIR/scripts/"*.sh

log "Включаю BBR (ускорение TCP)..."
cat >/etc/sysctl.d/99-bbr.conf <<'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
sysctl --system >/dev/null 2>&1 || true

log "Включаю автоматические security-обновления..."
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

systemctl enable --now cron >/dev/null 2>&1 || true
log "Вешаю еженедельное автообновление 3x-ui (cron: среда, 04:37)..."
cat >/etc/cron.d/3x-ui-update <<EOF
# Еженедельное обновление 3x-ui: бэкап + docker compose pull && up -d
37 4 * * 3 root bash $APP_DIR/scripts/update.sh >> /var/log/3x-ui-update.log 2>&1
EOF
chmod 644 /etc/cron.d/3x-ui-update

log "Настраиваю файрвол (UFW)..."
SSH_PORT="$(sshd -T 2>/dev/null | grep -m1 '^port ' | awk '{print $2}' || true)"
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] || SSH_PORT=22
ufw allow "$SSH_PORT"/tcp comment 'SSH' >/dev/null
ufw allow 443/tcp comment 'VLESS Reality' >/dev/null
ufw allow 2053/tcp comment '3x-ui panel' >/dev/null
ufw --force enable >/dev/null
log "UFW: разрешены $SSH_PORT/tcp (SSH), 443/tcp (Reality), 2053/tcp (панель)"

log "Запускаю 3x-ui..."
cd "$APP_DIR"
docker compose pull
docker compose up -d

SERVER_IP="$(curl -fsSL --max-time 5 -4 ifconfig.me 2>/dev/null || true)"
[[ -n "$SERVER_IP" ]] || SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
[[ -n "$SERVER_IP" ]] || SERVER_IP="IP-СЕРВЕРА"

echo
log "Готово! Панель 3x-ui: http://${SERVER_IP}:2053"
echo "    Логин/пароль по умолчанию: admin / admin"
echo "    СРАЗУ смени их в настройках панели и задай секретный путь (Web Base Path)."
echo "    Дальше действуй по README.md: создай inbound VLESS+Reality на 443 и добавь пользователей."
echo "    Автообновление 3x-ui: еженедельно по cron (/etc/cron.d/3x-ui-update), лог /var/log/3x-ui-update.log"
warn "Если на этом сервере работает AmneziaWG — открой её порт: ufw allow <порт>/udp"
