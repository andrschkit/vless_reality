#!/usr/bin/env bash
# Certbot DNS-01 cleanup-hook для Porkbun: удаляет TXT _acme-challenge.<домен>.
set -e
. /root/.porkbun-keys

DOMAIN="4myfamdom.xyz"
SUB="_acme-challenge.${CERTBOT_DOMAIN%.$DOMAIN}"
if [ "$SUB" = "_acme-challenge.$CERTBOT_DOMAIN" ]; then SUB="_acme-challenge"; fi

curl -sf -X POST "https://api.porkbun.com/api/json/v3/dns/delete/$DOMAIN/TXT/$SUB" \
  -H "Content-Type: application/json" \
  -d "{\"apikey\":\"$PORKBUN_APIKEY\",\"secretapikey\":\"$PORKBUN_SECRET\"}" || true
