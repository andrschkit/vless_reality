#!/usr/bin/env bash
# Certbot DNS-01 auth-hook для Porkbun: создаёт TXT _acme-challenge.<домен>.
# Ключи читаются из /root/.porkbun-keys (формат: PORKBUN_APIKEY="pk1_...", PORKBUN_SECRET="sk1_...").
# DNS_ACCESS: у домена в Porkbun должен быть включён «API Access» (Domain Management → ⚙).
set -e
. /root/.porkbun-keys

DOMAIN="4myfamdom.xyz"   # корневая зона, в которой лежит поддомен панели
SUB="_acme-challenge.${CERTBOT_DOMAIN%.$DOMAIN}"
if [ "$SUB" = "_acme-challenge.$CERTBOT_DOMAIN" ]; then SUB="_acme-challenge"; fi

curl -sf -X POST "https://api.porkbun.com/api/json/v3/dns/create/$DOMAIN" \
  -H "Content-Type: application/json" \
  -d "{\"apikey\":\"$PORKBUN_APIKEY\",\"secretapikey\":\"$PORKBUN_SECRET\",\"name\":\"$SUB\",\"type\":\"TXT\",\"content\":\"$CERTBOT_VALIDATION\",\"ttl\":600}"

# даём DNS-серверам Porkbun время применить запись
sleep 25
