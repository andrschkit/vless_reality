#!/usr/bin/env bash
# Веб-фасад мастера: nginx (HTTPS по домену) + certbot (DNS-01 через Porkbun) + UFW.
#
# Что делает:
#   1. Ставит nginx и certbot.
#   2. Пишет серверный блок: домен → панель (по секретному пути) и подписки (по секретным путям).
#   3. Открывает 80/443 (nginx), открывает порт Reality мастера (по умолчанию 8443),
#      закрывает наружный доступ к панели (2053) — она доступна только через nginx.
#   4. Выпускает сертификат Let's Encrypt через DNS-01 (хуки porkbun-*.sh,
#      ключи из /root/.porkbun-keys). HTTP-01 не работает, если хостер
#      фильтрует входящий 80 порт для чужих сетей — поэтому DNS-01.
#
# ВАЖНО: перед запуском в панели должны быть настроены webBasePath, subPath,
# subJsonPath, subClashPath, subURI, subListen=127.0.0.1, webDomain (Panel Settings).
#
# Запуск на мастере:
#   bash scripts/setup-web.sh <ДОМЕН> <PANEL_BASE_PATH> <SUB_PATH> [SUB_JSON_PATH] [SUB_CLASH_PATH] [REALITY_PORT]
# Пример:
#   bash scripts/setup-web.sh abs.4myfamdom.xyz /dc2aa7ceebcf/ /kfaatpmh442ci4kr/ /9j7u7kjlj7t0m7nz/ /ez3yhxism2a09dfd/ 8443
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "[x] Запусти от root" >&2; exit 1; }

DOMAIN="${1:?Домен: bash $0 <домен> <panel_base_path> <sub_path> [sub_json] [sub_clash] [reality_port]}"
PANEL_PATH="${2:?Секретный путь панели (webBasePath), напр. /a9f3k7/}"
SUB_PATH="${3:?Секретный путь подписок (subPath), напр. /kfaatpmh/}"
SUB_JSON_PATH="${4:-}"
SUB_CLASH_PATH="${5:-}"
REALITY_PORT="${6:-8443}"

log() { echo -e "\033[1;32m[+]\033[0m $*"; }

log "Ставлю nginx и certbot..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx certbot curl >/dev/null 2>&1
systemctl enable --now nginx >/dev/null 2>&1

# --- конфиг nginx ---
SUB_LOCS=""
for p in "$SUB_PATH" "$SUB_JSON_PATH" "$SUB_CLASH_PATH"; do
  [ -n "$p" ] && SUB_LOCS="${SUB_LOCS}    location $p { proxy_pass http://127.0.0.1:2096; proxy_set_header Host \$host; }
"
done

CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
cat > "/etc/nginx/sites-available/$DOMAIN" <<NGINX
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name $DOMAIN;

    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location $PANEL_PATH {
        proxy_pass http://127.0.0.1:2053;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }

$SUB_LOCS
    location / { return 404; }
}
NGINX

ln -sf "/etc/nginx/sites-available/$DOMAIN" /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t >/dev/null && systemctl reload nginx
log "nginx настроен: панель $PANEL_PATH, подписки $SUB_PATH"

# --- UFW ---
log "Настраиваю UFW..."
ufw allow 80/tcp comment 'http->https' >/dev/null
ufw allow 443/tcp comment 'nginx https' >/dev/null
ufw allow "$REALITY_PORT"/tcp comment 'VLESS Reality master' >/dev/null
ufw delete allow 2053/tcp >/dev/null 2>&1 || true
log "UFW: 80, 443 (nginx), $REALITY_PORT (Reality); панель 2053 — только локально (через nginx)"

# --- сертификат ---
if [ -d "$CERT_DIR" ]; then
  log "Сертификат $DOMAIN уже есть, пропускаю выпуск."
else
  log "Выпускаю сертификат (DNS-01 через Porkbun)..."
  certbot certonly --manual --preferred-challenges dns \
    --manual-auth-hook /usr/local/bin/porkbun-auth.sh \
    --manual-cleanup-hook /usr/local/bin/porkbun-cleanup.sh \
    --manual-public-ip-logging-ok \
    -d "$DOMAIN" \
    --agree-tos --no-eff-email -m "admin@${DOMAIN#abs.}" \
    --deploy-hook "systemctl reload nginx"
  systemctl reload nginx
fi

cat <<EOF
[+] Готово.
    Панель:  https://$DOMAIN${PANEL_PATH%/}/
    Проверь: curl -I https://$DOMAIN${PANEL_PATH%/}/
    Продление: certbot.timer (systemd), хуки DNS-01 уже привязаны к сертификату.
EOF
