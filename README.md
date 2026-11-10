# PGN

PasarGuard Node + HAProxy + Caddy по схеме Altair. Управляющий API, VLESS XHTTP Reality, VLESS XHTTP TLS и HTTPS-сайт используют один публичный TCP-порт `443`.

Панель управляет конфигурацией Xray, ключами Reality, `shortIds` и пользователями. Этот репозиторий готовит серверную обвязку, API-ключ и сертификат API. После запуска ноду нужно добавить в панель и назначить ей конфигурацию и пользователей.

## Требования

- Linux-сервер с локальным Docker Engine и Docker Compose v2, Python 3.9+ и OpenSSL 1.1.1+.
- Доступ к Docker; в примерах команды запускаются от root через `sudo`.
- Два разных домена с DNS A-записями на IPv4 сервера, без CDN-прокси.
- Свободные входящие TCP-порты `80` и `443`, доступные также в firewall провайдера.
- Исходящий доступ к реестру образов и ACME. Caddy получает сертификат сайта через HTTP-01 на порту `80`.

HAProxy, как на Altair, слушает публичный `443` по IPv4. Не публикуйте AAAA для этих доменов без добавления IPv6-listener в шаблон HAProxy. Внутренние порты слушают только loopback; открывать `62050`, `10443`, `10444`, `8443` наружу не нужно.

Скрипт не устанавливает Docker и не меняет firewall. Для Debian/Ubuntu установите Docker Engine с Compose plugin по [официальной инструкции](https://docs.docker.com/engine/install/), затем:

```sh
sudo apt-get update
sudo apt-get install -y git python3 openssl
```

## Быстрый запуск

```sh
git clone https://github.com/thefakedevs/pgn.git pgn
cd pgn
cp .env.example .env
nano .env
sudo ./pgn up
```

Заполните три значения:

```dotenv
NODE_DOMAIN=vpn.your-domain.ru
API_DOMAIN=pg.vpn.your-domain.ru
XHTTP_PATH=/assets/your-path-from-panel/
```

`XHTTP_PATH` — точный путь TLS XHTTP-входа из панели, с `/` в начале и конце. Разрешены сегменты из латинских букв, цифр, `_` и `-`. Это не путь XHTTP Reality: его локальная обвязка не использует. Для новой ноды выберите TLS-путь и задайте одинаковое значение в `.env` и панели.

`API_KEY` при первой установке оставьте пустым. Скрипт создаст UUID, сохранит в `.env` и выставит права `600`. Можно заранее указать существующий UUID. `.env` читается как данные: команды shell и подстановки переменных не выполняются. В нём допустимы только параметры из `.env.example`; окружение shell не заменяет его значения.

Команда `up`:

1. Проверяет `.env` и локальный Docker.
2. Генерирует недостающий API-ключ и самоподписанный сертификат API с SAN для `API_DOMAIN` (RSA 3072, 10 лет).
3. Формирует `config/haproxy.cfg` и `config/Caddyfile` из шаблонов.
4. Проверяет Compose, HAProxy и Caddy их собственными командами проверки; при включении логов также проверяет Vector.
5. Запускает контейнеры и выводит данные для панели, включая публичный сертификат API.

Проверки HAProxy и Caddy используют временные контейнеры из тех же образов. Docker при необходимости скачает образы. Проверка синтаксиса не подтверждает DNS, выдачу публичного сертификата или работоспособность VPN-клиента.

Повторный `up` сохраняет API-ключ, сертификаты и данные Caddy. Изменения сгенерированных конфигов отражаются в label контейнеров: Compose пересоздаёт прокси при изменении конфигурации. При неизменной конфигурации принудительного перезапуска нет.

## Подключение к панели

После запуска скопируйте выведенные значения в карточку ноды:

| Поле | Значение |
|---|---|
| Адрес | `API_DOMAIN` |
| Порт | `443` |
| Протокол | gRPC |
| TLS | включён |
| API key | UUID из `.env` / вывода скрипта |
| Сертификат | весь публичный PEM из вывода скрипта |

Подключайтесь по `API_DOMAIN`, чтобы TLS SNI совпадал с маршрутом HAProxy. Подключение к голому IP без нужного SNI попадёт в Xray вместо API. Приватный `ssl_key.pem` в панель не передаётся.

В панели настройте/назначьте ноде два входа:

| Настройка | XHTTP Reality | XHTTP TLS за Caddy |
|---|---|---|
| Протокол | VLESS | VLESS |
| Listen | `127.0.0.1` | `127.0.0.1` |
| Внутренний порт | `10443` | `10444` |
| Transport | XHTTP | XHTTP |
| Security на входе Xray | Reality | none |
| `sockopt.acceptProxyProtocol` | `true` | не включать |
| Reality target / dest | `127.0.0.1:8443` | — |
| Reality `serverNames` | `[NODE_DOMAIN]` | — |
| XHTTP path | задаётся в панели | точно `XHTTP_PATH` из `.env` |

Публичный адрес клиента в обоих случаях — `NODE_DOMAIN:443`. Для второго варианта клиент использует TLS с SNI `NODE_DOMAIN`; TLS завершает Caddy. Остальные параметры Reality/XHTTP и пользователей задайте в панели. Отдельный Xray JSON репозиторий не создаёт и с панелью по API не взаимодействует.

Обычный TLS проходит через Reality target в Caddy: специальный путь проксируется в Xray, остальные запросы получают файлы из `site/`. Измените `site/index.html` для собственного сайта.

## Команды

```sh
./pgn help         # справка без .env, Docker и прав root
sudo ./pgn prepare  # создать конфиги и identity API без Docker; вывести данные панели
sudo ./pgn check    # подготовка + проверки Compose/HAProxy/Caddy без запуска сервисов
sudo ./pgn up       # подготовка + проверки + запуск/применение изменений
sudo ./pgn info     # повторно показать данные панели и публичный сертификат
sudo ./pgn status   # состояние контейнеров
sudo ./pgn logs     # последние 100 строк логов каждого сервиса
sudo ./pgn down     # остановить и удалить контейнеры; сохранить state и .env
```

`./pgn`, `./pgn help` и `./pgn --help` выводят одну и ту же справку.

`prepare` и `check` при первом запуске создают постоянные учётные данные API. Данные панели выводятся командами `prepare`, `up` и `info`, поэтому их вывод содержит API-ключ. Не публикуйте его. `logs` может содержать адреса и данные VPN-соединений.

Используйте `./pgn up` для применения `.env`: один `docker compose up` не генерирует конфиги и сертификат. Сгенерированные файлы не редактируйте вручную — меняйте `.env` или `templates/`. Не меняйте `COMPOSE_PROJECT_NAME` после установки: это создаст отдельный Compose-проект.

После подключения панели:

```sh
curl -I https://vpn.your-domain.ru
sudo ./pgn status
```

Дождитесь публичного сертификата Caddy. Затем проверьте подключение клиентом отдельно через Reality и TLS XHTTP: успешный сайт ещё не подтверждает работу VPN.

## Access-логи Xray в PostgreSQL (опционально)

По умолчанию функция выключена. Старые `.env` не требуют изменений. При включении
Vector `0.61.0` запускается отдельным контейнером Compose и отправляет пакеты прямо
в **внешний** PostgreSQL 16+ по TLS. PostgreSQL в этот стек не добавляется.

### 1. Подготовьте PostgreSQL

Установите `pg_partman` **5.x** на сервере БД. От администратора создайте расширение
в схеме `partman`, затем примените миграции один раз, по порядку:

```sh
psql "$ADMIN_DSN" -v ON_ERROR_STOP=1 -c 'CREATE SCHEMA partman; CREATE EXTENSION pg_partman SCHEMA partman;'
psql "$OWNER_DSN" -v ON_ERROR_STOP=1 -f sql/001_access_logs.sql
psql "$OWNER_DSN" -v ON_ERROR_STOP=1 -f sql/002_access_partitions.sql
```

`OWNER_DSN` — администратор либо выделенный владелец схемы с правом CREATE в БД
и доступом к объектам `partman`; именно этот владелец должен выполнять обслуживание.
Существующее расширение в другой схеме сначала согласуйте с DBA. Миграции
транзакционные, не выполняются при `./pgn up` и не предназначены для повторного применения.

Создайте отдельный login `pgn_ingest` и задайте пароль через `\password pgn_ingest`
в интерактивном psql; не помещайте рабочий пароль в SQL-файлы. Выдайте только:

```sql
GRANT USAGE ON SCHEMA pgn TO pgn_ingest;
GRANT INSERT ON pgn.access_ingest TO pgn_ingest;
```

Приёмное представление вызывает SECURITY DEFINER trigger с фиксированным search_path.
Оно не позволяет клиенту задавать ID или отметку обработки. Все вставки идут через
родительскую `pgn.access_logs` с одной общей sequence для `BIGINT GENERATED ALWAYS
AS IDENTITY`. Ключ строки — `(event_time, id)`; ID не задаёт порядок коммитов.
Представление также обходит [ограничение defaults в sink Vector](https://vector.dev/docs/reference/configuration/sinks/postgres/).

В `postgresql.conf` добавьте к существующему списку preload-библиотек:

```ini
shared_preload_libraries = 'pg_partman_bgw'
pg_partman_bgw.dbname = 'имя_базы'
pg_partman_bgw.role = 'владелец_схемы'
pg_partman_bgw.interval = 3600
timezone = 'UTC'
```

Перезапустите PostgreSQL. Суточные UTC-партиции создаются на семь дней вперёд,
исходно покрывают последние 30 дней. `retention='30 days'` и
`retention_keep_table=false` удаляют целые старые партиции, включая необработанные
строки. Фактический срок — около 30–31 дня плюс интервал обслуживания. Удалённые
партиции автоматически не восстанавливаются. Проверяйте логи BGW и настройки:

```sql
SELECT parent_table, premake, retention, retention_keep_table, automatic_maintenance
FROM partman.part_config WHERE parent_table='pgn.access_logs';
SELECT * FROM partman.show_partitions('pgn.access_logs');
-- Ручная проверка от владельца, с timezone UTC:
SELECT partman.run_maintenance('pgn.access_logs');
```

Если BGW недоступен у провайдера, настройте ежечасный внешний scheduler с последней
командой. Без обслуживания новые партиции исчерпаются и вставки завершатся ошибкой.
Партиции DEFAULT нет: она могла бы бесконечно удерживать просроченные записи.

### 2. Включите файлы в панели PasarGuard

В существующем объекте `log` конфигурации ядра в панели задайте пути, сохранив
остальные настройки:

```json
{
  "log": {
    "access": "/var/lib/pg-node/logs/access.log",
    "error": "/var/lib/pg-node/logs/error.log",
    "loglevel": "warning"
  }
}
```

Примените конфигурацию к ноде через панель. PasarGuard сам принимает конфигурацию,
перехватывает stdout Xray и пишет эти файлы. Его текущий классификатор относит
rejected-события и access без email к error-файлу, поэтому Vector читает оба.
`DEBUG` не нужен; `state/node/generated/xray.json` не изменяется.

Время Xray интерпретируется как UTC; Compose задаёт `TZ=UTC` для ноды и Vector.
При первом включении Vector читает существующие файлы с начала, затем использует
постоянные checkpoints. Не переименовывайте source/sink IDs без плана миграции состояния.

### 3. Настройте `.env` и запустите

```dotenv
ACCESS_LOG_ENABLED=true
ACCESS_LOG_POSTGRES_DSN='postgres://pgn_ingest:percent-encoded-password@db.example.net/logs?sslmode=verify-full'
ACCESS_LOG_NODE_ID=eu-1
ACCESS_LOG_BUFFER_BYTES=1073741824
ACCESS_LOG_POSTGRES_CA_FILE=certs/postgres-ca.pem
```

Пароль и другие специальные символы URI кодируйте percent-encoding. ID ноды должен
быть стабильным и уникальным среди отправителей. CA — PEM-файл, путь абсолютный или
относительно репозитория; оставьте пустым для системных CA образа Vector. Скрипт
копирует CA в `config/postgres-ca.pem`, монтирует его read-only и добавляет путь к DSN
внутри контейнера. Имя хоста DSN должно совпадать с сертификатом. Параметры,
переопределяющие host или TLS отдельными query-опциями, запрещены.

```sh
sudo ./pgn check
sudo ./pgn up
sudo ./pgn status
```

`check`/`up` дополнительно выполняют `vector validate --no-environment` и `vector test`.
Они проверяют схему, VRL и fixtures без требования доступности БД. Отдельно проверить
соединение и TLS можно после `prepare` (вывод может содержать секреты):

```sh
sudo docker compose --profile access-logs run --rm --no-deps vector validate /etc/vector/vector.yaml
```

При собственном CA для этой прямой команды в DSN дополнительно укажите
`sslrootcert=/etc/vector/postgres-ca.pem` через защищённое окружение Docker Compose;
обычные команды `./pgn` добавляют этот параметр сами. Запускайте эту проверку до
`up` либо после `docker compose --profile access-logs stop vector`, чтобы проверочный
контейнер не конкурировал за дисковый буфер с работающим Vector. Sink healthcheck выполняет
`SELECT 1`; проверяйте приём отдельно запросом к `pgn.access_logs` от владельца БД.
Compose явно разрешает подстановку окружения Vector для ссылки на DSN в статическом
шаблоне (`VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION=true`, требуется с Vector 0.57).
Секрет не записывается в генерируемый YAML; не публикуйте `docker inspect` и вывод
прямого `docker compose config`, поскольку они содержат окружение контейнера.

Сохраняются `event_time` (`timestamptz`), `email`, `destination_host` (домен/IPv4/IPv6),
`destination_port`, `protocol`, `status`, `route` и `node_id`. Протокол — `tcp`/`udp`
назначения, не VLESS; маршрут — строка detour Xray, например `in -> direct`.
Отсутствующие email, порт и маршрут хранятся как NULL. Accepted означает принятие
соединения Xray, а не подтверждение успешного обмена с назначением.

Повреждённые access-строки и время более чем на пять минут в будущем идут в отдельный
ограниченный диагностический Docker json-file лог контейнера Vector (10 МиБ × 3),
строка обрезается до 2048 символов. Обычные error-строки отфильтровываются. События
старше 30 дней отбрасываются и перед отправкой, и при приёме просроченного буфера в БД.

### Надёжность, диски и отключение

Пакет — до 500 событий или одна секунда. Сетевые ошибки повторяются с backoff до
30 секунд между попытками. Буфер и checkpoints находятся в `state/vector`; минимум
буфера 268435488 байт, по умолчанию 1 ГиБ. При заполнении чтение файлов замедляется.
Это не ограничивает размер исходных файлов Xray. Диск контролируйте отдельно.

Ограничьте исходные логи системным logrotate, например (замените абсолютный путь):

```text
/opt/pgn/state/node/logs/*.log {
    daily
    maxsize 100M
    rotate 7
    missingok
    notifempty
    copytruncate
    nocompress
    su root root
}
```

Для контроля maxsize запускайте logrotate ежечасно. При `copytruncate` возможна
потеря строк между копированием и усечением; при длительном простое БД ротация может
удалить ещё не прочитанные события. Абсолютная гарантия доставки и exactly-once не
предоставляются: после неопределённого результата INSERT возможны дубликаты,
дисковая синхронизация не мгновенная, SQL-ошибки не все повторяемые. Неверная схема,
права и отсутствие партиций требуют исправления оператором. Следите за ошибками
sink, discarded events, размером буфера, свободным диском и последним временем приёма.

Поставьте `ACCESS_LOG_ENABLED=false` и выполните `sudo ./pgn up`: Vector остановится,
его буфер сохранится. `status`, `logs`, `down` видят сервис и после выключения флага.
Для прекращения записи файлов также уберите пути логов в панели и примените её
конфигурацию. Не удаляйте `state/vector`, если хотите продолжить доставку.

### Безопасная пакетная обработка

[sql/process_batch.sql](sql/process_batch.sql) — исполняемый пример для одной группы
внешних обработчиков. Замените отмеченную секцию своим действием до запуска:

```sh
psql "$WORKER_DSN" -v ON_ERROR_STOP=1 -v batch_size=500 -f sql/process_batch.sql
```

Worker требует USAGE на `pgn`, SELECT на таблицу и UPDATE для `processed_at`.
Выборка `processed_at IS NULL LIMIT … FOR UPDATE SKIP LOCKED`, бизнес-действие и
отметка по `(event_time,id)` выполняются в одной транзакции. При rollback строки
доступны снова. Не используйте `id > last_id`: меньший ID может закоммититься позже.
Внешние эффекты вне PostgreSQL требуют идемпотентности; несколько независимых групп
обработчиков этой единственной отметкой не поддерживаются.

Linux CI проверяет реальный Vector, TLS, PostgreSQL/pg_partman, конкурентные транзакции
и восстановление после остановки БД и аварийного перезапуска Vector. Для локального
повтора нужен свободный `127.0.0.1:55432`, Docker и Python с `psycopg[binary]`:

```sh
docker build -t pgn-postgres-test tests/integration
python3 tests/integration/access_logs.py
```

## Перенос существующей ноды

1. Подготовьте новый сервер и склонируйте этот репозиторий.
2. Безопасно перенесите прежние `.env` и `state/`, а также свой `site/`. Для согласованной копии состояния Caddy копируйте после остановки старого стека; на старом сервере с таким репозиторием это `sudo ./pgn down`. Для Altair сначала задайте все параметры нового `.env` и перенесите его прежний API-ключ — старый `.env` может иметь другую структуру.
3. Убедитесь, что `API_DOMAIN` соответствует перенесённому сертификату. Копируйте `ssl_cert.pem` и `ssl_key.pem` одной парой.
4. Переключите DNS и при необходимости адрес ноды в панели, запустите `sudo ./pgn up`, восстановите связь с панелью.
5. Проверьте сайт и оба варианта VPN клиентом.

Если сохранить домены, API-ключ, сертификат API и конфигурацию в панели, учётные данные менять не нужно. Ключи Reality, `shortIds` и UUID пользователей переносятся через сохранённую конфигурацию панели. Клиентские профили с прежним IP потребуют обновления.

Если `API_KEY` потерян при наличии сертификата, скрипт остановится вместо автоматической смены ключа. Если сертификат/ключ неполны, не совпадают, истекли или не подходят к `API_DOMAIN`, скрипт также остановится. Для намеренной замены сначала сохраните резервную копию, затем установите новую пару и обновите сертификат в панели. Истекающий сертификат API автоматически не ротируется.

## Файлы и версии

```text
.env.example             # пользовательские параметры
compose.yml              # официальные образы; Vector закреплён на версии
pgn                      # точка входа
scripts/pgn.py           # подготовка, проверки и управление
templates/               # HAProxy / Caddy / опциональный Vector
sql/                     # миграции access-логов и пример обработки
site/                    # HTTPS-сайт
config/                  # генерируется, исключено из Git
state/node/certs/        # identity управляющего API, исключено из Git
state/node/generated/    # каталог PGN; не считать резервной копией Xray
state/caddy/             # ACME и сертификаты сайта, исключено из Git
state/node/logs/         # логи Xray при включении через панель
state/vector/            # постоянный буфер и checkpoints Vector
```

В Compose используются `pasarguard/node:latest`, `haproxy:latest` и `caddy:latest`.
Опциональный сервис логов использует фиксированный `timberio/vector:0.61.0-debian`.
Обновления выполняются вручную. Сохраните `.env` и `state/`, затем:

```sh
sudo docker compose pull
sudo ./pgn up
```

Maintenance API, обновлений из панели и автоматической замены геофайлов в этом
проекте нет. Нода использует Xray и геофайлы из официального образа.

Тесты подготовки и сохранения identity:

```sh
python3 -m unittest discover -s tests -v
```

Источники: [параметры PGN](https://docs.pasarguard.org/en/node/configuration/), [проект PGN](https://github.com/PasarGuard/node), [Caddy ACME](https://caddyserver.com/docs/caddyfile/directives/tls), [проверка Caddy](https://caddyserver.com/docs/command-line#caddy-validate).
