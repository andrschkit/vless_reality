#!/usr/bin/env bash
# Установка телеграм-бота семейного VPN на МАСТЕРЕ (только мастер!).
# Идемпотентен: повторный запуск просто пересобирает и перезапускает контейнер,
# bootstrap-код и привязки в bot.env/bot.db сохраняются.
#
# Предусловия:
#   - мастер развёрнут (install-master.sh), панель работает;
#   - токен от @BotFather: в /root/.tg-bot-token (chmod 600) — install-скрипт
#     скопирует его в /opt/3x-ui/bot/tg-bot-token (владелец uid контейнера, chmod 400);
#     либо сразу положи токен в /opt/3x-ui/bot/tg-bot-token.
#
# Запуск: bash scripts/install-bot.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "[x] Запусти от root" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/3x-ui
SUB_URI_DEFAULT="https://abs.4myfamdom.xyz/kfaatpmh442ci4kr/"
BOT_NAME_DEFAULT="AndyFam1313_bot"

command -v docker >/dev/null || { echo "[x] docker не найден" >&2; exit 1; }
[[ -f "$APP_DIR/db/x-ui.db" ]] || { echo "[x] не найдена БД панели $APP_DIR/db/x-ui.db — это точно мастер?" >&2; exit 1; }

# 1. Код бота + compose
mkdir -p "$APP_DIR/bot"
cp -r "$SCRIPT_DIR/../bot/." "$APP_DIR/bot/"
cp "$SCRIPT_DIR/../docker-compose.bot.yml" "$APP_DIR/docker-compose.bot.yml"

# 2. Токен: контейнер читает его под root, chmod 400 достаточно.
#    /root непроходим для чужих uid, поэтому монтируем копию из каталога бота.
if [[ ! -f "$APP_DIR/bot/tg-bot-token" ]]; then
  [[ -f /root/.tg-bot-token ]] || { echo "[x] Нет токена: положи его в /root/.tg-bot-token или $APP_DIR/bot/tg-bot-token" >&2; exit 1; }
  cp /root/.tg-bot-token "$APP_DIR/bot/tg-bot-token"
  chmod 600 /root/.tg-bot-token
fi
chown root:root "$APP_DIR/bot/tg-bot-token"
chmod 400 "$APP_DIR/bot/tg-bot-token"
touch "$APP_DIR/bot/bot.db"
chown root:root "$APP_DIR/bot" "$APP_DIR/bot/bot.db"
chmod 600 "$APP_DIR/bot/bot.db"

# 3. Конфиг бота (генерируется один раз: bootstrap-код живёт здесь)
if [[ ! -f "$APP_DIR/bot/bot.env" ]]; then
  cat > "$APP_DIR/bot/bot.env" <<EOF
BOT_NAME=$BOT_NAME_DEFAULT
SUB_URI=${SUB_URI:-$SUB_URI_DEFAULT}
ADMIN_BOOTSTRAP=$(openssl rand -hex 8)
INVITE_TTL_DAYS=7
EOF
  chmod 600 "$APP_DIR/bot/bot.env"
  echo "[+] создан $APP_DIR/bot/bot.env"
fi

# 4. Сборка и запуск
cd "$APP_DIR"
docker compose -f docker-compose.bot.yml up -d --build

# 5. Проверка: getMe отвечает И процесс бота реально стартовал (строка в логах)
TOKEN="$(cat "$APP_DIR/bot/tg-bot-token")"
ok=""
for i in $(seq 1 12); do
  if curl -sf --max-time 10 "https://api.telegram.org/bot$TOKEN/getMe" | grep -q '"ok":true'; then
    ok=1; break
  fi
  sleep 5
done
[[ -n "$ok" ]] || { echo "[x] Telegram API не отвечает (getMe). Логи: docker logs tg-vpn-bot" >&2; exit 1; }
ok=""
for i in $(seq 1 8); do
  if docker logs tg-vpn-bot 2>&1 | grep -q "старт бота"; then
    ok=1; break
  fi
  sleep 3
done
if [[ -z "$ok" ]]; then
  echo "[x] контейнер поднялся, но бот не стартовал. Последние логи:" >&2
  docker logs tg-vpn-bot 2>&1 | tail -15 >&2
  exit 1
fi

BOOT="$(grep '^ADMIN_BOOTSTRAP=' "$APP_DIR/bot/bot.env" | cut -d= -f2)"

cat <<EOF

[+] Бот работает: контейнер tg-vpn-bot (docker logs tg-vpn-bot)
    1. Открой от себя в телеграме: https://t.me/$BOT_NAME_DEFAULT?start=$BOOT
       -> станешь администратором бота (bootstrap-код одноразовый).
    2. Привяжи свой профиль панели: отправь боту
       /bindme andrew_main
    3. Реестр клиентов: /sync
    4. Семьи: /family <email> <семья> [head]   | обзор: /families
    5. Пригласи родных: /invite <email> -> перешли человеку ссылку из ответа.
    6. Проверка: кнопки «Моя ссылка» и «QR-код» должны дать
       $SUB_URI_DEFAULT<subId человека>

    Откат: docker compose -f $APP_DIR/docker-compose.bot.yml down
EOF
