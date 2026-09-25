# Telegram Message Filter

Сервис для Raspberry Pi: читает сообщения конкретного Telegram-бота **от имени
вашего пользовательского аккаунта** (Telethon / MTProto), фильтрует их по
правилам и пересылает только важные через **вашего собственного бота**
(Telegram Bot API) в выбранный чат.

Зачем: исходный бот шлёт много уведомлений, звук приходится отключать и
важное теряется. Этот сервис оставляет только то, что действительно нужно,
и присылает это отдельным каналом — с включёнными уведомлениями.

Проверено на: Raspberry Pi, Debian 12 (bookworm), Python 3.11.2,
Telethon 1.45.0, aiohttp 3.14.3, python-dotenv 1.2.3.

---

## Содержание

1. [Как это работает](#как-это-работает)
2. [Структура проекта](#структура-проекта)
3. [Шаг 1. API ID и API Hash](#шаг-1-получить-telegram-api-id-и-api-hash)
4. [Шаг 2. Свой бот для уведомлений](#шаг-2-создать-своего-бота-для-уведомлений)
5. [Шаг 3. Узнать TARGET_CHAT_ID](#шаг-3-узнать-target_chat_id)
6. [Шаг 4. Python и venv](#шаг-4-python-виртуальное-окружение-и-зависимости)
7. [Шаг 5. Файл .env](#шаг-5-файл-env)
8. [Шаг 6. Первый запуск и авторизация](#шаг-6-первый-запуск-и-авторизация-telethon)
9. [Шаг 7. Проверка фильтра](#шаг-7-проверка-фильтра)
10. [Шаг 8. Установка systemd-сервиса](#шаг-8-установка-systemd-сервиса)
11. [Диагностика](#диагностика)
12. [Обновление приложения](#обновление-приложения)
13. [Справочник по .env](#справочник-по-env)
14. [Архитектура: что за что отвечает](#архитектура-что-за-что-отвечает)
15. [Безопасность](#безопасность)
16. [Тесты](#тесты)

---

## Как это работает

```
Исходный бот  ──►  ваш аккаунт Telegram
                        │  (Telethon, MTProto, event handler — не polling)
                        ▼
                 NewMessage event
                        │
                        ├─► проверка отправителя (это точно нужный бот?)
                        ├─► текст сообщения (raw_text)
                        ├─► MessageFilter: exclude → длина → keywords/regex
                        ├─► SQLite: это сообщение уже пересылали?
                        ├─► format_message(): безопасный HTML
                        ├─► Telegram Bot API  (ваш бот, свой токен)
                        ▼
                 целевой чат  ──►  запись в SQLite (только после успеха)
```

Две авторизации никогда не смешиваются:

| Что | Чем | Где в коде |
|---|---|---|
| Чтение сообщений исходного бота | Telethon, сессия вашего аккаунта | `app/telegram_client.py` |
| Отправка отфильтрованного | Bot API, токен вашего бота | `app/bot_sender.py` |

---

## Структура проекта

```
telegram_filter/
├── app/
│   ├── __init__.py
│   ├── main.py             точка входа, обработчик событий, graceful shutdown
│   ├── config.py           чтение и валидация .env
│   ├── telegram_client.py  Telethon: авторизация, источник, переподключение
│   ├── filter.py           правила фильтрации (чистая логика, без сети)
│   ├── bot_sender.py       Bot API + format_message() + экранирование HTML
│   └── models.py           SQLite: журнал обработанных сообщений
├── tests/
│   └── test_filter.py      минимальные тесты фильтра и форматирования
├── systemd/
│   └── telegram-filter.service
├── .env.example
├── .gitignore
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

---

## Шаг 1. Получить Telegram API ID и API Hash

Нужны для работы от имени вашего аккаунта (MTProto).

1. Откройте <https://my.telegram.org> и войдите по номеру телефона
   (код придёт в Telegram).
2. Выберите **API development tools**.
3. Заполните форму:
   * *App title*: `telegram-filter`
   * *Short name*: `tgfilter`
   * *Platform*: Desktop
4. После создания вы увидите **App api_id** (число) и **App api_hash**
   (32 символа). Это и есть `API_ID` и `API_HASH`.

> ⚠️ `API_HASH` — такой же секрет, как пароль. Он не должен попадать в Git,
> в чаты и в логи.

---

## Шаг 2. Создать своего бота для уведомлений

1. Напишите [@BotFather](https://t.me/BotFather) команду `/newbot`.
2. Укажите отображаемое имя и username (должен заканчиваться на `bot`).
3. BotFather пришлёт токен вида `1234567890:AAE...` — это `TARGET_BOT_TOKEN`.

Если бот будет писать в группу или канал — добавьте его туда:
* группа: добавьте бота участником (для канала — администратором с правом
  публикации);
* по умолчанию бот не видит чужие сообщения, но отправлять свои может.

---

## Шаг 3. Узнать TARGET_CHAT_ID

**Вариант А — личные сообщения себе (самый простой):**

1. Напишите своему новому боту любое сообщение (например, `/start`).
2. Выполните на Pi:

   ```bash
   curl -s "https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates" | python3 -m json.tool
   ```

3. Найдите `"chat": {"id": 123456789, ...}` — это `TARGET_CHAT_ID`.

**Вариант Б — группа:** добавьте бота в группу, напишите там сообщение и
снова вызовите `getUpdates`. Идентификатор группы отрицательный, например
`-1001234567890`.

**Вариант В — канал:** можно указать публичный username канала:
`TARGET_CHAT_ID=@my_alerts_channel`.

Проверить, что всё сходится:

```bash
curl -s "https://api.telegram.org/bot<ТОКЕН>/sendMessage" \
     -d "chat_id=<CHAT_ID>" -d "text=test"
```

---

## Шаг 4. Python, виртуальное окружение и зависимости

Нужен Python 3.11+. На Raspberry Pi OS (bookworm) он уже есть:

```bash
python3 --version          # ожидается 3.11 или выше
```

Если нет:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

Скопируйте проект на Pi и создайте окружение:

```bash
# рекомендуемое место для сервиса
sudo mkdir -p /opt/telegram-filter
sudo chown pi:pi /opt/telegram-filter
cp -r ./* /opt/telegram-filter/      # или git clone
cd /opt/telegram-filter

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Проверьте реальный путь к интерпретатору — именно он пойдёт в systemd-юнит
(**не** `/usr/bin/python3`: в системном Python нет Telethon):

```bash
ls -l /opt/telegram-filter/.venv/bin/python
/opt/telegram-filter/.venv/bin/python -c "import telethon; print(telethon.__version__)"
```

---

## Шаг 5. Файл .env

```bash
cd /opt/telegram-filter
cp .env.example .env
chmod 600 .env          # читать может только владелец
nano .env
```

Минимально нужно заполнить:

```dotenv
API_ID=123456
API_HASH=ваш_хеш
SESSION_NAME=telegram_filter

SOURCE_BOT_USERNAME=@some_bot

TARGET_BOT_TOKEN=1234567890:AAE...
TARGET_CHAT_ID=123456789

FILTER_KEYWORDS=важно,критично,ошибка,авария
CASE_SENSITIVE=false
LOG_LEVEL=INFO
```

Полный список параметров — в разделе [Справочник по .env](#справочник-по-env).

---

## Шаг 6. Первый запуск и авторизация Telethon

Авторизация интерактивная, поэтому её нужно выполнить **вручную один раз**,
до установки сервиса (под systemd терминала нет, и сервис специально не
пытается читать stdin — он выведет понятную ошибку).

```bash
cd /opt/telegram-filter
.venv/bin/python -m app.main --login
```

Что спросит Telethon:

1. `Please enter your phone (or bot token):` — ваш номер в формате
   `+380XXXXXXXXX`;
2. `Please enter the code you received:` — код, который придёт **в сам
   Telegram** (не SMS);
3. `Please enter your password:` — пароль двухфакторной аутентификации,
   если она включена (ввод скрыт).

После успеха появится файл сессии:

```
/opt/telegram-filter/telegram_filter.session
```

Имя — из `SESSION_NAME`. Каталог можно изменить переменной `SESSION_DIR`.
Этот файл эквивалентен доступу к вашему аккаунту: он в `.gitignore`,
не копируйте его никуда.

Дальше сервис будет использовать существующую сессию и больше ничего не
спросит. Пробный запуск в обычном режиме (остановить — `Ctrl+C`):

```bash
.venv/bin/python -m app.main
```

Ожидаемый вывод:

```
2026-09-20 15:20:01 INFO app.models SQLite store ready: /opt/telegram-filter/processed_messages.db
2026-09-20 15:20:01 INFO app.bot_sender Bot API ready: @my_alerts_bot (id=1234567890)
2026-09-20 15:20:02 INFO app.telegram_client Telegram client connected as Имя (id=...)
2026-09-20 15:20:02 INFO app.telegram_client Источник найден: Some Bot (id=..., username=@some_bot)
2026-09-20 15:20:02 INFO app Monitoring @some_bot
2026-09-20 15:20:02 INFO app Filter rules: keywords=['важно', 'критично'], case_sensitive=False
```

---

## Шаг 7. Проверка фильтра

Без Telegram, прямо в консоли — прогнать строку через текущие правила из `.env`:

```bash
.venv/bin/python -m app.main --check-filter "Сервер недоступен, это КРИТИЧНО"
```

```
Правила: keywords=['важно', 'критично'], exclude=['реклама'], case_sensitive=False
matched = True
reason  = keyword: "критично"

--- так будет выглядеть уведомление ---
<b>🚨 ВАЖНОЕ СООБЩЕНИЕ</b>
...
```

Код возврата: `0` — сообщение прошло бы фильтр, `1` — было бы отброшено.
Удобно для быстрой проверки правил:

```bash
.venv/bin/python -m app.main --check-filter "ВАЖНО! РЕКЛАМА"   # exit 1, excluded
```

Полный набор тестов правил — `tests/test_filter.py` (см. [Тесты](#тесты)).

---

## Шаг 8. Установка systemd-сервиса

Юнит рассчитан на установку в `/opt/telegram-filter` с пользователем `pi`.
Если у вас другой путь или пользователь — поправьте `WorkingDirectory`,
`ExecStart`, `EnvironmentFile`, `ReadWritePaths`, `User`/`Group`.

```bash
sudo cp /opt/telegram-filter/systemd/telegram-filter.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Включить автозапуск при загрузке Raspberry Pi и стартовать сейчас:

```bash
sudo systemctl enable telegram-filter.service
sudo systemctl start telegram-filter.service
```

(или одной командой: `sudo systemctl enable --now telegram-filter.service`)

Статус:

```bash
sudo systemctl status telegram-filter.service
```

Логи (в реальном времени):

```bash
journalctl -u telegram-filter.service -f
```

Остановить / перезапустить / выключить автозапуск:

```bash
sudo systemctl stop telegram-filter.service
sudo systemctl restart telegram-filter.service
sudo systemctl disable telegram-filter.service
```

Проверка автозапуска после перезагрузки:

```bash
sudo reboot
# после загрузки:
systemctl is-enabled telegram-filter.service   # enabled
systemctl is-active telegram-filter.service    # active
```

---

## Диагностика

```bash
# последние 200 строк лога
journalctl -u telegram-filter.service -n 200 --no-pager

# только ошибки
journalctl -u telegram-filter.service -p err --no-pager

# логи за сегодня
journalctl -u telegram-filter.service --since today --no-pager

# подробный режим: временно поднять уровень логирования
#   в .env: LOG_LEVEL=DEBUG   (и LOG_MESSAGE_TEXT=true, если нужен текст сообщений)
sudo systemctl restart telegram-filter.service

# проверить синтаксис юнита
systemd-analyze verify /etc/systemd/system/telegram-filter.service

# что лежит в журнале обработанных сообщений
sqlite3 /opt/telegram-filter/processed_messages.db \
  "SELECT chat_id, message_id, processed_at FROM processed_messages ORDER BY processed_at DESC LIMIT 10;"

# проверить токен целевого бота
curl -s "https://api.telegram.org/bot<ТОКЕН>/getMe" | python3 -m json.tool
```

Частые проблемы:

| Симптом в логе | Причина и что делать |
|---|---|
| `Нет активной Telegram-сессии…` | Не выполнен `--login`, или сессия удалена. Выполните `.venv/bin/python -m app.main --login` |
| `Источник @xxx не найден` | Опечатка в `SOURCE_BOT_USERNAME` либо у аккаунта нет диалога с ботом — откройте бота в Telegram и нажмите Start |
| `Telegram Bot API отклонил запрос: getMe: Unauthorized` | Неверный `TARGET_BOT_TOKEN` |
| `Bad Request: chat not found` | Неверный `TARGET_CHAT_ID`, или бот не добавлен в чат/канал |
| `FloodWait N c` | Telegram просит подождать; сервис ждёт указанное время сам, ничего делать не нужно |
| `Ошибка соединения с Telegram … Повтор через N c` | Нет интернета. Сервис переподключится сам, когда сеть вернётся |
| `Сессия перестала быть валидной` | Сессию завершили в настройках Telegram → повторите `--login` |

---

## Обновление приложения

```bash
cd /opt/telegram-filter
sudo systemctl stop telegram-filter.service

git pull                      # или скопируйте новые файлы
.venv/bin/pip install -r requirements.txt   # если менялись зависимости

sudo systemctl start telegram-filter.service
sudo systemctl status telegram-filter.service
```

`.env`, `*.session` и база `processed_messages.db` при обновлении не трогаются,
повторная авторизация не нужна.

---

## Справочник по .env

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `API_ID` | — (обязательна) | ID приложения с my.telegram.org |
| `API_HASH` | — (обязательна) | Hash приложения с my.telegram.org |
| `SESSION_NAME` | `telegram_filter` | Имя файла сессии Telethon |
| `SESSION_DIR` | корень проекта | Каталог для `.session` |
| `SOURCE_BOT_USERNAME` | — (обязательна) | Источник: `@some_bot` или числовой id |
| `TARGET_BOT_TOKEN` | — (обязательна) | Токен вашего бота от @BotFather |
| `TARGET_CHAT_ID` | — (обязательна) | Куда слать: id или `@channelusername` |
| `FILTER_KEYWORDS` | пусто | Разрешающие слова через запятую |
| `FILTER_EXCLUDE_KEYWORDS` | пусто | Запрещённые слова (высший приоритет) |
| `FILTER_REGEX` | пусто | Regex (Python `re`), работает по ИЛИ с keywords |
| `CASE_SENSITIVE` | `false` | Учитывать регистр |
| `FILTER_MIN_LENGTH` | `0` | Минимальная длина текста (0 = не проверять) |
| `FILTER_MAX_LENGTH` | `0` | Максимальная длина текста (0 = не проверять) |
| `STARTUP_PROBE` | `true` | При старте прислать последнее совпадение (самопроверка) |
| `STARTUP_PROBE_LIMIT` | `50` | Сколько последних сообщений просмотреть при старте |
| `BOT_COMMANDS` | `true` | Отвечать на `/info` (long polling getUpdates) |
| `DB_PATH` | `processed_messages.db` в корне | Путь к SQLite |
| `DB_RETENTION_DAYS` | `30` | Через сколько дней чистить журнал (0 = не чистить) |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `LOG_MESSAGE_TEXT` | `false` | Писать полный текст сообщений (только для отладки) |
| `FLOOD_WAIT_MAX_SECONDS` | `900` | Максимум ожидания при FloodWait |
| `BOT_API_TIMEOUT` | `20` | Таймаут HTTP-запроса к Bot API, c |
| `BOT_API_MAX_RETRIES` | `4` | Число повторов при сетевых ошибках/5xx |
| `RECONNECT_MAX_DELAY` | `60` | Максимальная пауза между переподключениями, c |

### Самопроверка при старте

При запуске сервис проходит по последним `STARTUP_PROBE_LIMIT` сообщениям
источника, находит самое свежее, подходящее под правила, и присылает его в
целевой чат с заголовком **«✅ СЕРВИС ЗАПУЩЕН — последнее совпадение»**.
Уведомление выглядит точно так же, как обычное, — по нему видно, что вся
цепочка настроена верно, и не нужно ждать нового реджекта. Настройки в
шапку не подмешиваются: они доступны по команде `/info`.

Если совпадений в истории нет, придёт **«⚙️ СЕРВИС ЗАПУЩЕН — совпадений
нет»**: это тоже полезный сигнал — значит, правила не ловят ничего из того,
что уже присылал источник.

Сообщение из истории **не** помечается обработанным: это диагностика, а не
пересылка, и на защиту от дубликатов она не влияет. Ошибки самопроверки
(FloodWait, недоступная история, отказ Bot API) пишутся в лог, но сервис
не останавливают — мониторинг важнее.

Выключается одной переменной:

```ini
STARTUP_PROBE=false
```

### Команды бота

Целевой бот отвечает на `/info` — настройки и состояние сервиса:

```
ℹ️ СОСТОЯНИЕ СЕРВИСА

Аккаунт: Oleksandr (id=8997424641)
Источник: @trident_appmonitor_bot
Правила: keywords=['April', …], exclude=[…], case_sensitive=False
Целевой чат: 8997424641
Самопроверка при старте: включена (50 сообщений)
Переслано сообщений: 12
Работает: 3 ч 20 мин
```

Для этого сервис ведёт long polling `getUpdates` — второй, независимый от
Telethon канал через Bot API. Команда `/info` регистрируется в меню бота
(`setMyCommands`), поэтому видна в интерфейсе Telegram.

**Бот отвечает только в `TARGET_CHAT_ID`.** Ответ содержит настройки
сервиса, и посторонний, нашедший бота по имени, их не получит — команда из
чужого чата пишется в лог как предупреждение и игнорируется.

Команды, накопившиеся пока сервис был выключен, отбрасываются при старте
(`getUpdates` с `offset=-1`), иначе бот разом ответил бы на всю очередь.

Выключается одной переменной:

```ini
BOT_COMMANDS=false
```

### Логика фильтра

Порядок проверок — сверху вниз, первая сработавшая даёт ответ:

1. **Запрещённые слова** (`FILTER_EXCLUDE_KEYWORDS`) — если найдено,
   сообщение отбрасывается, даже если в нём есть разрешённое слово.
   `"ВАЖНО! РЕКЛАМА"` → не отправляется.
2. **Длина** (`FILTER_MIN_LENGTH` / `FILTER_MAX_LENGTH`).
3. **Разрешающие правила**: `FILTER_KEYWORDS` **или** `FILTER_REGEX`.
   Достаточно одного совпадения.

Если не заданы ни keywords, ни regex — проходят все сообщения, не отсеянные
пунктами 1–2.

Регистр по умолчанию не учитывается: `КРИТИЧНО` совпадает с `критично`
(в том числе внутри regex — добавляется флаг `re.IGNORECASE`).

Некорректный regex — это ошибка конфигурации: приложение не падает с
traceback, а печатает понятное сообщение и завершается с кодом `2`.

---

## Архитектура: что за что отвечает

### `app/config.py`
Единственное место, где читаются переменные окружения. `load_config()`
подхватывает `.env` (переменные окружения имеют приоритет — это важно для
systemd), валидирует типы и диапазоны и возвращает иммутабельный `Config`.
Любая проблема — `ConfigError` с человеческим текстом, а не traceback.
Секреты (`api_hash`, `target_bot_token`) помечены `repr=False`, поэтому
даже случайный `log.debug(config)` их не покажет.

### `app/filter.py`
Чистая логика, не знает ни про Telegram, ни про сеть — поэтому легко
тестируется. `MessageFilter.check(text)` возвращает
`FilterResult(matched, reason)`; `reason` попадает в лог и в уведомление.
Новое правило добавляется методом `_check_<name>` и одной строкой в `check()`
— контракт `FilterResult` не меняется.

### `app/models.py`
Защита от дубликатов. SQLite, таблица `processed_messages` с первичным ключом
`(chat_id, message_id)` — это и есть уникальный идентификатор сообщения в
Telegram. Запись добавляется **только после успешной отправки**: если Bot API
вернул ошибку, сообщение не считается обработанным. sqlite3 синхронный,
поэтому каждый вызов уходит в `asyncio.to_thread`, а соединение защищено
`threading.Lock` — event loop не блокируется. Старые записи чистятся по
`DB_RETENTION_DAYS`, чтобы база не росла бесконечно.

### `app/bot_sender.py`
Bot API и только он: HTTP-клиент на aiohttp, свой токен, никакой связи с
сессией Telethon. `format_message()` собирает HTML-уведомление и **экранирует
пользовательский текст** (`&`, `<`, `>`), поэтому символы из исходного
сообщения не ломают разметку; текст обрезается так, чтобы уложиться в лимит
Telegram в 4096 символов. Ссылка «Открыть оригинал» добавляется, только если
её реально можно построить. Транспорт умеет: `429` → ждать `retry_after`,
`5xx` и сетевые ошибки → экспоненциальный backoff с джиттером (ограниченное
число попыток, без бесконечного цикла), прочие `4xx` → сразу ошибка,
потому что повторять бессмысленно.

### `app/telegram_client.py`
Telethon: создание клиента (`auto_reconnect`, бесконечные попытки
переподключения с паузами, `flood_sleep_threshold`), проверка сессии,
поиск источника (`resolve_source` — при отсутствии бота понятная ошибка и
выход), построение ссылки на оригинал и внешний цикл `run_until_stopped()`.
Цикл нужен на случай, когда Telethon всё-таки отключился совсем: он
поднимает соединение с экспоненциальной паузой и мгновенно просыпается по
сигналу остановки.

### `app/main.py`
Сборка всего вместе: конфиг → логирование → SQLite → Bot API → Telethon →
регистрация event handler (`events.NewMessage(from_users=источник,
incoming=True)`) → цикл работы. Здесь же обработчик `_on_new_message`,
который никогда не бросает исключение наружу (один сбойный апдейт не должен
убивать сервис), и graceful shutdown: `SIGTERM`/`SIGINT` → закрываются
Telethon, HTTP-сессия и SQLite → выход с кодом `0` без traceback.

Почему event handler, а не polling: Telethon держит одно MTProto-соединение и
сам получает апдейты. Опрашивать Telegram в цикле не нужно — это лишний
трафик и риск FloodWait. Обрабатываются только новые сообщения; история чата
при перезапуске не перечитывается (`catch_up` выключен).

### Коды возврата

| Код | Значение |
|---|---|
| `0` | Штатное завершение (SIGTERM/SIGINT) |
| `1` | Ошибка выполнения: нет сессии, источник не найден, Bot API отказал |
| `2` | Ошибка конфигурации `.env` |

---

## Безопасность

* `.env`, `*.session`, `*.db` перечислены в `.gitignore` — секреты не попадают
  в Git. Проверить перед первым коммитом: `git status --short`.
* В логи не пишутся `API_HASH`, токен бота, пароль, код подтверждения и
  содержимое сессии. Полный текст сообщений пишется только при
  `LOG_LEVEL=DEBUG` **и** `LOG_MESSAGE_TEXT=true` — в production оставьте
  `false`.
* Права на файлы: `chmod 600 .env` и `chmod 600 *.session`.
* Файл сессии даёт полный доступ к вашему аккаунту Telegram. При
  компрометации — завершите сессию в Telegram: Настройки → Устройства.
* Сервис только читает сообщения, которые и так приходят вашему аккаунту, и
  пересылает выбранные в ваш собственный чат. Он не рассылает сообщения
  третьим лицам и не обходит ограничения Telegram.

---

## Тесты

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

Покрыто: совпадение по ключевым словам, регистронезависимость и режим
`CASE_SENSITIVE`, приоритет запрещённых слов над разрешающими, regex,
комбинация «keywords ИЛИ regex», ограничения длины, экранирование HTML,
обрезка до лимита Telegram и добавление ссылки только когда она задана.
