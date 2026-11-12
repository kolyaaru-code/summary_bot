# ТЗ: перенос summary_bot с Railway на свой VPS

Документ для ИИ-агента, который будет выполнять переезд. Сначала прочитай `CONTEXT.md` в корне проекта: там описано, что за бот и как он устроен. Здесь только то, что касается переезда.

> Репозиторий публичный. В этот файл и в git **не записывать** IP сервера, токены, ключи, ID чатов и пароли. IP сервера владелец сообщит в чате.

---

## 1. Цель

Запустить бота на VPS владельца вместо Railway. На Railway кончился бесплатный лимит, бот стоит.

**Результат:** бот работает на VPS как systemd-служба вместе с локальным Postgres, отвечает в новом чате, сам поднимается после падения и после перезагрузки. VPN на сервере работает так же, как до переезда.

**В объём не входит (это этап 2, отдельное ТЗ):** веб-игры «найди писюн» и казино (`/game`, `/casino`). Им нужен https-адрес, а порт 443 занят VPN. Пока не перенесём, ссылки из этих команд работать не будут, и это нормально.

---

## 2. Как работать

- Агент работает с Mac владельца и заходит на сервер командой `ssh root@<IP>`. Вход по ключу, без пароля.
- Команды выполнять неинтерактивно (`apt-get -y`, без редакторов). Долгие команды (`apt-get install`, `pip install`) запускать с таймаутом или в фоне.
- **Сначала предложить, потом делать.** Перед каждым этапом из раздела 6, который что-то устанавливает или меняет, коротко сказать владельцу, что сейчас будет, и дождаться «да». Чтение и проверки можно делать без спроса.
- Общаться по-русски, простым языком: владелец не профессиональный разработчик.
- Не усложнять: Docker, Ansible, CI, мониторинг, nginx и прочая инфраструктура не нужны.
- Если что-то пошло не по плану, остановиться и рассказать владельцу, а не чинить вслепую.

---

## 3. Сервер (состояние на 11.11.2026)

| Параметр | Значение |
|---|---|
| ОС | Ubuntu 24.04.5 LTS, x86_64 |
| CPU | 1 ядро |
| RAM | 1,9 ГБ, из них ~1,5 ГБ доступно; swap 511 МБ |
| Диск | 15 ГБ, свободно 9,6 ГБ |
| Часовой пояс системы | UTC |
| Docker | не установлен |
| Python | системный 3.12 (в проекте `.python-version` = 3.11, для бота разница не важна) |

**Что уже работает на сервере: VPN, 2 пользователя.** Панель 3x-ui (процесс `x-ui`) и ядро xray (`xray-linux-amd64`).

| Порт | Кто слушает |
|---|---|
| `*:443` | xray (VPN) |
| `<IP>:8443` | xray (VPN) |
| `*:2096` | x-ui |
| `127.0.0.1:11111`, `127.0.0.1:62789` | xray |
| `127.0.0.1:19609` | x-ui |
| `*:22` | sshd |

Порты 80, 5432 и 8080 свободны.

---

## 4. Жёсткие запреты

1. **Не трогать VPN:** x-ui, xray, их конфиги, службы и порты из таблицы выше. Не перезапускать их и не обновлять.
2. **Не трогать ssh и файрвол:** `sshd_config`, `ufw`, `iptables`/`nftables`. Ошибка здесь отрежет владельца от сервера или сломает VPN.
3. **Не перезагружать сервер** и не делать `apt upgrade` / `full-upgrade`. Ставить только пакеты из этого ТЗ.
4. **Не вводить и не читать секреты.** Токен Telegram, ключи Groq и DeepSeek и `ALLOWED_CHAT_ID` владелец вписывает в `.env` сам. Не делать `cat .env`, не выводить переменные окружения службы.
5. **Не запускать бота на сервере, пока он работает где-то ещё** (Railway, локально). С одним токеном Telegram отдаёт обновления только одному процессу, второй получает ошибку `Conflict`, и сообщения теряются.
6. Не пушить в GitHub и не менять историю git. Force-push делает владелец сам.
7. Бот работает не от root, а от отдельного системного пользователя.

---

## 5. Предусловия (проверить до начала, на Mac)

| # | Условие | Как проверить |
|---|---|---|
| П1 | На GitHub история без дампа (владелец сделал `git push --force origin main`) | `git fetch origin && git log origin/main --oneline -- backup.sql` — вывод пустой |
| П2 | Исправления `bot.py` закоммичены и запушены | `git status` чистый для `bot.py`, `main` совпадает с `origin/main` |
| П3 | Сервис бота на Railway остановлен или удалён | спросить владельца |
| П4 | Решено, что делать с портом веб-API (см. ниже) | спросить владельца |

**П4: порт веб-API.** В `bot.py` (функция `main()`) веб-сервер жёстко слушает `0.0.0.0:$PORT`. На VPS это откроет порт 8080 по http всем в интернете, на том же IP, что и VPN. Этого лучше избежать: лишний открытый сервис привлекает внимание к IP VPN, а на этапе 1 веб-API всё равно никому не нужен. Предложить владельцу правку в одну строку:

```python
site = web.TCPSite(runner, os.getenv("WEB_HOST", "0.0.0.0"), port)
```

На Railway ничего не изменится (значение по умолчанию то же), а на VPS в `.env` будет `WEB_HOST=127.0.0.1`. Такой вариант пригодится и на этапе 2: туннель Cloudflare подключается к локальному адресу. Правку делать **только с согласия владельца**, сохраняя CRLF в `bot.py`. Коммит по-русски, коротко, например «адрес веб-сервера из WEB_HOST». Если владелец откажется, явно предупредить, что порт 8080 будет открыт наружу.

---

## 6. Этапы работ

### Этап 0. Снимок «до» (только чтение)

```bash
free -h; df -h /; ss -tlnp; systemctl is-active x-ui; timedatectl | grep 'Time zone'
```

Сохранить вывод в отчёт. По нему в конце проверяем, что VPN не пострадал.

### Этап 1. Пакеты

```bash
apt-get update
apt-get install -y postgresql python3-venv git
```

Проверить:
- `systemctl is-active postgresql` → `active`;
- `ss -tlnp | grep 5432` → Postgres слушает **только** `127.0.0.1` / `::1`;
- порты VPN на месте.

### Этап 2. Пользователь и база

Системный пользователь, от которого работает бот:

```bash
useradd --system --create-home --home-dir /opt/summary_bot --shell /usr/sbin/nologin summarybot
```

База с входом через unix-сокет по peer-аутентификации: имя пользователя ОС совпадает с ролью в Postgres, поэтому пароль не нужен.

```bash
sudo -u postgres createuser summarybot
sudo -u postgres createdb -O summarybot summarybot
```

**Часовой пояс базы должен быть UTC.** Бот хранит `TIMESTAMPTZ` и сам прибавляет `TIMEZONE_OFFSET` при выводе времени. Если сессия Postgres не в UTC, время в сообщениях бота уедет.

```bash
sudo -u postgres psql -tAc "SHOW timezone"
```

Если ответ не `UTC` / `Etc/UTC`, то:

```bash
sudo -u postgres psql -c "ALTER DATABASE summarybot SET timezone TO 'UTC'"
```

Проверка подключения:

```bash
sudo -u summarybot psql "postgresql:///summarybot?host=/var/run/postgresql" -c "SELECT 1"
```

Настройки памяти Postgres не трогать, значений по умолчанию хватает.

### Этап 3. Код и зависимости

```bash
cd /opt/summary_bot
sudo -u summarybot git clone https://github.com/kolyaaru-code/summary_bot.git app
sudo -u summarybot python3 -m venv app/venv
sudo -u summarybot app/venv/bin/pip install --upgrade pip
sudo -u summarybot app/venv/bin/pip install -r app/requirements.txt
```

Проверить:
- `sudo -u summarybot git -C app log --oneline -- backup.sql` → пусто (в истории нет дампа);
- `sudo -u summarybot app/venv/bin/python -c "import aiogram, aiohttp, groq, openai, psycopg2, dotenv; print('ok')"` → `ok`.

Код лежит в `/opt/summary_bot/app`, а `.env` и бэкапы — рядом, вне git.

### Этап 4. `.env`

Агент создаёт файл `/opt/summary_bot/app/.env` (бот вызывает `load_dotenv()`, а рабочая папка службы — `app`). В файле заполнены только несекретные значения, секретные оставлены пустыми:

```dotenv
TG_TOKEN=
GROQ_KEY=
GROQ_KEY_2=
DEEPSEEK_KEY=
ALLOWED_CHAT_ID=
TIMEZONE_OFFSET=3
DATABASE_URL=postgresql:///summarybot?host=/var/run/postgresql
PORT=8080
WEB_HOST=127.0.0.1
```

(`WEB_HOST` нужен только если владелец согласился на правку из П4.)

```bash
chown summarybot:summarybot /opt/summary_bot/app/.env
chmod 600 /opt/summary_bot/app/.env
```

`.env` уже есть в `.gitignore`, проверить: `git -C /opt/summary_bot/app check-ignore .env`.

Дальше **владелец сам** заходит на сервер и заполняет ключи:

```bash
nano /opt/summary_bot/app/.env
```

Про `ALLOWED_CHAT_ID`: без него бот падает на старте. Если ID нового чата владелец ещё не знает, временно можно вписать ID старого чата, запустить бота, написать `/id` в новом чате (команда работает в любом чате), а потом заменить значение и перезапустить службу. Новая группа должна быть супергруппой (ID начинается с `-100`), подробнее в `CONTEXT.md`, раздел 9.

Агент проверяет только, что поля заполнены, не показывая значений:

```bash
grep -cE '^(TG_TOKEN|GROQ_KEY|GROQ_KEY_2|DEEPSEEK_KEY|ALLOWED_CHAT_ID)=.+' /opt/summary_bot/app/.env
```

Должно быть `5`. Если `DEEPSEEK_KEY` нет, бот работает на Groq, но это нежелательно: DeepSeek основная модель.

### Этап 5. systemd-служба

Файл `/etc/systemd/system/summary_bot.service`:

```ini
[Unit]
Description=summary_bot (Telegram bot)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
User=summarybot
Group=summarybot
WorkingDirectory=/opt/summary_bot/app
ExecStart=/opt/summary_bot/app/venv/bin/python -u bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
Environment=MALLOC_ARENA_MAX=2

# Чтобы бот никогда не мешал VPN
MemoryMax=400M
CPUWeight=50
Nice=5

# Простая защита
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

Пояснения:
- `Restart=always` + `RestartSec=10`: если после перезагрузки сервера Postgres ещё не готов, бот упадёт на подключении и через 10 секунд поднимется сам.
- `MemoryMax=400M`: обычно бот занимает ~100 МБ, лимит берётся с запасом. Если бот его превысит, systemd убьёт и перезапустит только бота.
- Логи пишет journald: `journalctl -u summary_bot`.

Запуск — **только после П3 (Railway остановлен) и этапа 4 (ключи вписаны):**

```bash
systemctl daemon-reload
systemctl enable --now summary_bot
```

### Этап 6. Проверка на сервере

```bash
systemctl is-active summary_bot
journalctl -u summary_bot -n 50 --no-pager
```

В логе должны быть строки:
- `Бот запущен и готов к работе!`
- `Планировщик дней рождений запущен`
- `Менеджер активности Даяны запущен`
- `Веб-сервер запущен на порту 8080`

В логе **не должно быть** `Conflict: terminated by other getUpdates request`. Если есть, бот ещё где-то запущен (см. запрет 5).

Убедиться, что таблицы создались:

```bash
sudo -u summarybot psql "postgresql:///summarybot?host=/var/run/postgresql" -c "\dt"
```

Должны быть `history`, `dayana_questions`, `peepee_scores`, `casino_balances`, `birthdays`, `bot_flags`.

Порт веб-API:

```bash
ss -tlnp | grep 8080
```

Если правка из П4 принята, должен быть `127.0.0.1:8080`, если нет — `0.0.0.0:8080`.

### Этап 7. Проверка в чате (делает владелец, агент подсказывает)

| Что | Ожидание |
|---|---|
| `/help` | список команд |
| `/id` в новом чате | ID совпадает с `ALLOWED_CHAT_ID` |
| несколько обычных сообщений, потом `/summary` | прожарка от «Бати» |
| голосовое сообщение, потом `/summary` | голосовое попало в саммари (расшифровка Groq Whisper) |
| «Даяна, ответь, …» | ответ Даяны |
| «Даяна, рассуди» реплаем на спор | вердикт |
| `/never`, затем `/neverstop` | игра запускается и останавливается |
| `/quote` | работает, когда в базе наберётся достаточно сообщений (от 4 слов, до 200 символов); до этого честно говорит, что цитат нет |
| `/birthday` | меню открывается, ДР сохраняется |

Утреннее приветствие (9–10 МСК), вечерний вопрос (21–22 МСК) и поздравления с ДР (08:00 МСК) проверить на следующий день по логу и в чате.

### Этап 8. Ежедневный бэкап базы

История чата хранится вечно, терять её жалко. Простой вариант: `pg_dump` раз в сутки, хранить 14 дней.

Файл `/etc/cron.d/summary_bot_backup`:

```cron
30 3 * * * summarybot pg_dump -d summarybot -h /var/run/postgresql | gzip > /opt/summary_bot/backup-$(date +\%F).sql.gz && find /opt/summary_bot -maxdepth 1 -name 'backup-*.sql.gz' -mtime +14 -delete
```

Проверить: запустить команду один раз руками от `summarybot` и убедиться, что файл появился и не пустой.

Предупредить владельца: бэкап лежит на том же сервере. От случайного удаления данных он спасёт, от смерти сервера — нет. Если нужно, архив можно иногда скачивать на Mac:

```bash
scp root@<IP>:/opt/summary_bot/backup-*.sql.gz ~/Downloads/
```

В git бэкапы не класть: `*.sql` уже в `.gitignore`, но `.sql.gz` туда не входит.

### Этап 9. Снимок «после» и сравнение

Повторить команды этапа 0 и сравнить:
- порты VPN те же, `x-ui` активен;
- добавились только `127.0.0.1:5432` (Postgres) и `8080` (бот);
- свободной памяти примерно на 150–250 МБ меньше, чем было.

Попросить владельца проверить, что VPN подключается.

---

## 7. Как обновлять бота дальше

Записать владельцу памятку. После пуша в GitHub:

```bash
ssh root@<IP>
sudo -u summarybot git -C /opt/summary_bot/app pull
systemctl restart summary_bot
journalctl -u summary_bot -n 30 --no-pager
```

Если менялся `requirements.txt`, перед рестартом ещё:

```bash
sudo -u summarybot /opt/summary_bot/app/venv/bin/pip install -r /opt/summary_bot/app/requirements.txt
```

Полезные команды:
- статус: `systemctl status summary_bot`
- живой лог: `journalctl -u summary_bot -f`
- остановить: `systemctl stop summary_bot`

---

## 8. Откат

Если что-то сломалось и быстро не чинится:

```bash
systemctl disable --now summary_bot
```

VPN при этом не задевается. Postgres можно оставить, он почти ничего не ест, или тоже остановить: `systemctl disable --now postgresql`. Вернуть бота на Railway можно, когда там обнулится лимит, но тогда сервис на VPS должен быть выключен.

Полное удаление (только по прямой просьбе владельца, данные пропадут):

```bash
systemctl disable --now summary_bot
rm /etc/systemd/system/summary_bot.service /etc/cron.d/summary_bot_backup
systemctl daemon-reload
sudo -u postgres dropdb summarybot && sudo -u postgres dropuser summarybot
userdel -r summarybot
```

---

## 9. Критерии готовности

- [ ] Предусловия П1–П4 выполнены или решения по ним приняты владельцем
- [ ] `summary_bot.service` в состоянии `active (running)` и включён в автозапуск (`enabled`)
- [ ] В логе нет ошибок `Conflict` и ошибок подключения к базе
- [ ] Все 6 таблиц созданы в локальном Postgres; часовой пояс базы — UTC
- [ ] Бот отвечает в новом чате: `/help`, `/summary`, Даяна
- [ ] Бот поднимается сам: `systemctl kill summary_bot`, через ~10 секунд снова `active`
- [ ] Бэкап создаётся, файл не пустой
- [ ] VPN работает: порты те же, `x-ui` активен, владелец подтвердил подключение
- [ ] Владелец получил памятку из раздела 7

## 10. Отчёт владельцу

Коротко, по-русски:
1. Что сделано, по этапам.
2. Что проверено и как: команды и результаты.
3. Что **не** проверено (например, утренние сообщения, которые придут только завтра).
4. Сколько памяти теперь свободно на сервере.
5. Что осталось на этап 2: веб-игры через https (вероятно, туннель Cloudflare) и смена адреса в `docs/index.html` и `docs/casino.html` (сейчас там `summarybot-production-0954.up.railway.app`).
