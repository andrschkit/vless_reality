#!/usr/bin/env bash
# Обновление 3x-ui: бэкап (на случай отката) + свежий образ + перезапуск.
# Вызывается cron'ом еженедельно (см. /etc/cron.d/3x-ui-update), можно и вручную:
# sudo bash /opt/3x-ui/scripts/update.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/3x-ui}"

cd "$APP_DIR"

echo "[+] $(date '+%F %T') Бэкап перед обновлением..."
bash "$APP_DIR/scripts/backup.sh"

echo "[+] Обновляю образ и перезапускаю..."
docker compose pull
docker compose up -d

# вычищаем старые слои образов, чтобы не забить диск
docker image prune -f >/dev/null

echo "[+] Обновление завершено."
