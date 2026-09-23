#!/usr/bin/env bash
# Библиотека для работы с API панели 3x-ui (проверено на 3.8.5).
# Скрывает грабли, найденные на живой системе:
#   - CSRF: токен из <meta name="csrf-token"> или GET <base>panel/csrf-token,
#     на всех мутациях заголовок X-CSRF-Token, иначе пустой 403 без логов;
#   - сессионная кука 3x-ui HttpOnly -> храним jar в файле, достаём значение grep'ом;
#   - при webDomain != "" панель отвечает ТОЛЬКО на Host: <webDomain>;
#   - лимитер: 5 неверных паролей на (IP, username) за 5 мин -> бан 15 мин
#     (не successes, не CSRF-отказы) — логинимся один раз и шарим сессию.
#
# Использование:
#   source "$(dirname "$0")/lib/xui-api.sh"
#   xui_init "http://127.0.0.1:2053" "/dc2aa7ceebcf/" "user" "pass" "abs.4myfamdom.xyz"
#   xui_api POST /inbounds/list '{}'
#   xui_apply_ru_filter "$(dirname "$0")/ru-filter-rules.json"

XUI_JAR=""; XUI_BASE=""; XUI_BASEPATH="/"; XUI_CK=""; XUI_HOST=""

xui_init() {
  XUI_BASE="${1:?base url, напр. http://127.0.0.1:2053}"
  XUI_BASEPATH="${2:-/}"
  local user="${3:?username}" pass="${4:?password}"
  XUI_HOST="${5:-}"
  XUI_JAR="$(mktemp)"
  local h=() html csrf code
  [[ -n "$XUI_HOST" ]] && h=(-H "Host: $XUI_HOST")
  html=$(curl -sf "${h[@]}" -c "$XUI_JAR" -b "$XUI_JAR" "$XUI_BASE$XUI_BASEPATH") \
    || { echo "[x] панель не отвечает: $XUI_BASE$XUI_BASEPATH" >&2; return 1; }
  csrf=$(printf '%s' "$html" | grep -oE 'csrf-token" content="[^"]+' | cut -d'"' -f3)
  [[ -n "$csrf" ]] || { echo "[x] CSRF-токен не найден на странице логина" >&2; return 1; }
  code=$(curl -s "${h[@]}" -c "$XUI_JAR" -b "$XUI_JAR" -X POST \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
    -d "{\"username\":\"$user\",\"password\":\"$pass\"}" \
    "$XUI_BASE${XUI_BASEPATH}login" -o /dev/null -w "%{http_code}")
  [[ "$code" == "200" ]] || { echo "[x] логин не удался (HTTP $code)" >&2; return 1; }
  XUI_CK=$(grep "3x-ui" "$XUI_JAR" | awk '{print $7}')
  [[ -n "$XUI_CK" ]] || { echo "[x] сессионная кука не получена" >&2; return 1; }
}

xui_csrf() {
  local h=()
  [[ -n "$XUI_HOST" ]] && h=(-H "Host: $XUI_HOST")
  curl -s "${h[@]}" -H "Cookie: 3x-ui=$XUI_CK" \
    "$XUI_BASE${XUI_BASEPATH}panel/csrf-token" | grep -oE '"obj":"[^"]+"' | cut -d'"' -f4
}

# xui_api <METHOD> <путь после /panel/api> [json-тело]
xui_api() {
  local m="${1:?method}" p="${2:?path}" body="${3:-{}}"
  local t; t=$(xui_csrf)
  local h=(-H "Cookie: 3x-ui=$XUI_CK" -H "X-CSRF-Token: $t" -H "Content-Type: application/json")
  [[ -n "$XUI_HOST" ]] && h+=(-H "Host: $XUI_HOST")
  curl -s "${h[@]}" -X "$m" -d "$body" "$XUI_BASE${XUI_BASEPATH}panel/api$p"
}

# Вливает RU-фильтр в шаблон xray текущей панели. Аргумент — json-файл с массивом правил.
xui_apply_ru_filter() {
  local rules_file="${1:?файл с правилами}" cur new t prog
  prog="$(mktemp)"
  cat > "$prog" <<'PY'
import json,sys
rules_new = json.load(open(sys.argv[1]))
d = json.loads(json.load(sys.stdin)["obj"])
cfg = d["xraySetting"]
rules = cfg.setdefault("routing", {}).setdefault("rules", [])
rules[:] = [x for x in rules if not (x.get("outboundTag") == "blocked"
            and ("geosite_RU" in json.dumps(x) or "geoip_RU" in json.dumps(x)))]
pos = 1 if rules and rules[0].get("outboundTag") == "api" else 0
rules[pos:pos] = rules_new
print(json.dumps(cfg, ensure_ascii=False))
PY
  cur=$(xui_api POST /xray/ '{}')
  new=$(printf '%s' "$cur" | python3 "$prog" "$rules_file")
  rm -f "$prog"
  t=$(xui_csrf)
  local h=(-H "Cookie: 3x-ui=$XUI_CK" -H "X-CSRF-Token: $t")
  [[ -n "$XUI_HOST" ]] && h+=(-H "Host: $XUI_HOST")
  curl -s "${h[@]}" -X POST --data-urlencode "xraySetting=$new" \
    "$XUI_BASE${XUI_BASEPATH}panel/api/xray/update"
}
