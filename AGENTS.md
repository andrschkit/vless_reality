# AGENTS.md — операционные инструкции проекта vless_reality

> Этот файл коммитится в Git и должен оставаться безопасным для публикации. Не добавляй сюда
> IP-адреса, hostname, реальные домены, скрытые пути панели и подписок, UUID, subId, Reality shortId/ключи,
> имена клиентов, логины, пароли, токены и Telegram-идентификаторы.
>
> Фактические значения хранятся локально в `AGENTS.secrets.md`. Этот файл указан в `.gitignore` и не коммитится.
> Перед работой с реальным развёртыванием прочитай его. Не копируй его содержимое в README, скрипты,
> коммиты и чаты с третьими лицами.
>
> Язык проекта — русский; все скрипты и README тоже на русском.

## 1. Что это за проект

Семейный VPN на VLESS+Reality (Xray) с панелью 3x-ui в Docker. Репозиторий — инструменты развёртывания
(install/backup/restore/update + nginx/certbot для доменного режима). Полная документация для человека — в `README.md`,
этот файл — для агента: архитектура, рецепты и грабли API 3x-ui. Доступы и фактическая топология — в `AGENTS.secrets.md`.

## 2. Развёртывание (актуальное состояние)

Актуальные IP-адреса, hostname, версии ПО и открытые порты каждого сервера перечислены в `AGENTS.secrets.md`.
Общая архитектура:

- **Мастер — чистый control plane**: панель + подписки, VPN-инбаундов нет. Его IP не виден клиентам
  и не грузится VPN-трафиком. Bans прилетают нодам.
- **Ноды**: Reality на порту 443, управляются мастером. Панель ноды доступна мастеру на порту 2053;
  снаружи этот порт закрыт UFW для всех, кроме IP мастера.
- На мастере наружу смотрят только SSH, HTTP и HTTPS (nginx). Панель и sub-сервер слушают localhost.
- Синхронизация нод осуществляется мастером через API с Bearer-токеном. Токены хранятся в БД мастера
  в plaintext (`sqlite3 /opt/3x-ui/db/x-ui.db "SELECT name,api_token FROM nodes;"`) — если токен потерян,
  достань его оттуда или создай новый в панели ноды и обнови ноду в мастере.
- Версии мастера и нод могут временно различаться из-за установки по `image:latest`; еженедельный автоапдейт
  (`/etc/cron.d/3x-ui-update`) со временем выравнивает версии. Проверенная совместимость зафиксирована в секретной заметке.

### Домен и сертификат

Домен и его A-запись указаны в `AGENTS.secrets.md`. Используется сертификат Let's Encrypt, **DNS-01**
(HTTP-01 не работает: хостер фильтрует входящий порт 80 для чужих сетей). DNS у Porkbun.
Автопродление: `certbot.timer`, dry-run проверен.

### Ключевые настройки панели мастера

Значения `webDomain`, `webBasePath`, `subPath`, `subURI`, `subJsonPath` и `subClashPath` хранятся в `AGENTS.secrets.md`.

- `subListen = 127.0.0.1`: sub-сервер только localhost, наружу — через nginx.
- Слэш в конце `subURI` ОБЯЗАТЕЛЕН: панель клеит ссылку как `subURI + subId` без разделителя,
  без слэша QR/ссылки дают 404.

### Инбаунды

Фактическая таблица инбаундов (id, remark, адреса нод, dest/SNI, shortId и pubkey) находится в `AGENTS.secrets.md`.

Dest/SNI разнесены по нодам против шаблонности: у каждой ноды свой домен-прикрытие.
Правила выбора dest: иностранный крупный сайт, TLS 1.3, отвечает с ноды (проверка `curl -sI --tlsv1.3 --tls-max 1.3`),
**serverNames строго = dest** (иначе проба с чужим SNI покажет несовпадающий сертификат — палево). Избегать
www.microsoft.com и www.apple.com: xray сам пишет warning, что они пересвечены как Reality-target
(см. логи ноды после смены dest: `docker logs 3xui_app | grep -i reality`). Смена dest — через `POST /inbounds/update/<id>`
(грабля 16; правим dest + serverNames + realitySettings.settings.serverName, остальное не трогаем); клиенты получают новый sni
через подписку — до её обновления в приложении нода для клиента недоступна. Проверка снаружи:
`openssl s_client -connect <IP>:443 -servername <домен>` должен показать настоящий сертификат этого домена.
**Домен не должен быть заблокирован в РФ**: SNI от клиента идёт открытым текстом через DPI его провайдера,
а соединение с заблокированным доменом режется.

Нейминг: `<СТРАНА>-<последний октет IP>` — одинаково в remark инбаунда (виден клиентам
в подписке как `#<REMARK>-<email>`) и в имени ноды (список Nodes). Переименование: `POST /inbounds/update/<id>`
(поле remark) и `POST /nodes/update/<id>` (поле name; из тела убрать runtime-поля status/latency/...).
Гео проверять фактически: `curl ip-api.com/json/<IP>` — но geo-IP базы тоже ошибаются. При расхождении
с панелью хостера — верить хостеру. Известные расхождения зафиксированы в `AGENTS.secrets.md`.

Инбаунд мастера удалён; в sub клиентов попадают только ноды. Скрытие через hosts/is_hidden
НЕ работает — is_hidden скрывает лишь оверрайд-эндпоинты, а не сам инбаунд.
`attach-node.sh` клонирует конфиг с последнего инбаунда ноды (либо создаёт дефолтный reality-инбаунд,
если инбаундов ещё нет). Реальные серверы проверяются e2e-тестом через инбаунд НОДЫ (см. «RU-фильтр»).

Используется vless + xtls-rprx-vision, uTLS **firefox** (почему не chrome — грабля 13). UUID, subId
и ссылка тестового клиента указаны в `AGENTS.secrets.md`. Добавляя родственников — копируй этот паттерн:
тот же subId у клиента во всех инбаундах.

### Клиенты

Фактический реестр клиентов и их subId находится в `AGENTS.secrets.md`. Ссылка формируется как `subURI + subId`.

Все клиенты: flow xtls-rprx-vision, безлимит (`total=0`, `expiry=0`), один UUID и subId во всех инбаундах.
Массовое добавление выполняется через API `POST /panel/api/clients/bulkCreate`
(множественное «clients»! массив `{client:{email,id,subId,flow,enable,...}, inboundIds:[...]}`; один вызов на всех,
панель сама фан-аутит пуш на ноды). **После добавления клиенты в БД нод доезжают сами, а в рантайм-конфиг xray — нет:
нужен `docker restart 3xui_app` на КАЖДОЙ ноде** (та же грабля 17; проверка: `docker exec 3xui_app grep -o <email> /app/bin/config.json`).
e2e-рецепт нового клиента (с мастера): `docker run -d --network host --entrypoint /app/bin/xray-linux-amd64 -v cfg:/tmp/e2e.json ghcr.io/mhsanaei/3x-ui:latest run -c /tmp/e2e.json`
(бинарь — `xray-linux-amd64`, не `xray`; entrypoint образа игнорирует команду и поднимает панель) + socks на 10808 → curl.

### RU-фильтр (жёсткий блок на серверах)

В xray-конфиг серверов добавлены routing-правила (идут сразу после api-правила):

- домены `ext:geosite_RU.dat:category-ru|category-bank-ru|category-gov-ru|mailru|tbank-ru` → `blocked`
- IP `ext:geoip_RU.dat:ru` → `blocked`

RU-сервисы через VPN не открываются вообще (защита от раскрытия IP VPN-сервера). Работает за счёт сниффинга
(http/tls/quic) в инбаундах. Файлы `geoip_RU.dat`/`geosite_RU.dat` лежат в `/app/bin/` контейнера и
обновляются кнопкой «Update Geo Files» в панели. Проверено e2e (xray-клиент в контейнере): google — 204,
ya.ru/vk.com/gosuslugi — обрыв.

**ВАЖНО:** шаблон xray на ноды НЕ синхронизируется с мастером — если правишь правила, правь КАЖДУЮ ноду отдельно
(её API, тот же `xui_apply_ru_filter`). Новая нода скриптом фильтр получает сама: `install-node.sh` вливает его на шаге 4;
`attach-node.sh` шаблон не трогает (он про инбаунд). Ручное повторение нужно, только если нода ставилась не скриптом.

Как редактировать: `GET`-аналог `POST /panel/api/xray/` → правишь `routing.rules` →
`POST /panel/api/xray/update` (form-поле `xraySetting`, весь конфиг целиком; панель валидирует и сама
рестартит xray). Категории гео-баз: `GET /panel/api/xray/geodata/categories?file=geosite_RU.dat&q=ru`.

**Исключение TRMobile:** на выбранных нодах (их список — в `AGENTS.secrets.md`) в `routing.rules`
ПЕРЕД RU-блоком (позиция 1, сразу после api-правила) стоит whitelist:
`{"type":"field","domain":["domain:tr-mobile.ru","domain:tr-portal.ru"],"outboundTag":"direct"}` —
покрывает `api.tr-mobile.ru` и `backend-monitoring.tr-portal.ru`. Остальной RU там по-прежнему блокируется.
`xui_apply_ru_filter` это правило НЕ затирает (снимает только blocked-правила с geosite_RU/geoip_RU),
но `install-node.sh` его не ставит: при переустановке/замене такой ноды — добавлять заново вручную.
Бэкапы xray-конфига до изменения лежат на соответствующих нодах в `/root/xray-backup-20261007-*.json`.

## 3. Доступы

Все реальные адреса, URL, логины и пароли находятся в `AGENTS.secrets.md`.

- **SSH с устройства пользователя** (Termux) и **с ПК пользователя**: `ssh -i ~/.ssh/vless_deploy root@<IP>` —
  ключ уже на всех серверах. Root-пароли хранятся только в секретной заметке; сменить их при первой возможности.
- **Панель мастера** доступна по HTTPS через домен и скрытый base path.
- **Панель ноды** доступна только с мастера через SSH-туннель:
  `ssh -L <LOCAL_PORT>:127.0.0.1:2053 root@<NODE_IP>`, далее `http://127.0.0.1:<LOCAL_PORT>/`.
  Напрямую с устройства порт 2053 закрыт UFW. Карточка ноды: `/root/.node-info` (там же API-токен мастера).
- **Porkbun API**: `/root/.porkbun-keys` на мастере (chmod 600), хуки в `/usr/local/bin/porkbun-{auth,cleanup}.sh`.
  У домена включён «API Access» (если перенесёшь домен — включать заново).
- Локальные копии: на серверах лежат снапшоты репо в `/root/vless_reality`, живые скрипты — `/opt/3x-ui/scripts`.

## 4. Работа с API панели (3x-ui v3.8.x) — читать перед любыми вызовами

База: `/panel/api/*` (session-cookie ИЛИ `Authorization: Bearer <api-token>`). Страницы SPA — GET, почти всё
прикладное API — **POST**. Полный список маршрутов: `internal/web/controller/{setting,inbound,node,server,api}.go`
в репозитории MHSanaei/3x-ui (тег v3.8.5).

### Логин (session flow)

Ниже плейсхолдеры; фактические `BASE` и `PANEL_DOMAIN` возьми из `AGENTS.secrets.md`.

```bash
B=http://127.0.0.1:2053             # либо HTTPS-URL мастера через nginx
BASE=/replace-with-web-base-path/   # webBasePath, со слэшами
PANEL_DOMAIN=panel.example.com      # настроенный webDomain
CSRF=$(curl -s -c /tmp/jar -b /tmp/jar "$B$BASE" | grep -oE 'csrf-token" content="[^"]+' | cut -d'"' -f3)
curl -s -c /tmp/jar -b /tmp/jar -X POST -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
  -d '{"username":"...","password":"..."}' "$B${BASE}login"          # 200 + Set-Cookie 3x-ui (HttpOnly!)
CK=$(grep "3x-ui" /tmp/jar | awk '{print $7}')                       # парсится ВРУЧНУЮ: HttpOnly-строки
T=$(curl -s -H "Cookie: 3x-ui=$CK" "$B${BASE}panel/csrf-token" | grep -oE '"obj":"[^"]+"' | cut -d'"' -f4)
api() { curl -s -H "Host: $PANEL_DOMAIN" -X "$1" -H "Cookie: 3x-ui=$CK" \
        -H "X-CSRF-Token: $T" -H "Content-Type: application/json" -d "${3:-{}}" "$B${BASE}panel/api$2"; }
api POST /setting/all | head -c 200
```

### Грабли (проверены на живой системе — не наступать повторно)

1. **Все мутации требуют CSRF**: токен из `<meta name="csrf-token">` страницы логина или из `GET <base>panel/csrf-token`,
   заголовок `X-CSRF-Token`. Без него — пустой 403, без записи в логах.
2. **webDomain-блокировка**: при `webDomain != ""` панель отвечает ТОЛЬКО на `Host` с настроенным доменом.
   Локальные curl'ы на 127.0.0.1 без `-H "Host: ..."` получат пустой 403. Работай через nginx-URL или с заголовком.
3. **Лимитер логинов**: 5 неверных паролей на (IP, username) за 5 мин → блок 15 мин (in-memory, сбрасывается
   перезапуском контейнера). Отвергнутые CSRF-запросы НЕ считаются. Ответ лимитера — 200 `success:false`, не 403.
4. **Cookie jar**: сессия `3x-ui` — HttpOnly, в Netscape-формате она строкой `#HttpOnly_...` — `awk '{print $7}'`
   по grep '3x-ui' работает, `grep -v '^#'` её «съест».
5. **Методы list-эндпоинтов — GET, не POST** (проверено на v3.8.5): `GET /inbounds/list`, `GET /nodes/list`,
   `GET /server/getNewX25519Cert` отдают JSON; POST на них — 404. И наоборот: **`/panel/api/setting/all` —
   только POST** с телом `{}` (GET → 404).
6. **restartPanel** — перезапуск внутри процесса: контейнер НЕ пересоздаётся (uptime сохраняется), сессия живёт,
   base path перечитывается.
7. **Изменение base path** делается через `POST /panel/api/setting/update` (полный объект настроек:
   `GET all` → правка ключей → `POST update`), затем `restartPanel`. Прямой INSERT webBasePath в БД не работает —
   в v3 ключ живёт не в таблице settings.
8. **Смена пароля панели**: `POST /panel/api/setting/updateUser {username,password}` или напрямую в sqlite:
   bcrypt через `htpasswd -bnBC 10 "" 'PASS'`, заменить `$2y$`→`$2a$`, `UPDATE users`, затем `docker restart 3xui_app`.
9. **Инбаунд**: `model.Inbound` — `settings`, `streamSettings`, `sniffing` это **строки** с xray-JSON внутри;
   деплой на ноду — поле `nodeId` (int) в теле `POST /inbounds/add`. Ключи Reality: `GET /panel/api/server/getNewX25519Cert`.
   **НО**: `GET /inbounds/list` возвращает эти поля уже **распарсенными (dict)** — перед повторным использованием
   в `add` сериализуй обратно через json.dumps (`attach-node.sh` это делает, см. as_obj/as_str).
10. **Добавление ноды**: `POST /panel/api/nodes/add` `{name, scheme:"http", address, port:2053, basePath:"", apiToken, enable:true}`.
    Мастер сразу проверяет доступность (ensureReachable) — UFW ноды должен пускать 2053 с IP мастера ДО добавления.
11. Сервер не всегда может достучаться до самого себя по публичному IP (hairpin) — проверяй HTTPS снаружи,
    а не с самого сервера.
12. `backup.sh` делает `docker compose stop/start` — сессии панели умирают. Если API вдруг начал отдавать
    пустые 403 сразу после бэкапа — не лимитер, просто перелогинься.
13. **Мобильные/GUI-клиенты (Happ, V2RayTun) vs Reality: «пинг есть, интернета нет»**:
    - Симптом: пинг сервера в приложении нормальный, но трафик не идёт. Причина «пинга»: Reality при любой
      неудаче релеит соединение на настоящий dest — телефон говорит TLS с этим сайтом через наш сервер.
    - **Happ: настройка «Фрагментация» должна быть ВЫКЛЮЧЕНА** — фрагментированный hello серверный Reality
      не парсит → `failed to read client hello`.
    - Даже целый hello с fp=chrome (~1800 байт, MLKEM) на RU-трассах (дом/мобила) часто приходит БЕЗ второго
      TCP-сегмента → тот же error → fallback. Лечение клиентом: **fp=firefox/safari** (hello ~600 байт,
      один сегмент) — после смены стабильно. Чистый xray.exe с ПК при этом работал со всеми fp —
      для A/B-тестов удобно держать под рукой.
    - Диагностика: debug-лог на ноде (xray/update log.loglevel=debug, потом вернуть warning) +
      tcpdump: релей виден как исходящие SYN к IP dest; `failed to read client hello` =
      hello не дошёл/не распарсился; `received request for` + `freedom: connection opened` = успех.
    - **РЕШЕНО на сервере**: в инбаундах fingerprint сменён chrome→firefox (realitySettings.settings
      + top-level), поэтому подписка сразу выдаёт ссылки с `fp=firefox` — клиентам ничего менять не надо.
      `attach-node.sh` это учитывает: при клонировании копирует шаблон целиком (там уже firefox), а в его
      дефолтном инбаунде (DEFAULT_STREAM, когда клонировать нечего) тоже стоит firefox.
14. **Клиент «выключен» = enable живёт в ДВУХ местах**:
    у клиента есть `client_traffics.enable` (таблица) и `enable` внутри `inbounds.settings` JSON —
    для генерации xray-конфига важен **второй**. Если клиент отключён в settings JSON, в логах ноды будет
    `Remove Inbound User <email> due to expiration or traffic limit`, а Reality-хендшейк молча не завершится.
    Проверка рантайма: `docker exec 3xui_app cat /app/bin/config.json` на ноде — клиент должен быть в
    `settings.clients`. Лечение: `POST /inbounds/update/<id>` на мастере с `enable:true` у клиента в settings
    (пуш доезжает до ноды, но рантайм перегенерируется с задержкой — для верности `docker restart 3xui_app`
    на ноде). Перед e2e всегда проверять, что тестовый клиент включён.
15. **Имя ссылки в подписке = настройка `remarkTemplate`, email — только в первой ссылке**:
    шаблон был `{{INBOUND}}-{{EMAIL}}|📊{{TRAFFIC_LEFT}}|⏳{{DAYS_LEFT}}D`; панель рендерит email/usage-токены
    ТОЛЬКО в первой ссылке подписки (`firstLinkOnlyBodyTokens`), в остальных вырезает. Шаблон сменён на
    `{{INBOUND}}|📊{{TRAFFIC_LEFT}}|⏳{{DAYS_LEFT}}D` (email убран; usage-сегменты пустые при total=0/expiry=0
    и вырезаются сами). **ВНИМАНИЕ: если очистить шаблон совсем, сработает fallback `remark-email` и email
    появится в КАЖДОЙ ссылке.** Грабля: sub-сервер (порт 2096) читает remarkTemplate один раз при старте (sub.go) —
    после `setting/update` нужен `POST /panel/api/setting/restartPanel`, иначе подписка отдаёт старые имена.
    `subShowIdentityOnAllLinks=true` делал бы обратное (email во всех ссылках).
16. **Смена инбаунда в v3.8.5: `POST /inbounds/update/<id>`** — id обязателен В ПУТИ, тело — полный объект
    инбаунда (settings/streamSettings/sniffing — строками, грабля 9). `POST /inbounds/update` без id —
    пустой 404 без записи в логах. Аналогично удаление — `/inbounds/del/<id>`.
17. **Пуш на ноду ≠ рантайм-конфиг ноды**: мастер пишет изменения в БД ноды, а xray на ноде конфиг НЕ
    перегенерирует — e2e молча не коннектится. Лечение одно: `docker restart 3xui_app` на ноде (дальше всё
    подхватывается). Проверка рантайма: `docker exec 3xui_app grep -o '"email": "test"' /app/bin/config.json`
    (для dest — grep домена) — config.json pretty-printed С ПРОБЕЛОМ после двоеточия, паттерн без пробела
    молча промахивается.

## 5. Рецепты

### Развёртывание мастера с нуля (скриптом)

Спроси у пользователя: домен (A-запись → IP сервера!) и ключи Porkbun (положить в `/root/.porkbun-keys`).
Дальше на чистом сервере: `bash scripts/install-master.sh <домен> [email]`.
Скрипт сам: базовый стек → логин/пароль/base path → доменные настройки (webDomain+subListen+subURI
в ОДНОМ update — иначе грабля Host-заголовка) → RU-фильтр → nginx + сертификат DNS-01 → UFW.
Итог печатается в конце + карточка `/root/.master-panel` (600). Останется вручную: 2FA, первая нода
через `attach-node.sh` (инбаунд создастся сам), клиенты.

### Развёртывание ноды с нуля + attach (скриптами)

1. Спроси у пользователя: IP нового сервера и root-пароль. Доступ: `sshpass -p '<пароль>' ssh -o StrictHostKeyChecking=accept-new root@<IP>`,
   поставь ключ `~/.ssh/vless_deploy` (ssh-copy-id), как при первом развёртывании.
   На ПК пользователя (Windows/Git Bash) sshpass НЕТ — обход: скрипт-askpass (`printf '#!/bin/bash\necho "PASS"' > /tmp/ap.sh; chmod +x /tmp/ap.sh`)
   + `SSH_ASKPASS=/tmp/ap.sh SSH_ASKPASS_REQUIRE=force DISPLAY=:0 ssh -o PreferredAuthentications=password ...`; после — удали /tmp/ap.sh.
   Если на ПК нет `~/.ssh/vless_deploy.pub` — сгенерируй: `ssh-keygen -y -f ~/.ssh/vless_deploy > ~/.ssh/vless_deploy.pub`.
   Свежий VPS может ещё не поднять sshd (port 22 closed) — подожди пару минут и повтори.
2. Скопируй репо, исключив секретную заметку: `tar czf - --exclude=.git --exclude=AGENTS.secrets.md . | ssh -i ~/.ssh/vless_deploy root@<IP> 'mkdir -p /root/vless_reality && tar xzf - -C /root/vless_reality'`
3. На ноде: `bash /root/vless_reality/scripts/install-node.sh <MASTER_IP>`
   → сам всё настроит и напечатает (и запишет в `/root/.node-info`): логин/пароль панели ноды и **API-токен**.
   На только что включённом VPS может упасть с `Could not get lock /var/lib/dpkg/lock-frontend` (cloud-init/unattended-upgrades
   ещё работают) — подожди (`while fuser /var/lib/dpkg/lock-frontend; do sleep 5; done`) и перезапусти скрипт, он идемпотентен.
4. На мастере: `bash /root/vless_reality/scripts/attach-node.sh <NODE_IP> <TOKEN> [INBOUND_ID] [NAME]` — регистрирует ноду,
   клонирует на неё локальный vless-инбаунд с ТЕМИ ЖЕ клиентами (новая пара ключей Reality, порт 443).
   NAME в формате `<СТРАНА>-<последний октет IP>` задаёт и имя ноды, и remark инбаунда; без INBOUND_ID передавай `""` третьим аргументом.
   Скрипт идемпотентен: повторный запуск находит ноду по IP и не дублирует её.
   Подписки клиентов подхватывают сервер сами.
5. Проверь рантайм ноды (грабля 17): клиенты и dest в `/app/bin/config.json`, при отсутствии — `docker restart 3xui_app`.
   e2e через ноду (с мастера): google — 204, ya.ru — обрыв.

Типовые фразы пользователя: «запусти ноду: IP x, пароль y» → шаги 1–4. «запусти мастера: домен z» → рецепт выше.

### Добавить родственника

В панели мастера (UI проще, чем API): Inbounds → любой инбаунд → Add Client: email = имя, уникальный subId (hex),
flow xtls-rprx-vision (не оставлять пустым!), и кнопка **«Привязанные входящие» → выбрать все** — клиент создастся
сразу во всех инбаундах (все ноды) с одним subId. Дать ссылку: клиенты → иконка подписки —
она имеет вид `<SUB_URI><subId>`; фактический `SUB_URI` указан в `AGENTS.secrets.md`.

### Нода заблокирована → новая нода

То же, что «Развёртывание ноды с нуля + attach» выше. После attach удали старую ноду в панели
(Nodes → delete): её инбаунд исчезнет из подписок клиентов.

### Заблокирован IP мастера

1. Новый VPS: `install.sh` + `setup-web.sh` (хуки и ключи Porkbun перенести с `/root` старого сервера; при смене домена —
   новый выпуск сертификата).
2. В Porkbun поменять A-запись используемого поддомена на новый IP.
3. Восстановить панель из бэкапа (`scripts/restore.sh`, см. README) или пересоздать инбаунды.
   Sub-ссылки с доменом переживают смену IP без перераздачи.

### Бэкап / восстановление / обновление

Как в README (разделы 6–8). Бэкап-скрипт останавливает контейнер на 2–3 с. Ежедневный cron-бэкап на мастере
настроен: `/etc/cron.d/3x-ui-backup` (04:17, лог `/var/log/3x-ui-backup.log`), KEEP=7 (≈неделя; архив ~190 КБ).
Еженедельное автообновление панели стоит на всех серверах (`/etc/cron.d/3x-ui-update`).

### Сертификат

`certbot renew --dry-run` — проверка; `certbot renew` — ручное продление. Хуки и deploy-hook (reload nginx)
привязаны к сертификату в `/etc/letsencrypt/renewal/`.

### «Везде 403 и пустые ответы» — чеклист

Host-заголовок = webDomain? CSRF свежий? Лимитер (подожди 15 мин / перезапусти контейнер)? Сессия не протухла
(Max-Age = sessionMaxAge минут)?

## 6. Телеграм-бот семьи (MVP)

Имя и username бота, а также администратор указаны в `AGENTS.secrets.md`. Бот на мастере выдаёт привязанным
пользователям их sub-ссылку/QR, показывает живость нод; глава семьи видит ссылки членов. Мутаций клиентов НЕ делает (фаза 2).

- **Деплой:** контейнер `tg-vpn-bot` (compose project `tg-vpn-bot`!), файл `/opt/3x-ui/docker-compose.bot.yml`,
  код `/opt/3x-ui/bot/` (копия из репо `bot/`). Переустановка/обновление: `bash scripts/install-bot.sh`
  (идемпотентен; `bot.env` и `bot.db` сохраняются). Откат: `docker compose -f /opt/3x-ui/docker-compose.bot.yml down`.
- **Секреты:** токен бота — `/opt/3x-ui/bot/tg-bot-token` (chmod 400; источник для копирования `/root/.tg-bot-token` 600).
  В заметки токен НЕ дублировать. `bot.env` (600): `ADMIN_BOOTSTRAP` (одноразовый код первого входа админа),
  `SUB_URI`, `INVITE_TTL_DAYS=7`. БД бота `/opt/3x-ui/bot/bot.db`: users(tg_id→email), clients(email→family/head),
  invites, pending_bind, audit. Просмотр реестра: `/clients`.
- **Семьи назначаются ВРУЧНУЮ**: `/family <email> <семья> [head|member]` (из email-префиксов родство не выводить;
  head — назначить главой, member — снять главу, повторное зачисление с другим именем = перемещение;
  `/family <email> -` — убрать из семьи). **Глава семьи** делегированно управляет ТОЛЬКО своей: зачисляет/перемещает
  к себе и убирает из своей, без права трогать чужие семьи/глав. Проверка прав — `require_head` (admin OR head).
- **Онбординг:** `/sync` → `/family` → `/invite <email>` → человек открывает
  `t.me/<BOT_USERNAME>?start=<код>` (одноразовый, 7 дней). Восстановление при смене телефона:
  `/bind @username <email>` (привязка при первом сообщении) или `/bind <tg_id> <email>`.
- **Чтение данных:** бот НЕ ходит в API панели, а читает sqlite read-only (`/opt/3x-ui/db` смонтирован в `/data:ro`,
  WAL: `-wal/-shm` должны лежать рядом — поэтому монтируется весь каталог, а не файл). Схема: `inbounds.settings`
  (JSON-строка) → `clients[].email/subId`; адрес ноды — join `inbounds.node_id = nodes.id`, Reality-порт = `inbounds.port`.
  Колонка называется `node_id`, не `nodeId`. subId должен быть консистентен по всем инбаундам.
- **Контейнер работает от root намеренно:** x-ui.db — root:root 600, панель при рестарте пересоздаёт `-shm/-wal`
  и права для не-root слетают; read-only rootfs + ro-монтирование данных панели гарантируют, что бот физически
  не может их повредить. Писать он может только в `/db` (своя sqlite).
- **Грабля 18:** TLS-probe к Reality-порту без учётных данных бессмысленна — часть нод релейит чужой
  client hello на dest (выглядит «живой»), часть рвёт его `TLSV1_ALERT_INTERNAL_ERROR`, хотя нода жива.
  В боте «живость» = TCP connect :443, так и оставить.
- **Грабля 19:** `getMe` отвечает даже когда процесс бота упал — проверка установки смотрит
  и `getMe`, и строку `старт бота` в `docker logs` (`install-bot.sh` так делает; не упрощать).
- **Фаза 2 (не сделана):** главы семей сами создают клиентов → мутации через API панели (подход `lib/xui-api.sh`)
  + `docker restart 3xui_app` на КАЖДОЙ ноде (грабля 17), для этого мастеру нужен ssh-доступ до нод.
- **Структура кода:** общее в `bot/app/common.py` (конфиг, БД-экземпляры, права require_admin/require_head,
  меню команд apply_commands, greet); инлайн-экраны в `bot/app/adminui.py` (Админка + экраны глав, FSM на имя
  семьи/контакт привязки, callback-префиксы `adm:`/`hd:`, имена семей валидируются `^[A-Za-z0-9_-]{1,24}$` —
  иначе ломается callback_data); `bot/app/main.py` — только сообщения/кнопки/точка входа.
  Новые инлайн-экраны добавлять в adminui, не в main.

## 7. Правила для агента

- `AGENTS.md` коммитится и должен оставаться sanitized. Не коммить `AGENTS.secrets.md`, `clients.txt`,
  ключи и другие секреты (gitignore уже настроен). Новые секреты — только на серверы (chmod 600)
  или в `AGENTS.secrets.md`.
- Перед коммитом проверяй состав индекса и diff: секретный файл не должен попасть в Git даже частично.
- Не открывай наружу 2053/2096; Reality — только на нодах (443), на мастере наружу смотрят лишь 22/80/443 (nginx);
  перед `nodes/add` проверяй UFW ноды.
- Любые правки `install.sh`/compose — проверяй `bash -n` и по возможности на тестовом сервере; README и AGENTS.md
  обновляй вместе с кодом. При изменении фактических доступов и топологии обновляй `AGENTS.secrets.md`.
- Перед разрушительными действиями (delete inbound/node, restore) — сделай `scripts/backup.sh`.
- Хостер режет чужие сети на 80 порту: любые проверки «снаружи» делай с устройства пользователя, а не с сервера.
