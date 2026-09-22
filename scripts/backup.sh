#!/usr/bin/env bash
# Бэкап всех данных 3x-ui: db (юзеры, ключи, настройки), cert, compose.
# Контейнер останавливается на пару секунд ради консистентной sqlite.
# Запуск: sudo bash /opt/3x-ui/scripts/backup.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/3x-ui}"
KEEP="${KEEP:-14}"

cd "$APP_DIR"
mkdir -p backups

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="backups/3x-ui-${STAMP}.tar.gz"

echo "[+] Останавливаю контейнер (2-3 секунды простоя)..."
docker compose stop >/dev/null 2>&1 || true

tar czf "$OUT" db cert docker-compose.yml

echo "[+] Запускаю контейнер обратно..."
docker compose start >/dev/null

echo "[+] Ротация: храню последние $KEEP архивов"
ls -1t backups/3x-ui-*.tar.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | xargs -r rm -f

echo "[+] Бэкап готов: $APP_DIR/$OUT"
echo "    Забрать на компьютер: scp root@<IP>:$APP_DIR/$OUT ."
