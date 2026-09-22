#!/usr/bin/env bash
# Восстановление 3x-ui из бэкапа на (новом) сервере.
# Сначала должен быть выполнен scripts/install.sh (Docker, UFW и т.д.).
# Запуск: sudo bash /opt/3x-ui/scripts/restore.sh /root/3x-ui-YYYYMMDD-HHMMSS.tar.gz
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/3x-ui}"

if [[ $# -ne 1 ]]; then
  echo "Использование: sudo bash $0 <файл-бэкапа.tar.gz>" >&2
  exit 1
fi

[[ $EUID -eq 0 ]] || { echo "[x] Запусти от root" >&2; exit 1; }

ARCHIVE="$(readlink -f "$1")"
[[ -f "$ARCHIVE" ]] || { echo "[x] Файл не найден: $1" >&2; exit 1; }

cd "$APP_DIR"

echo "[+] Останавливаю текущий стек (если запущен)..."
docker compose down >/dev/null 2>&1 || true

echo "[+] Распаковываю $(basename "$ARCHIVE") в $APP_DIR..."
tar xzf "$ARCHIVE" -C "$APP_DIR"

echo "[+] Запускаю 3x-ui..."
docker compose pull
docker compose up -d

echo
echo "[+] Восстановление завершено: пользователи, UUID, ключи Reality и настройки — как в бэкапе."
echo "    Если у этого сервера другой IP — раздай родным новые sub-ссылки из панели (см. README)."
