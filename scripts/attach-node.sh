#!/usr/bin/env bash
# Подключение ноды к мастеру: регистрирует ноду и разворачивает на ней копию Reality-инбаунда
# мастера С ТЕМИ ЖЕ клиентами (одинаковые UUID/subId -> подписка клиента автоматически
# начинает содержать новый сервер). У новой ноды — своя пара ключей Reality.
#
# Запуск НА МАСТЕРЕ: bash scripts/attach-node.sh <NODE_IP> <NODE_TOKEN> [INBOUND_ID] [NAME]
#   INBOUND_ID — с какого инбаунда клонировать (по умолчанию: первый локальный vless-инбаунд).
#   NAME — читаемое имя ноды и инбаунда (напр. FR-138); по умолчанию node-<IP> / node-reality-<IP>.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "[x] Запусти от root (на мастере)" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_IP="${1:?Укажи IP ноды: bash $0 <NODE_IP> <NODE_TOKEN> [INBOUND_ID] [NAME]}"
NODE_TOKEN="${2:?Укажи API-токен ноды (см. /root/.node-info на ноде)}"
TPL_ID="${3:-}"
NODE_NAME="${4:-node-$NODE_IP}"

[[ -f /root/.master-panel ]] || { echo "[x] Нет /root/.master-panel — этот скрипт работает на мастере" >&2; exit 1; }
set -a; . /root/.master-panel; set +a

source "$SCRIPT_DIR/lib/xui-api.sh"
xui_init "$BASE" "$BASEPATH" "$USER" "$PASS" "$HOST"

# 1. Регистрация ноды (идемпотентно: при повторном запуске берём существующую)
NID=$(xui_api GET /nodes/list '' | python3 -c "
import sys,json
ip=sys.argv[1]
d=json.load(sys.stdin).get('obj') or []
for n in (d if isinstance(d,list) else []):
    if n.get('address')==ip and int(n.get('port') or 0)==2053:
        print(n.get('id')); break
" "$NODE_IP" 2>/dev/null || true)
if [[ -n "$NID" ]]; then
  echo "[i] Нода уже зарегистрирована (id $NID) — пропускаю nodes/add"
else
  R=$(xui_api POST /nodes/add "{\"name\":\"$NODE_NAME\",\"scheme\":\"http\",\"address\":\"$NODE_IP\",\"port\":2053,\"basePath\":\"\",\"apiToken\":\"$NODE_TOKEN\",\"enable\":true}")
  NID=$(printf '%s' "$R" | python3 -c "import sys,json;d=json.load(sys.stdin);o=d.get('obj') or {};print(o.get('id',''))" 2>/dev/null || true)
  [[ -n "$NID" ]] || { echo "[x] нода не добавлена: ${R:0:300}" >&2; echo "    Частая причина: UFW ноды не пускает 2053 с IP мастера (node-hardening.sh)." >&2; exit 1; }
  echo "[+] Нода зарегистрирована (id $NID)"
fi

# 2. Шаблонный инбаунд — локальный vless (без nodeId)
L=$(xui_api GET /inbounds/list '{}')
TPL_PROG="$(mktemp)"
cat > "$TPL_PROG" <<'PY'
import json,sys
want = sys.argv[1]
d = json.load(sys.stdin)["obj"]
lst = d if isinstance(d, list) else d.get("inbounds", [])
if want:
    cands = [i for i in lst if str(i.get("id")) == want]
else:
    # 1) локальный vless-инбаунд мастера; 2) иначе — копия с последней ноды
    cands = [i for i in lst if not i.get("nodeId") and i.get("protocol") == "vless"]
    if not cands:
        cands = sorted([i for i in lst if i.get("nodeId") and i.get("protocol") == "vless"],
                       key=lambda x: x.get("id", 0), reverse=True)
print(json.dumps(cands[0]) if cands else "", end="")
PY
TPL=$(printf '%s' "$L" | python3 "$TPL_PROG" "$TPL_ID")
rm -f "$TPL_PROG"

# 3. Собираем инбаунд для ноды: те же клиенты, свои ключи Reality, порт 443 (если шаблон на 8443)
KEYS=$(xui_api GET /server/getNewX25519Cert '')
PAY_PROG="$(mktemp)"
cat > "$PAY_PROG" <<'PY'
import json,sys,secrets
line = sys.stdin.readline().strip()
keys = json.loads(sys.stdin.readline())["obj"]
node_ip, nid = sys.argv[1], int(sys.argv[2])
remark = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else f"node-reality-{node_ip}"
DEFAULT_STREAM = {"network":"tcp","security":"reality","externalProxy":[],
  "realitySettings":{"show":False,"dest":"www.yahoo.com:443","xver":0,
    "serverNames":["www.yahoo.com"],"privateKey":"","shortIds":[],
    "settings":{"publicKey":"","fingerprint":"firefox","serverName":"www.yahoo.com","spiderX":"/"}},
  "tcpSettings":{"acceptProxyProtocol":False,"header":{"type":"none"}}}
def as_obj(v):
    # list отдаёт settings/streamSettings/sniffing уже распарсенными (dict), add ждёт строки
    return json.loads(v) if isinstance(v, str) else v
def as_str(v):
    return v if isinstance(v, str) else json.dumps(v)
if line:
    tpl = json.loads(line)
    stream = as_obj(tpl["streamSettings"])
    settings = as_str(tpl["settings"])
    port = 443 if tpl.get("port") == 8443 else tpl.get("port")
else:
    tpl = None; stream = DEFAULT_STREAM
    settings = json.dumps({"clients":[],"decryption":"none","fallbacks":[]})
    port = 443
rs = stream.get("realitySettings")
if rs:
    rs["privateKey"] = keys["privateKey"]
    rs["shortIds"] = [secrets.token_hex(8)]
    rs.setdefault("settings", {})["publicKey"] = keys["publicKey"]
sniff = as_str((tpl or {}).get("sniffing")) if (tpl or {}).get("sniffing") else '{"enabled":true,"destOverride":["http","tls","quic"]}'
out = {"remark": remark, "enable": True, "listen": "", "port": port,
       "protocol": "vless", "tag": f"in-node-{node_ip}-{port}",
       "settings": settings, "streamSettings": json.dumps(stream),
       "sniffing": sniff,
       "nodeId": nid}
print(json.dumps(out), end="")
PY
PAYLOAD=$(printf '%s\n%s' "$TPL" "$KEYS" | python3 "$PAY_PROG" "$NODE_IP" "$NID" "${4:-}")
rm -f "$PAY_PROG"
[[ -n "$PAYLOAD" ]] || { echo "[x] не удалось собрать конфиг инбаунда" >&2; exit 1; }
R2=$(xui_api POST /inbounds/add "$PAYLOAD")
ID2=$(printf '%s' "$R2" | python3 -c "import sys,json;d=json.load(sys.stdin);print((d.get('obj') or {}).get('id',''))" 2>/dev/null || true)
[[ -n "$ID2" ]] || { echo "[x] инбаунд на ноду не развёрнут: ${R2:0:300}" >&2; exit 1; }
echo "[+] Инбаунд развёрнут на ноде (id $ID2, порт $(printf '%s' "$PAYLOAD" | python3 -c 'import sys,json;print(json.load(sys.stdin)["port"])'))"

# 4. Проверка
sleep 10
ST=$(xui_api GET /nodes/list '')
CHK_PROG="$(mktemp)"
cat > "$CHK_PROG" <<'PY'
import sys,json
d=json.load(sys.stdin)["obj"]
for n in (d if isinstance(d,list) else []):
    if n.get("id")==int(sys.argv[1]):
        print(f"[i] нода: status={n.get('status')} latency={n.get('latencyMs')}ms xray={n.get('xrayVersion')}")
PY
printf '%s' "$ST" | python3 "$CHK_PROG" "$NID"
rm -f "$CHK_PROG"
cat <<EOF

[+] Готово. Подписки клиентов уже содержат новый сервер (те же UUID/subId);
    клиентские приложения подхватят его при ближайшем обновлении подписки.
    Проверка снаружи: tcp-доступность $NODE_IP:443 и обрыв RU-сайтов через неё.

EOF
