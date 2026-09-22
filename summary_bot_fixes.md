# summary_bot — план исправлений и улучшений

Документ собран по результатам разбора `bot.py` и лога `/summary 12`.
Все места указаны **по строкам-ориентирам в коде**, без номеров строк.

---

## Как пользоваться этим документом

Блоки идут по приоритету. Делай их по порядку и **после каждого блока** проверяй:

```
python3 -m py_compile bot.py && echo OK
```

Если вывело `OK` — перезапускай бота и прогоняй тесты из раздела «Чек-лист проверки» в конце.

| Блок | Что | Срочность |
|---|---|---|
| A | Саммари и резервные модели | 🔴 сейчас, без этого `/summary N` не работает |
| B | Бот зависает во время генерации | 🟠 важно |
| C | База данных | 🟡 желательно до роста истории и до RAG |
| D | Логические баги | 🟡 желательно |
| E | Мелочи надёжности и безопасности | 🟢 по желанию |

Общие правила вставки кода:
- отступы — только пробелы, никаких табов;
- «Найти → Заменить» — ищи строку целиком, в редакторе это `Ctrl+H`;
- если сказано «между строкой X и строкой Y» — сами строки X и Y остаются на месте.

---

## Что уже выяснили по логу `/summary 12`

1. **DeepSeek не вызывался вообще.** Ошибка `UnboundLocalError: cannot access local variable 'completion'` — при прошлой вставке потерялась строка с вызовом `client_deepseek.chat.completions.create(...)`. Код пытался прочитать ответ, которого не было. Чинится в A1.
2. **Все три модели Groq удалены.** Ошибка `404 model_not_found` для `llama-3.3-70b-versatile`, `llama-3.1-8b-instant` и `qwen/qwen3-32b`. Groq отключил их для бесплатного тарифа летом 2026 (Llama — 16 августа, Qwen3-32B — в июле). Значит, с середины августа резерв мёртв **во всём боте**, всё держится только на DeepSeek. Чинится в A2–A5.
3. **Почему раньше `/summary` работал только за 1 час.** Раз Groq мёртв, все успешные саммари делал DeepSeek — значит, ключ и соединение в порядке. Гипотеза: вызов шёл без отключения режима размышлений и с `max_tokens=2000`. На длинной переписке размышления съедают весь лимит, ответ приходит пустым, бот идёт в мёртвый резерв. Гипотеза подтвердится или опровергнется строкой `finish_reason=...` в логе после A1.

---

# Блок A. Саммари и резервные модели 🔴

## A1. Ветка DeepSeek в `get_ai_summary`

**Что не так.** В `_dayana_complete` режим размышлений явно выключается (`"thinking": {"type": "disabled"}`), а в `get_ai_summary` — нет. Плюс `max_tokens=2000` — мало для саммари за сутки. Плюс при прошлой правке потерялся сам вызов модели.

**Что делаем.** Заменяем всю ветку DeepSeek целиком, чтобы гарантированно убрать следы неудачной вставки. Добавляем:
- `extra_body={"thinking": {"type": "disabled"}}` — модель сразу пишет ответ, не тратя лимит на размышления;
- `max_tokens=4000` — с запасом на длинный разбор;
- `timeout=180` — если DeepSeek завис, через 3 минуты бот уйдёт в резерв, а не будет ждать вечно;
- лог `finish_reason` — главный диагностический признак: `stop` — всё хорошо, `length` — упёрлись в лимит.

**Где.** В функции `get_ai_summary` найди строку:

```python
    print(f"\n--- [LOG] СТАРТ САММАРИ: Сообщений: {message_count}, Период: {timeframe_text} ---")
```

и строку:

```python
    sampled_text = build_prompt_text(rows)
```

**Всё между ними удали** и вставь:

```python
    if client_deepseek is not None:
        try:
            full_text = format_full_log(rows)
            prompt = _build_summary_prompt(full_text, timeframe_text, message_count)
            print(f"[LOG] DeepSeek: Длина отправляемого текста = {len(prompt)} символов.")

            completion = client_deepseek.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.85,
                max_tokens=4000,
                extra_body={"thinking": {"type": "disabled"}},
                timeout=180,
            )

            choice = completion.choices[0]
            answer = choice.message.content
            print(f"[LOG] DeepSeek: finish_reason={choice.finish_reason}, длина={len(answer) if answer else 0}")

            if answer and answer.strip():
                return answer
            else:
                print("[LOG] ⚠️ DeepSeek вернул абсолютно пустую строку! Иду к резерву...")
        except Exception as e:
            print(f"[LOG] ❌ DeepSeek УПАЛ с ошибкой: {type(e).__name__} - {e}")
            traceback.print_exc()

```

Строка `import traceback` выше ориентира остаётся — она нужна.

---

## A2. Замена мёртвых моделей Groq по всему файлу

**Что не так.** Названия моделей захардкожены в восьми местах: саммари, блок Даяны в саммари, диспетчер Даяны, рассуди, виноват, посоветуй, поздравления, игра «Я никогда не». Все три модели больше не существуют.

**Что делаем.** Меняем на рекомендованные Groq замены. Через `Ctrl+H` → «Заменить все». Ищи **вместе с кавычками**, чтобы не задеть ничего лишнего:

| Найти | Заменить на |
|---|---|
| `"llama-3.3-70b-versatile"` | `"openai/gpt-oss-120b"` |
| `"llama-3.1-8b-instant"` | `"openai/gpt-oss-20b"` |
| `"qwen/qwen3-32b"` | `"qwen/qwen3.6-27b"` |

`"whisper-large-v3-turbo"` (распознавание голосовых) **не трогай** — эта модель не отключалась.

**Важно.** Новые модели устроены иначе: GPT-OSS — модели с размышлениями, они тратят часть лимита ответа на «подумать», прежде чем писать текст. Если дать им маленький `max_tokens`, ответ придёт пустым. Поэтому одной замены названий мало — нужны правки A3–A5.

---

## A3. Вызов Groq в `get_ai_summary`

**Что не так.** Вызов не учитывает, что новые модели размышляют. Для Qwen есть ещё риск, что размышления попадут прямо в текст ответа в виде тегов `<think>...</think>` и улетят в чат.

**Что делаем.**
- для GPT-OSS: `reasoning_effort: "low"` — думать коротко, лимит оставить на ответ;
- для Qwen: `reasoning_format: "hidden"` — размышления не показывать в ответе;
- `max_tokens=4000` — если ты ставил 1500 по моему прошлому совету, это откатывается: под новые модели 1500 мало.

**Где.** В `get_ai_summary`, в цикле Groq найди строку:

```python
            print(f"[LOG] Groq: Пробую модель {model}...")
```

и ближайшую строку под ней:

```python
            answer = completion.choices[0].message.content
```

**Всё между ними удали** (это старый вызов `client.chat.completions.create(...)` целиком, со скобками) и вставь:

```python
            groq_kwargs = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.85,
                "max_tokens": 4000,
            }
            if model.startswith("openai/gpt-oss"):
                groq_kwargs["extra_body"] = {"reasoning_effort": "low"}
            elif model.startswith("qwen/"):
                groq_kwargs["extra_body"] = {"reasoning_format": "hidden"}
            completion = client.chat.completions.create(**groq_kwargs)
```

> Если Groq ответит ошибкой `400` про параметр `reasoning_format` — убери две строки с `elif model.startswith("qwen/")`. Для GPT-OSS параметр `reasoning_effort` документирован, для новой Qwen 3.6 я его не проверял.

---

## A4. Резерв Groq в `_dayana_complete` (Даяна, поздравления, утро/вечер)

**Что не так — три проблемы сразу.**
1. Та же история с размышлениями: у утренних и вечерних сообщений `max_tokens=300`, GPT-OSS может потратить их целиком на размышления и вернуть пустоту.
2. Пустой ответ от Groq не проверяется — Даяна может отправить в чат пустое сообщение.
3. Любая ошибка, кроме `rate_limit` и `model`, делает `raise` и **пропускает `fallback_text`**. То есть заготовленная запасная фраза не срабатывает, пользователь видит «Не могу ответить».

**Что делаем.** Перебираем модели до первого непустого ответа; при любой ошибке идём к следующей; если все упали — отдаём `fallback_text`. Лимит для Groq ограничен сверху 4000 (на бесплатном тарифе огромные `max_tokens` вроде 15000 из `ask_dayana` могут упереться в лимит токенов в минуту), а для GPT-OSS — не меньше 1024, чтобы хватило и на размышления, и на ответ.

**Где.** В функции `_dayana_complete` найди строку:

```python
            print(f"DeepSeek (Даяна) недоступен ({e}), откатываюсь на Groq...")
```

и строку:

```python
    if fallback_text is not None:
```

**Всё между ними удали** и вставь:

```python

    if groq_models is None:
        groq_models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    for model in groq_models:
        try:
            groq_max = min(max_tokens, 4000)
            groq_kwargs = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": groq_max,
            }
            if model.startswith("openai/gpt-oss"):
                groq_kwargs["max_tokens"] = max(groq_max, 1024)
                groq_kwargs["extra_body"] = {"reasoning_effort": "low"}
            elif model.startswith("qwen/"):
                groq_kwargs["extra_body"] = {"reasoning_format": "hidden"}
            completion = client_dayana.chat.completions.create(**groq_kwargs)
            answer = completion.choices[0].message.content
            if answer and answer.strip():
                return answer
            print(f"Даяна: {model} вернула пустой ответ, пробую следующую...")
        except Exception as e:
            print(f"Даяна: {model} недоступна ({type(e).__name__}: {e}), переключаюсь...")
            continue

```

Заодно этот блок гарантированно уберёт опечатку `:q`, если она где-то осталась.

---

## A5. Игра «Я никогда не» — с августа выдаёт одну и ту же фразу

**Что не так.** Функция `generate_never_phrase` работает **только через Groq**, без DeepSeek. Модели удалены → ошибка 404 → она не `rate_limit` → `raise` → в `never_next_round` срабатывает запасная фраза. Итог: с 16 августа каждый раунд каждой игры — «Я никогда не делал что-то о чём потом жалел». Даже после замены названий (A2) останется проблема с `max_tokens=100`: GPT-OSS потратит их на размышления и вернёт пустоту.

**Что делаем.** Переводим генерацию на общий диспетчер `_dayana_complete`: DeepSeek основной, Groq резерв, запасная фраза — последний рубеж. Промпт не меняется.

**Где.** В функции `generate_never_phrase` найди строку (конец промпта):

```python
Выдай ТОЛЬКО одну фразу начиная со слов "Я никогда не". Без кавычек, без пояснений.
"""
```

и строку, с которой начинается следующая функция:

```python
def build_never_join_text(game: dict) -> str:
```

**Всё между ними удали** (цикл `for model in [...]` и финальный `return`) и вставь:

```python
    phrase = _dayana_complete(
        prompt,
        ds_model="deepseek-v4-flash",
        thinking=False,
        groq_models=["openai/gpt-oss-120b", "qwen/qwen3.6-27b"],
        temperature=0.95,
        max_tokens=150,
        fallback_text="Я никогда не делал что-то о чём потом жалел",
    )
    phrase = phrase.strip().strip('"\'«»')
    if not phrase.lower().startswith("я никогда не"):
        phrase = "Я никогда не " + phrase
    return phrase

```

Оставь пустую строку перед `def build_never_join_text`.

---

# Блок B. Бот зависает во время генерации 🟠

**Что не так.** Бот асинхронный (aiogram), но все запросы к ИИ — синхронные. Пока Батя пишет саммари за сутки (30–60 секунд) или Даяна выносит вердикт в режиме размышлений, **весь бот стоит**: не отвечает на команды, не сохраняет сообщения, таймеры игр съезжают, кнопки не нажимаются.

**Что делаем.** Оборачиваем каждый вызов ИИ в `await asyncio.to_thread(...)`. Запрос уходит в отдельный поток, а бот в это время продолжает работать. Логика функций не меняется — меняется только то, как их вызывают.

**Где.** Все замены — через «Найти → Заменить». Все эти строки находятся внутри `async def`, поэтому `await` там допустим.

| # | Найти | Заменить на |
|---|---|---|
| 1 | `transcription = client.audio.transcriptions.create(model="whisper-large-v3-turbo", file=buffer)` | `transcription = await asyncio.to_thread(client.audio.transcriptions.create, model="whisper-large-v3-turbo", file=buffer)` |
| 2 | `raw_summary = get_ai_summary(all_rows, f"{hours} ч.", len(all_rows))` | `raw_summary = await asyncio.to_thread(get_ai_summary, all_rows, f"{hours} ч.", len(all_rows))` |
| 3 | `dayana_comments = get_dayana_block(dayana_questions)` | `dayana_comments = await asyncio.to_thread(get_dayana_block, dayana_questions)` |
| 4 | `raw_advice = dayana_advise(user_topic, author)` *(встречается 2 раза — «Заменить все»)* | `raw_advice = await asyncio.to_thread(dayana_advise, user_topic, author)` |
| 5 | `answer = ask_dayana(question)` | `answer = await asyncio.to_thread(ask_dayana, question)` |
| 6 | `verdict = dayana_judge(format_context(rows), hint)` | `verdict = await asyncio.to_thread(dayana_judge, format_context(rows), hint)` |
| 7 | `guilty = dayana_guilty(format_context(rows), hint)` | `guilty = await asyncio.to_thread(dayana_guilty, format_context(rows), hint)` |
| 8 | `phrase = generate_never_phrase(players_list, chat_context, game["used_phrases"], category)` | `phrase = await asyncio.to_thread(generate_never_phrase, players_list, chat_context, game["used_phrases"], category)` |
| 9 | `text = dayana_birthday_morning(user_name, age)` | `text = await asyncio.to_thread(dayana_birthday_morning, user_name, age)` |
| 10 | `text = dayana_birthday_midday(user_name, age)` | `text = await asyncio.to_thread(dayana_birthday_midday, user_name, age)` |
| 11 | `text_general = dayana_generate_morning_general()` | `text_general = await asyncio.to_thread(dayana_generate_morning_general)` |
| 12 | `text_personal = dayana_generate_morning_personal(user)` | `text_personal = await asyncio.to_thread(dayana_generate_morning_personal, user)` |
| 13 | `text_general = dayana_generate_evening_general()` | `text_general = await asyncio.to_thread(dayana_generate_evening_general)` |
| 14 | `text_personal = dayana_generate_evening_personal(user)` | `text_personal = await asyncio.to_thread(dayana_generate_evening_personal, user)` |
| 15 | `text = dayana_generate_bait(context)` | `text = await asyncio.to_thread(dayana_generate_bait, context)` |

Обрати внимание: в `to_thread` функция передаётся **без скобок**, а её аргументы — через запятую после неё.

**Чего НЕ делать.** Не оборачивай в `to_thread` функции работы с БД (`save_message`, `get_last_messages` и т.д.). Пул `SimpleConnectionPool` не рассчитан на работу из нескольких потоков — можно получить трудноуловимые сбои. Запросы к БД короткие, их блокировка некритична. Если когда-нибудь понадобится — сначала заменить `SimpleConnectionPool` на `ThreadedConnectionPool`.

---

# Блок C. База данных 🟡

## C1. Индексы

**Что не так.** В таблицах нет ни одного индекса. Все выборки идут по `chat_id` + `timestamp`, а история теперь хранится вечно. Сейчас это незаметно, но с каждым месяцем `/summary`, «рассуди», реаниматор и игры будут читать всю таблицу целиком — и медленнее.

**Что делаем.** Индекс по `(chat_id, timestamp)` для истории и для вопросов Даяне. `IF NOT EXISTS` — безопасно при каждом перезапуске. На уже существующей таблице индекс построится при первом запуске (на объёмах чата это секунды).

## C2. Колонка `user_id` в истории

**Что не так.** В `history` хранится только имя (`user_name`). Люди меняют имена в Telegram — и для бота это уже «другой человек». Для будущего «Даяна вспомни» (RAG по истории) это критично: поиск будет ссылаться на старые имена, а статистику по человеку не собрать.

**Что делаем.** Добавляем колонку `user_id`. Старые сообщения останутся с пустым `user_id` — это нормально, новые будут сохраняться с ним.

## C3. Таблица флагов (нужна для D4)

Сразу создаём таблицу `bot_flags` — в ней бот будет отмечать «уже поздравил», «уже пожелал доброе утро», чтобы не повторяться после перезапуска. Подробности в D4.

## Вставка в `init_db` (C1 + C2 + C3 разом)

**Где.** В функции `init_db` найди строку:

```python
        print("БД инициализирована")
```

Прямо над ней стоит строка `        conn.commit()`. **Перед этой строкой `conn.commit()`** (то есть после последнего `cursor.execute(...)` про таблицу `birthdays`) вставь:

```python
            cursor.execute('ALTER TABLE history ADD COLUMN IF NOT EXISTS user_id BIGINT')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_history_chat_ts ON history (chat_id, timestamp DESC)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_dayana_q_chat_ts ON dayana_questions (chat_id, timestamp DESC)')
            cursor.execute('''CREATE TABLE IF NOT EXISTS bot_flags
                             (flag_key TEXT PRIMARY KEY,
                              created_at TIMESTAMPTZ DEFAULT NOW())''')
```

Отступ — 12 пробелов, как у соседних `cursor.execute`.

## C4. Сохранение `user_id` в `save_message`

**Где.** Три замены через «Найти → Заменить»:

| Найти | Заменить на |
|---|---|
| `def save_message(chat_id, user_name, text):` | `def save_message(chat_id, user_name, text, user_id=None):` |
| `'INSERT INTO history (chat_id, user_name, message_text) VALUES (%s, %s, %s)',` | `'INSERT INTO history (chat_id, user_name, message_text, user_id) VALUES (%s, %s, %s, %s)',` |
| `(chat_id, user_name, text)` | `(chat_id, user_name, text, user_id)` |

Строка `(chat_id, user_name, text)` в файле одна — в `save_dayana_question` там `question`, её замена не заденет.

`user_id=None` по умолчанию — значит, старые вызовы без `user_id` не сломаются. Передавать `user_id` начнём в D1.

## C5. Лишняя проверка соединения (не трогать сейчас)

`get_conn()` делает `SELECT 1` перед каждым запросом — это лишний запрос к БД на каждое сохранённое сообщение. Для чата на 20 человек нагрузка ничтожная, а защита от «протухших» соединений полезна. Оставляем как есть; вернуться, если БД будет на удалённом сервере с заметной задержкой.

---

# Блок D. Логические баги 🟡

## D1. Сообщения к Даяне не попадают в историю

**Что не так.** В обработчике `collect_messages` все ветки Даяны («ответь», «рассуди», «виноват», «посоветуй», «кто тебя создал») заканчиваются `return` — **до** блока сохранения в историю. Самые содержательные реплики чата выпадают из саммари Бати и из будущего поиска по истории.

**Что делаем.** Переносим блок сохранения **выше** веток Даяны. Сообщение сначала сохраняется, потом обрабатывается. Заодно добавляем `user_id` (из C4) и подписи к фото (D2).

Ответы на ForceReply (ввод даты ДР и темы совета) по-прежнему не сохраняются — это служебный ввод, в саммари он не нужен.

**Шаг 1 — удалить старый блок.** В конце функции `collect_messages` найди строку:

```python
    # ── Сохраняем в историю ──
```

Удали её и всё, что ниже, **до строки** `async def main():` (сама `async def main():` остаётся). Это блок из `if message.text: save_message(...)` / `elif message.voice:` / `elif message.video_note:`.

**Шаг 2 — вставить новый блок выше.** Найди строку:

```python
    # ── Обработка текстовых сообщений с упоминанием Даяны ──
```

и **перед ней** вставь:

```python
    # ── Сохраняем в историю ──
    if message.text:
        save_message(message.chat.id, author, message.text, user_id=user_id)
        print(f"[{author}]: {message.text}")
    elif message.voice:
        text = await transcribe_audio(message.voice.file_id, "voice.ogg", message.voice.file_size or 0)
        save_message(message.chat.id, author, f"[🎤 Голосовое]: {text}" if text else "[🎤 Голосовое]: не удалось распознать", user_id=user_id)
    elif message.video_note:
        text = await transcribe_audio(message.video_note.file_id, "video_note.mp4", message.video_note.file_size or 0)
        save_message(message.chat.id, author, f"[📹 Кружочек]: {text}" if text else "[📹 Кружочек]: не удалось распознать", user_id=user_id)
    elif message.caption:
        save_message(message.chat.id, author, f"[🖼 Медиа с подписью]: {message.caption}", user_id=user_id)

```

Если C4 ещё не сделан — убери `, user_id=user_id` из всех четырёх вызовов, иначе будет ошибка.

**Побочный эффект (полезный).** Сообщение «Даяна рассуди» теперь само попадает в контекст, который Даяна читает. Это только помогает: она видит, о чём её просят.

## D2. Подписи к фото и видео терялись

**Что не так.** Сохранялись только текст, голосовые и кружочки. Фото или видео с подписью — мимо истории, хотя подпись часто и есть суть сообщения («смотрите, что Вася натворил»).

**Что делаем.** Уже учтено в новом блоке D1 — ветка `elif message.caption:`.

**По желанию.** Чтобы Батя понимал новую пометку, в `_build_summary_prompt` найди строку:

```python
Голосовые [🎤] и кружочки [📹] — полноценные сообщения.
```

и замени на:

```python
Голосовые [🎤], кружочки [📹] и подписи к медиа [🖼] — полноценные сообщения.
```

## D3. Часовой пояс задан двумя разными способами

**Что не так.** Есть настройка `TIMEZONE_OFFSET` из `.env` — по ней форматируется время в саммари и контексте. Но в трёх местах сдвиг +3 захардкожен. Если когда-нибудь поменяешь `TIMEZONE_OFFSET`, время в саммари сдвинется, а поздравления, доброе утро и реаниматор — нет.

**⚠️ Сначала проверь `.env` на сервере:**

```
grep TIMEZONE_OFFSET .env
```

Должно быть `TIMEZONE_OFFSET=3`. Если строки нет — по умолчанию подставится `0`, и после этой правки утро, вечер и дни рождения **съедут на 3 часа**. В этом случае сначала добавь `TIMEZONE_OFFSET=3` в `.env`.

**Где.** «Найти → Заменить»:

| Найти | Заменить на |
|---|---|
| `now = datetime.now(timezone.utc) + timedelta(hours=3)` | `now = datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)` |
| `now_msk = now + timedelta(hours=3)` *(2 раза — «Заменить все»)* | `now_msk = now + timedelta(hours=TIMEZONE_OFFSET)` |

Константы `BIRTHDAY_MORNING_UTC` и `BIRTHDAY_MIDDAY_UTC` заданы в UTC — их не трогаем.

## D4. После перезапуска бот повторяет утро, вечер и поздравления

**Что не так.** Отметки «уже отправил» живут в памяти процесса: `sent_keys` в `birthday_checker`, `morning_done` / `evening_done` / `bait_done` в `dayana_activity_manager`. Перезапуск бота обнуляет их. Перезапустил в 09:40 — Даяна второй раз пожелает доброго утра. Перезапустил в первые 5 минут окна поздравлений — именинника поздравят дважды.

**Что делаем.** Отметки дублируем в БД, в таблицу `bot_flags` (создана в C3). Используем один трюк Postgres: `INSERT ... ON CONFLICT DO NOTHING`. Если строка вставилась — мы первые, отправляем. Если такая уже есть — уже отправляли, пропускаем. Работает даже при двух одновременно запущенных копиях бота.

Если БД недоступна, функция вернёт «уже отправлено» — лучше пропустить одно утреннее сообщение, чем заспамить чат.

**Шаг 1 — функция.** Найди строку:

```python
# 3. КАЗИНО — БД
```

и **перед ней** вставь:

```python
def try_claim_flag(key: str) -> bool:
    """True — флаг поставлен сейчас впервые, можно действовать. False — уже был."""
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                'INSERT INTO bot_flags (flag_key) VALUES (%s) ON CONFLICT (flag_key) DO NOTHING',
                (key,)
            )
            claimed = cursor.rowcount == 1
        conn.commit()
        return claimed
    except Exception as e:
        print(f"Ошибка флага {key}: {e}")
        conn.rollback()
        return False
    finally:
        release_conn(conn)

```

**Шаг 2 — дни рождения.** В `birthday_checker` найди:

```python
                if key in sent_keys:
                    continue

                sent_keys.add(key)
```

и замени на:

```python
                if key in sent_keys:
                    continue

                sent_keys.add(key)
                if not try_claim_flag(f"bday:{user_id}:{period}:{today_str}"):
                    continue
```

**Шаг 3 — доброе утро.** В `dayana_activity_manager` найди:

```python
                user = get_random_active_user(ALLOWED_CHAT_ID)
                morning_done = True
```

и замени на:

```python
                morning_done = True
                if not try_claim_flag(f"morning:{today_str}"):
                    continue
                user = get_random_active_user(ALLOWED_CHAT_ID)
```

**Шаг 4 — вечер.** Там же найди:

```python
                user = get_random_active_user(ALLOWED_CHAT_ID)
                evening_done = True
```

и замени на:

```python
                evening_done = True
                if not try_claim_flag(f"evening:{today_str}"):
                    continue
                user = get_random_active_user(ALLOWED_CHAT_ID)
```

**Шаг 5 — реаниматор.** Там же найди:

```python
                    if diff.total_seconds() > 5 * 3600:
```

и **сразу под ней** вставь (отступ 24 пробела):

```python
                        bait_done = True
                        if not try_claim_flag(f"bait:{today_str}"):
                            continue
```

Строка `bait_done = True` ниже, после отправки, останется — она не мешает.

**Шаг 6 — чистка старых флагов.** В `cleanup_old_messages` найди:

```python
            deleted_dayana = cursor.rowcount
```

и **сразу под ней** вставь:

```python
            cursor.execute("DELETE FROM bot_flags WHERE created_at < NOW() - INTERVAL '7 days'")
```

## D5. Игры зависают после перезапуска (только к сведению)

Состояние игр «Я никогда не» и «Угадай автора» хранится в памяти. Перезапуск посреди игры — сообщение с кнопками остаётся в чате, но на нажатия бот ответит «игра закончилась». Это не ломает бота, лечится новой командой `/never` или `/quote`. Переносить игры в БД — много работы ради редкого случая. **Не трогаем.**

---

# Блок E. Мелочи надёжности и безопасности 🟢

## E1. Кулдаун на `/summary`

**Что не так.** Любой может отправить `/summary 48` десять раз подряд — это десять дорогих запросов к DeepSeek и десять зависаний бота (до блока B).

**Что делаем.** Не чаще одного саммари в минуту на чат.

**Шаг 1 — константа.** Найди строку:

```python
MAX_PROMPT_CHARS = 9000
```

и **под ней** вставь:

```python
SUMMARY_COOLDOWN = 60      # секунд между /summary в одном чате
```

**Шаг 2 — хранилище.** Найди строку:

```python
advice_waiting: dict = {}
```

и **под ней** вставь:

```python

# Время последнего /summary: chat_id -> timestamp
summary_cooldown: dict = {}
```

**Шаг 3 — проверка.** В `cmd_summary` найди строку:

```python
    status_msg = await message.answer(f"⏳ Читаю ваш бред за последние {hours} ч...")
```

и **перед ней** вставь:

```python
    now_ts = time.time()
    last_ts = summary_cooldown.get(message.chat.id, 0)
    if now_ts - last_ts < SUMMARY_COOLDOWN:
        wait = int(SUMMARY_COOLDOWN - (now_ts - last_ts))
        await message.answer(f"⏳ Батя ещё не отдышался. Подожди {wait} сек.")
        return
    summary_cooldown[message.chat.id] = now_ts
```

Проверка стоит после разбора аргументов — значит, опечатка вроде `/summary abc` не «сжигает» минуту.

## E2. Внутренние ошибки уходят наружу через веб-API

**Что не так.** Эндпоинты игры и казино при ошибке отдают браузеру `str(e)` — текст исключения, где могут быть детали БД и структура кода. API открыт всем (`Access-Control-Allow-Origin: *`).

**Что делаем.** «Найти → Заменить все»:

| Найти | Заменить на |
|---|---|
| `return web.json_response({"error": str(e)}, status=500)` | `return web.json_response({"error": "Internal error"}, status=500)` |

Минус: в логах сервера этих ошибок не будет (кроме `casino_spin`, там уже есть `print`). Если нужно видеть их в логах — перед каждой такой строкой добавь `print(f"Ошибка API: {e}")` с тем же отступом.

## E3. Мёртвая переменная `RAILWAY_URL`

После переезда с Railway не используется нигде. Найди и удали строку:

```python
RAILWAY_URL = os.getenv("RAILWAY_URL", "")
```

Из `.env` на сервере её тоже можно убрать.

## E4. К сведению, без правок

- **`ALLOWED_CHAT_ID = int(os.getenv(...))`** упадёт при старте, если переменной нет в `.env`. Это правильное поведение («упасть сразу и громко»), оставляем.
- **`send_long_message`** при очень длинном тексте без переносов режет его ровно по 4000 символам и теоретически может разрезать HTML-сущность вроде `&amp;` пополам — Telegram откажется отправлять. На практике у ответов модели переносы есть всегда. Оставляем.
- **HMAC-ключ игр — это токен бота.** Если когда-нибудь перевыпустишь токен в BotFather, все активные ссылки на игру и казино станут недействительными. Живут они 5 минут, так что ничего страшного.

---

# Что сделано хорошо — не трогать

- **Токены для игр** подписаны HMAC с временем жизни 5 минут и сравниваются через `hmac.compare_digest`. Подделать результат или баланс из браузера нельзя.
- **Экранирование HTML** перед отправкой сделано во всех местах, где идёт текст от модели. Модель не сможет сломать разметку или вставить ссылку.
- **Каскад моделей** DeepSeek → Groq → запасная фраза — правильная архитектура. Сломался он только потому, что Groq удалил модели, а не из-за самой идеи.
- **Промпты** — сильная сторона бота. Персонажи выдержаны, в `dayana_advise` есть фильтр чувствительных тем, в «рассуди» и «виноват» схема «сначала подумай про себя, потом пиши» заметно улучшает вердикты.
- **Подробные `[LOG]`-строки в саммари** — именно благодаря им поломку нашли по одному скриншоту.

---

# Чек-лист проверки

После каждого блока: `python3 -m py_compile bot.py && echo OK` → перезапуск бота → тесты.

**После блока A:**
- [ ] `/summary` — работает, в логе `finish_reason=stop`
- [ ] `/summary 12` — работает, в логе `finish_reason=stop`
- [ ] `/summary 24` — работает
- [ ] `Даяна ответь сколько будет 2+2` — отвечает
- [ ] `/never` — разные фразы в разных раундах
- [ ] Резерв Groq: временно закомментируй `DEEPSEEK_KEY` в `.env`, перезапусти, проверь `/summary 3` и «Даяна ответь». В логе — строки Groq без 404. **Верни ключ обратно.**

**После блока B:**
- [ ] Запусти `/summary 24` и, пока «⏳ Читаю ваш бред…», отправь `/help` — бот должен ответить на `/help` сразу, не дожидаясь саммари

**После блока C:**
- [ ] В логе при старте нет ошибок `init_db`
- [ ] Отправь сообщение → в БД: `SELECT user_name, user_id FROM history ORDER BY id DESC LIMIT 3;` — у новых строк заполнен `user_id`

**После блока D:**
- [ ] `Даяна ответь ...` → через минуту `/summary` — вопрос виден в разборе
- [ ] Фото с подписью → `/summary` — подпись видна
- [ ] Перезапуск бота в окне 09:00–10:00 после утреннего сообщения — повтора нет

**После блока E:**
- [ ] Два `/summary` подряд — второй отвечает «Подожди N сек.»
