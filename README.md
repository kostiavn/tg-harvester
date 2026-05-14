# TG Harvester

Собирает сообщения из Telegram-чатов **от твоего акка** через Telethon (MTProto).
Сохраняет в формате, **совместимом с экспортом Telegram Desktop** (`result.json`) — тот же формат, что ты сейчас руками выгружаешь. Готовые файлы можно скармливать Claude как контекст.

## Что умеет

- 4 раза в сутки автоматически забирает новые сообщения из списка чатов (cron/systemd timer)
- Сохраняет всё в `data/<chat>.json` — формат 1-в-1 как ручной экспорт TG Desktop
- Раз в неделю упаковывает всё в `.tar.gz` и **отправляет тебе в Saved Messages**
- Слушает команды в Saved Messages: `/dump remnaflood 24`, `/harvest`, `/archive`, `/status`
- Сохраняет `reply_to_message_id` — reply-цепочки восстанавливаются полностью
- Атомарная запись через temp+rename (не теряет данные при крэше/ребуте)
- Логирование в `logs/harvester.log` + journalctl

## Что НЕ делает (специально)

- Не качает медиа-файлы (фото/видео). Сохраняет только метаданные. Иначе диск забьётся за месяц.
- Не фильтрует сообщения. Сырьё хранится целиком — фильтр потом, на стороне Claude.
- Не парсит сообщения в реальном времени (это другая задача — `cmd_listen` слушает только Saved).

## Архитектура

```
/opt/tg-harvester/
├── harvester.py                # главный скрипт
├── config.yaml                 # api_id, api_hash, список чатов
├── state.json                  # last_message_id для каждого чата
├── session/
│   └── harvester.session       # Telethon сессия (chmod 600)
├── data/
│   ├── remnaflood.json         # архив (растёт инкрементально)
│   └── prizrak-talk.json
├── archive/
│   ├── 2026-W19/               # ротация при размере > 50 MB
│   └── tg-archive-2026-W19.tar.gz
├── logs/
│   └── harvester.log
└── venv/
```

## Установка (в LXC на Proxmox)

### 1. Создать LXC контейнер

В Proxmox UI:
- Template: Debian 12 (bookworm)
- CPU: 1 core, RAM: 512 MB, Disk: 4 GB (минимум — нам много не надо)
- Network: bridge vmbr0, DHCP
- Hostname: `tg-harvester`

### 2. Скопировать файлы и запустить установщик

С хоста или из контейнера:
```bash
# В контейнере
mkdir -p /opt/tg-harvester-install
# Закинуть туда harvester.py, config.example.yaml, install.sh
# (через scp с компа, или git clone, как удобнее)

cd /opt/tg-harvester-install
chmod +x install.sh
./install.sh
```

### 3. Получить API credentials

1. Зайти на https://my.telegram.org/apps
2. Залогиниться своим телефоном
3. Создать приложение:
   - App title: `Harvester` (любое)
   - Short name: `harvester`
   - Platform: `Other`
4. Скопировать `App api_id` и `App api_hash`

### 4. Заполнить config.yaml

```bash
nano /opt/tg-harvester/config.yaml
```

Вставить `api_id`, `api_hash`. В блоке `chats:` — реальные @username чатов которые хочешь собирать. Поле `key` — короткое имя, под ним будет файл `data/<key>.json`.

### 5. Первичная авторизация (интерактивно, один раз)

```bash
cd /opt/tg-harvester
./venv/bin/python harvester.py auth
```

Скрипт спросит:
- Телефон в международном формате (`+79...`)
- Код подтверждения (придёт в Telegram)
- Пароль 2FA (если включён)

После успеха создаётся `session/harvester.session` с правами 600. **Этот файл — твой акк.** Не светить.

### 6. Тестовый запуск

```bash
./venv/bin/python harvester.py harvest
```

Должны появиться файлы в `data/`. Проверь:
```bash
ls -lh data/
cat state.json   # last_message_id обновился
```

### 7. Включить расписание

```bash
systemctl enable --now tg-harvester.timer
systemctl enable --now tg-harvester-archive.timer

# Опционально — listener для команд в Saved Messages
systemctl enable --now tg-harvester-listen.service

# Проверка
systemctl list-timers | grep tg-harvester
```

## Использование

### Автоматический режим

После шага 7 ничего больше делать не надо. Раз в неделю в воскресенье 23:00 получишь в Saved Messages tar.gz со всеми архивами.

### Команды через Saved Messages (если включён `listen`)

Пишешь себе в Saved:

```
/status                    — что сейчас в архиве, сколько сообщений по каждому чату
/harvest                   — забрать сейчас (не ждать расписания)
/dump remnaflood 24        — выгрузить последние 24 часа конкретного чата (придёт файлом)
/dump prizrak-talk 168     — выгрузить за неделю
/archive                   — упаковать и прислать tar.gz прямо сейчас
/help                      — список команд
```

Это удобно когда хочешь дать Claude свежий контекст: пишешь `/dump remnaflood 24` → получаешь JSON → перетаскиваешь в чат с Claude.

### Ручные команды

```bash
cd /opt/tg-harvester

# Собрать сейчас
./venv/bin/python harvester.py harvest

# Выгрузить чат за N часов (создаст архив + отправит в Saved)
./venv/bin/python harvester.py dump remnaflood 24

# Упаковать всё и отправить в Saved
./venv/bin/python harvester.py archive
```

## Диагностика

```bash
# Логи скрипта
tail -f /opt/tg-harvester/logs/harvester.log

# Логи systemd
journalctl -u tg-harvester.service -f
journalctl -u tg-harvester-listen.service -f

# Состояние таймеров
systemctl list-timers | grep tg-harvester

# Что в state
cat /opt/tg-harvester/state.json | jq

# Размер архивов
du -sh /opt/tg-harvester/data/* /opt/tg-harvester/archive/*
```

## Безопасность

1. **session-файл** — это полный доступ к твоему акку. Права 600 ставятся автоматически. Не клонировать, не бэкапить наружу.
2. **api_hash в config.yaml** — менее критично, но всё равно не светить. `chmod 600 config.yaml` если есть параноя.
3. **LXC** — рекомендую не пробрасывать наружу. Внутренняя сеть Proxmox.
4. **Если session засветится** — заходишь в Telegram → Settings → Devices → завершить активный сеанс «Harvester». И заново `auth`.

## Известные нюансы

- **FloodWaitError** — если Telegram попросит подождать, скрипт логирует и пропускает заход. Следующий по расписанию подхватит.
- **Сервисные сообщения** (joined/left/pinned) — пропускаем.
- **Медиа** — сохраняем только тип и filename, файлы не качаем.
- **Reply-цепочки** — сохраняется `reply_to_message_id`, восстанавливаются по `id` при парсинге.
- **Edit** — если сообщение отредактировали ПОСЛЕ нашего захода, мы не узнаем (для этого нужен realtime listener на все чаты, отдельная история).
