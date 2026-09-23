#!/usr/bin/env bash
# Установка МАСТЕРА: панель + подписки за доменом с HTTPS + RU-фильтр.
# Готовый мастер = базовый стек + доменная настройка панели + nginx/certbot (DNS-01) + RU-фильтр.
#
# Предусловия:
#   - A-запись <ДОМЕН> уже указывает на IP этого сервера;
#   - /root/.porkbun-keys с ключами API Porkbun (chmod 600):
#       PORKBUN_APIKEY="pk1_..."
#       PORKBUN_SECRET="sk1_..."
#     и у домена в Porkbun включён «API Access»;
#   - чистый сервер, 443/tcp свободен.
#
# Запуск: bash scripts/install-master.sh <ДОМЕН> [EMAIL]
# Пример: bash scripts/install-master.sh abs.4myfamdom.xyz admin@4myfamdom.xyz
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "[x] Запусти от root" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOMAIN="${1:?Укажи домен: bash $0 <домен> [email]}"
[[ "$DOMAIN" == */* || "$DOMAIN" == *..* ]] && { echo "[x] Нужен домен без путей: $DOMAIN" >&2; exit 1; }
EMAIL="${2:-admin@${DOMAIN#*.}}"
[[ -f /root/.porkbun-keys ]] || { echo "[x] Нет /root/.porkbun-keys — положи туда PORKBUN_APIKEY/PORKBUN_SECRET (chmod 600)" >&2; exit 1; }

MY_IP="$(curl -fsSL --max-time 5 -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
RESOLVED="$(dig +short "$DOMAIN" 2>/dev/null | tr '\n' ' ')"
if [[ -n "$RESOLVED" && "$RESOLVED" != *"$MY_IP"* ]]; then
  echo "[!] ВНИМАНИЕ: $DOMAIN резолвится в [$RESOLVED], а IP сервера $MY_IP."
  echo "    Если запись свежая — подожди пропагации; certbot упадёт, пока DNS не сойдётся."
  read -r -p "    Продолжить? [y/N] " a; [[ "$a" == "y" ]] || exit 1
fi

# 1. Базовый стек
bash "$SCRIPT_DIR/install.sh"

# 2. Свои логин/пароль панели
apt-get install -y -qq sqlite3 apache2-utils >/dev/null 2>&1
NEWUSER="adm$(openssl rand -hex 3)"
NEWPASS="$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c 20)"
HASH="$(htpasswd -bnBC 10 "" "$NEWPASS" | tr -d ':\n' | sed 's/^\$2y/\$2a/')"
sqlite3 /opt/3x-ui/db/x-ui.db "update users set username='$NEWUSER', password='$HASH' where id=1;"
docker restart 3xui_app >/dev/null
for i in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:2053/ && break; sleep 2; done

# 3. Настройки панели: base path, домен, подписки на localhost (webDomain — в ТОМ ЖЕ update!)
NBP="/$(openssl rand -hex 6)/"
source "$SCRIPT_DIR/lib/xui-api.sh"
xui_init "http://127.0.0.1:2053" "/" "$NEWUSER" "$NEWPASS"

CUR=$(xui_api POST /setting/all '{}')
MERGE_PROG="$(mktemp)"
cat > "$MERGE_PROG" <<'PY'
import json,sys
d = json.load(sys.stdin)["obj"]
d["webBasePath"] = sys.argv[1]
d["webDomain"] = sys.argv[2]
d["subListen"] = "127.0.0.1"
d["subURI"] = "https://" + sys.argv[2] + d["subPath"].rstrip("/")
json.dump(d, open("/tmp/master_settings.json","w"))
print("subPath:", d["subPath"], "| subJsonPath:", d["subJsonPath"], "| subClashPath:", d["subClashPath"], file=sys.stderr)
PY
printf '%s' "$CUR" | python3 "$MERGE_PROG" "$NBP" "$DOMAIN"
rm -f "$MERGE_PROG"
xui_api POST /setting/update "$(cat /tmp/master_settings.json)" | grep -q '"success":true' \
  || { echo "[x] не удалось сохранить настройки панели" >&2; exit 1; }
xui_api POST /setting/restartPanel >/dev/null
for i in $(seq 1 30); do curl -sf -o /dev/null -H "Host: $DOMAIN" "http://127.0.0.1:2053$NBP" && break; sleep 2; done

# 4. RU-фильтр (запросы теперь с Host, иначе пустой 403 — грабля из AGENTS.md)
xui_init "http://127.0.0.1:2053" "$NBP" "$NEWUSER" "$NEWPASS" "$DOMAIN"
FILTER_RESP=$(xui_apply_ru_filter "$SCRIPT_DIR/ru-filter-rules.json")
printf '%s' "$FILTER_RESP" | grep -q '"success":true' || { echo "[x] RU-фильтр не применён: ${FILTER_RESP:0:200}" >&2; exit 1; }

# 5. Хуки certbot + веб-фасад (nginx, UFW, сертификат DNS-01)
install -m 700 "$SCRIPT_DIR/certbot/porkbun-auth.sh" /usr/local/bin/porkbun-auth.sh
install -m 700 "$SCRIPT_DIR/certbot/porkbun-cleanup.sh" /usr/local/bin/porkbun-cleanup.sh
SUB_PATH="$(printf '%s' "$CUR" | python3 -c "import sys,json;print(json.load(sys.stdin)['obj']['subPath'])")"
SUB_JSON="$(printf '%s' "$CUR" | python3 -c "import sys,json;print(json.load(sys.stdin)['obj']['subJsonPath'])")"
SUB_CLASH="$(printf '%s' "$CUR" | python3 -c "import sys,json;print(json.load(sys.stdin)['obj']['subClashPath'])")"
bash "$SCRIPT_DIR/setup-web.sh" "$DOMAIN" "$NBP" "$SUB_PATH" "$SUB_JSON" "$SUB_CLASH" 8443

# 6. Проверка и карточка мастера
code=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN${NBP%/}/")
[[ "$code" == "200" ]] || { echo "[x] панель не отвечает по https://$DOMAIN$NBP (HTTP $code)" >&2; exit 1; }

cat > /root/.master-panel <<EOF
BASE=http://127.0.0.1:2053
BASEPATH=$NBP
USER=$NEWUSER
PASS=$NEWPASS
HOST=$DOMAIN
EOF
chmod 600 /root/.master-panel

cat <<EOF

[+] Мастер готово: https://$DOMAIN${NBP%/}/
    Логин/пароль панели: $NEWUSER / $NEWPASS
    Подписки: https://$DOMAIN${SUB_PATH%/}/<subId>
    Карточка мастера: /root/.master-panel (chmod 600)
    Проверка извне: curl -I https://$DOMAIN${NBP%/}/

    Дальше вручную (UI панели):
      1. Включи 2FA (Settings).
      2. Подключи первую ноду: bash scripts/attach-node.sh <NODE_IP> <TOKEN>
         (инбаунд создастся сам, клиентов добавишь в него; следующие ноды клонируют его).
      3. Родные добавляются как клиенты с уникальным subId в инбаунд ноды.

EOF
