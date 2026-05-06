import asyncio
import os
import io
import random
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv
from groq import Groq
from psycopg2 import pool

# 1. НАСТРОЙКИ
load_dotenv()
TOKEN = os.getenv("TG_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID"))
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

MAX_VOICE_SIZE_MB = 5
MAX_MESSAGE_LENGTH = 4000
MAX_TEXT_LENGTH = 4000
MAX_PROMPT_CHARS = 9000

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = Groq(api_key=GROQ_KEY)

# 2. ПУЛ СОЕДИНЕНИЙ С БД
db_pool = None

def init_db_pool():
    global db_pool
    db_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL)
    print("Пул соединений с БД создан")

def get_conn():
    return db_pool.getconn()

def release_conn(conn):
    db_pool.putconn(conn)

def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''CREATE TABLE IF NOT EXISTS history 
                             (id SERIAL PRIMARY KEY, 
                              chat_id BIGINT,
                              user_name TEXT, 
                              message_text TEXT, 
                              timestamp TIMESTAMPTZ DEFAULT NOW())''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS peepee_scores
                             (id SERIAL PRIMARY KEY,
                              chat_id BIGINT,
                              user_id BIGINT,
                              user_name TEXT,
                              wins INTEGER DEFAULT 0,
                              losses INTEGER DEFAULT 0,
                              UNIQUE(chat_id, user_id))''')
        conn.commit()
        print("БД инициализирована")
    finally:
        release_conn(conn)

def save_message(chat_id, user_name, text):
    text = text[:MAX_TEXT_LENGTH] if len(text) > MAX_TEXT_LENGTH else text
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                'INSERT INTO history (chat_id, user_name, message_text) VALUES (%s, %s, %s)',
                (chat_id, user_name, text)
            )
        conn.commit()
    except Exception as e:
        print(f"Ошибка сохранения сообщения в БД: {e}")
        conn.rollback()
    finally:
        release_conn(conn)

def cleanup_old_messages():
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM history WHERE timestamp < NOW() - INTERVAL '7 days'"
            )
            deleted = cursor.rowcount
        conn.commit()
        print(f"Очистка БД: удалено {deleted} старых сообщений")
    except Exception as e:
        print(f"Ошибка очистки БД: {e}")
        conn.rollback()
    finally:
        release_conn(conn)

def update_peepee_score(chat_id: int, user_id: int, user_name: str, won: bool):
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                INSERT INTO peepee_scores (chat_id, user_id, user_name, wins, losses)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    user_name = EXCLUDED.user_name,
                    wins = peepee_scores.wins + %s,
                    losses = peepee_scores.losses + %s
            ''', (
                chat_id, user_id, user_name,
                1 if won else 0,
                0 if won else 1,
                1 if won else 0,
                0 if won else 1,
            ))
        conn.commit()
    except Exception as e:
        print(f"Ошибка обновления счёта: {e}")
        conn.rollback()
    finally:
        release_conn(conn)

def get_peepee_scores(chat_id: int) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT user_name, wins, losses
                FROM peepee_scores
                WHERE chat_id = %s AND (wins + losses) > 0
                ORDER BY wins DESC, losses ASC
            ''', (chat_id,))
            return cursor.fetchall()
    finally:
        release_conn(conn)

# 3. УМНАЯ ОБРЕЗКА СООБЩЕНИЙ
def build_prompt_text(rows: list) -> str:
    if not rows:
        return ""

    total = len(rows)

    if total <= 100:
        selected = rows
    else:
        start_count = int(total * 0.40)
        mid_count = int(total * 0.20)
        end_count = int(total * 0.40)

        start = rows[:start_count]
        mid_start = total // 2 - mid_count // 2
        mid = rows[mid_start:mid_start + mid_count]
        end = rows[total - end_count:]

        seen_ids = set()
        selected = []
        for r in start + mid + end:
            key = (r[2], r[0])
            if key not in seen_ids:
                seen_ids.add(key)
                selected.append(r)
        selected.sort(key=lambda x: x[2])

    result = ""
    prev_time = None

    for r in selected:
        time_str = (r[2] + timedelta(hours=TIMEZONE_OFFSET)).strftime('%H:%M')

        if prev_time and (r[2] - prev_time).seconds > 1800:
            result += f"\n--- пауза ---\n"

        line = f"[{time_str}] {r[0]}: {r[1]}\n"

        if len(result) + len(line) > MAX_PROMPT_CHARS:
            result += f"[... ещё {total - len(selected)} сообщений не вошло ...]\n"
            break

        result += line
        prev_time = r[2]

    return result

# 4. ТРАНСКРИБАЦИЯ ГОЛОСА
async def transcribe_audio(file_id: str, filename: str, file_size: int) -> str | None:
    size_mb = file_size / (1024 * 1024)
    if size_mb > MAX_VOICE_SIZE_MB:
        print(f"Файл слишком большой: {size_mb:.1f} МБ — пропускаем")
        return f"[файл слишком большой для распознавания: {size_mb:.1f} МБ]"
    try:
        buffer = io.BytesIO()
        await bot.download(file_id, destination=buffer)
        buffer.seek(0)
        buffer.name = filename
        transcription = client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=buffer,
        )
        return transcription.text
    except Exception as e:
        print(f"Ошибка транскрибации: {e}")
        return None

# 5. ИНТЕЛЛЕКТ
def get_ai_summary(messages_text: str, timeframe_text: str, message_count: int):
    if message_count < 10:
        volume_instruction = "Сообщений мало — будь краток, не раздувай из мухи слона."
    elif message_count < 50:
        volume_instruction = "Средняя активность — стандартный разбор."
    else:
        volume_instruction = "Чат бурлил — можешь развернуться, но без воды."

    prompt = f"""
Ты — Батя этого чата. Не модератор, не ведущий, не журналист. Батя.
Ты знаешь всех в лицо, помнишь кто что говорил месяц назад и не даёшь никому забыть об этом.
Любишь всех, но спуску не даёшь никому. Ни новичкам, ни старожилам.

ВВОДНЫЕ:
- Период: {timeframe_text}
- Сообщений: {message_count}
- {volume_instruction}

КАК ГОВОРИШЬ:
- Матом — естественно, как в разговоре с друзьями. Не для красоты, а потому что так говоришь.
- Каждого называешь по имени и припоминаешь что он за человек.
- Если кто-то нёс хуйню — говоришь прямо: нёс хуйню.
- Если кто-то был красавчик — говоришь: красавчик, но тут же поддеваешь.
- Короткие удары. Никакой воды. Никаких "следует отметить" и "таким образом".
- Пишешь живо, резко, с характером. Как будто рассказываешь другу за пивом.

ЧТО ДОЛЖНО БЫТЬ В ТЕКСТЕ:
- Какая атмосфера была в чате — весело, уныло, срач, философия?
- Кто отличился и как именно — конкретно, с деталями
- Что обсуждали и чем кончилось (или не кончилось)
- Кто молчал весь день и вдруг вылез — отдельно отметить
- Незакрытые вопросы и споры
- Вердикт одной фразой — жёстко и точно

ЗАПРЕЩЕНО КАТЕГОРИЧЕСКИ:
- Мягкие формулировки типа "участники обсудили" или "было высказано мнение"
- Одинаковая структура каждый раз — удивляй
- Шаблонные заголовки — придумывай свои каждый раз
- Вата, политкорректность, осторожные формулировки
- Хвалить без подъёба

Голосовые [🎤 Голосовое] и кружочки [📹 Кружочек] — полноценные сообщения, учитывай.

ВОТ ЧТО БЫЛО В ЧАТЕ:
{messages_text}
"""
    for model in ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen/qwen3-32b"]:
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.85,
                max_tokens=1500,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if "rate_limit_exceeded" in str(e):
                print(f"Модель {model} исчерпала лимит, переключаюсь...")
                continue
            raise
    raise Exception("Все модели исчерпали лимит. Попробуй позже.")

# 6. ФЕЙС-КОНТРОЛЬ
def is_chat_allowed(chat_id):
    return chat_id == ALLOWED_CHAT_ID

# 7. РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ
async def send_long_message(target, text: str):
    if len(text) <= MAX_MESSAGE_LENGTH:
        await target.edit_text(text, parse_mode="HTML")
        return
    parts = []
    while len(text) > MAX_MESSAGE_LENGTH:
        split_at = text.rfind('\n', 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH
        parts.append(text[:split_at])
        text = text[split_at:].lstrip()
    parts.append(text)
    await target.edit_text(parts[0], parse_mode="HTML")
    for part in parts[1:]:
        await target.answer(part, parse_mode="HTML")

# 8. КОМАНДЫ
@dp.message(Command("id"))
async def cmd_id(message: types.Message):
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>", parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🤖 <b>Команды бота:</b>\n\n"
        "/summary — выжимка за последний час\n"
        "/summary 3 — выжимка за 3 часа\n"
        "/summary 24 — выжимка за сутки\n\n"
        "/id — узнать ID этого чата\n"
        "/help — это сообщение\n\n"
        "⚠️ Максимум: 48 часов за один запрос\n"
        "🎤 Голосовые и кружочки тоже учитываются\n\n"
        "🎮 /game — найди писюн\n"
        "🍆 /peepee — рейтинг охотников\n"
        "📊 /mypeepee — твоя статистика"
    )
    await message.answer(help_text, parse_mode="HTML")

@dp.message(Command("game"))
async def cmd_game(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return

    winner_pos = random.randint(0, 8)
    keyboard = []
    row = []
    for i in range(9):
        row.append(types.InlineKeyboardButton(
            text="📦",
            callback_data=f"box_{i}_{winner_pos}"
        ))
        if len(row) == 3:
            keyboard.append(row)
            row = []

    markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    await message.answer("🎮 Найди писюн! Открой одну коробку:", reply_markup=markup)

@dp.callback_query(lambda c: c.data.startswith("box_"))
async def process_box(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    chosen = int(parts[1])
    winner = int(parts[2])
    name = callback.from_user.first_name or "Анон"
    user_id = callback.from_user.id

    won = chosen == winner
    update_peepee_score(callback.message.chat.id, user_id, name, won)

    keyboard = []
    row = []
    for i in range(9):
        if i == winner:
            text = "🍆"
        elif i == chosen and chosen != winner:
            text = "💨"
        else:
            text = "📦"
        row.append(types.InlineKeyboardButton(
            text=text,
            callback_data="done"
        ))
        if len(row) == 3:
            keyboard.append(row)
            row = []

    markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard)

    win_taunts = [
        f"🍆 {name} нашёл! Ну и что, теперь что с ним делать будешь?",
        f"🎉 {name} везунчик, нашёл писюн. Маме расскажи.",
        f"🍆 {name} нашёл писюн. Видимо не первый раз ищет.",
        f"🎉 Ну надо же, {name} справился. Запиши в резюме.",
    ]

    lose_taunts = [
        f"💨 {name}, ну ты лох. Писюн был в коробке {winner + 1}, а ты куда полез?",
        f"🗑 {name} не нашёл. Руки из жопы, коробка {winner + 1} же была очевидна.",
        f"💨 {name} промазал мимо писюна. Это талант — коробка {winner + 1} прямо смотрела на тебя.",
        f"🤡 {name}, серьёзно? Писюн в коробке {winner + 1} сидел и ждал, а ты мимо.",
    ]

    result = random.choice(win_taunts) if won else random.choice(lose_taunts)
    await callback.message.edit_text(result, reply_markup=markup)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "done")
async def process_done(callback: types.CallbackQuery):
    await callback.answer("Игра уже закончена!", show_alert=False)

@dp.message(Command("peepee"))
async def cmd_peepee(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return

    rows = get_peepee_scores(message.chat.id)

    if not rows:
        await message.answer("Ещё никто не играл. Введи /game и начни позориться!")
        return

    medals = ["🥇", "🥈", "🥉"]
    text = "<b>🍆 Рейтинг охотников за писюном:</b>\n\n"

    for i, (name, wins, losses) in enumerate(rows):
        total = wins + losses
        pct = int((wins / total) * 100) if total > 0 else 0
        if i < 3:
            medal = medals[i]
        elif i == len(rows) - 1 and len(rows) > 3:
            medal = "💩"
        else:
            medal = "▪️"
        text += f"{medal} {name} — {wins} нашёл / {losses} мимо ({pct}%)\n"

    if len(rows) > 1:
        loser = rows[-1]
        text += f"\n🤡 Главный мимострел: <b>{loser[0]}</b>"

    await message.answer(text, parse_mode="HTML")

@dp.message(Command("mypeepee"))
async def cmd_mypeepee(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return

    user_id = message.from_user.id
    name = message.from_user.first_name or "Анон"

    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT wins, losses FROM peepee_scores
                WHERE chat_id = %s AND user_id = %s
            ''', (message.chat.id, user_id))
            row = cursor.fetchone()
    finally:
        release_conn(conn)

    if not row:
        await message.answer(f"{name}, ты ещё не играл. Введи /game!")
        return

    wins, losses = row
    total = wins + losses
    pct = int((wins / total) * 100) if total > 0 else 0

    if pct >= 60:
        verdict = "Настоящий охотник 🏆"
    elif pct >= 40:
        verdict = "Так себе, но бывает 🤷"
    else:
        verdict = "Позор семьи 💩"

    text = (
        f"🍆 <b>Статистика {name}:</b>\n\n"
        f"Нашёл: {wins}\n"
        f"Промазал: {losses}\n"
        f"Точность: {pct}%\n\n"
        f"Вердикт: {verdict}"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("summary"))
async def cmd_summary(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        await message.answer("Ты кто такой? Я тебя не знаю. Проваливай из моего контекста! 🖕")
        return

    args = message.text.split()
    hours = 1

    if len(args) > 1:
        if args[1].isdigit():
            hours = int(args[1])
            if hours < 1:
                hours = 1
            elif hours > 48:
                await message.answer("⚠️ Максимум — 48 часов. Не жадничай.")
                return
        else:
            await message.answer("⚠️ Используй число. Например: /summary 3")
            return

    status_msg = await message.answer(f"⏳ Читаю ваш бред за последние {hours} ч...")

    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT user_name, message_text, timestamp 
                FROM history 
                WHERE chat_id = %s 
                AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
                ORDER BY timestamp ASC
            ''', (message.chat.id, hours))
            all_rows = cursor.fetchall()
    except Exception as e:
        await status_msg.edit_text("Ошибка при чтении базы данных. Попробуй ещё раз.")
        print(f"Ошибка запроса к БД: {e}")
        return
    finally:
        release_conn(conn)

    if not all_rows:
        await status_msg.edit_text("За это время сообщений нет. Либо вы спите, либо я сломался.")
        return

    formatted_chat = build_prompt_text(all_rows)

    try:
        raw_summary = get_ai_summary(formatted_chat, f"{hours} ч.", len(all_rows))
        safe_summary = raw_summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await send_long_message(status_msg, f"<b>🔥 ПРОЖАРКА ЧАТА:</b>\n\n{safe_summary}")
    except Exception as e:
        print(f"Ошибка AI: {e}")
        await status_msg.edit_text("Все модели исчерпали лимит. Попробуй через час.")

# 9. СБОР СООБЩЕНИЙ
@dp.message()
async def collect_messages(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return

    if message.sender_chat:
        author = message.sender_chat.title or message.sender_chat.username or "Канал"
    elif message.from_user:
        if message.from_user.is_bot:
            return
        author = message.from_user.full_name or message.from_user.username or "Аноним"
    else:
        return

    if message.text:
        save_message(message.chat.id, author, message.text)
        print(f"[{author}]: {message.text}")

    elif message.voice:
        text = await transcribe_audio(
            message.voice.file_id,
            "voice.ogg",
            message.voice.file_size or 0
        )
        if text:
            save_message(message.chat.id, author, f"[🎤 Голосовое]: {text}")
            print(f"[{author}] 🎤: {text}")
        else:
            save_message(message.chat.id, author, "[🎤 Голосовое]: не удалось распознать")

    elif message.video_note:
        text = await transcribe_audio(
            message.video_note.file_id,
            "video_note.mp4",
            message.video_note.file_size or 0
        )
        if text:
            save_message(message.chat.id, author, f"[📹 Кружочек]: {text}")
            print(f"[{author}] 📹: {text}")
        else:
            save_message(message.chat.id, author, "[📹 Кружочек]: не удалось распознать")

async def main():
    init_db_pool()
    init_db()
    cleanup_old_messages()
    print("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())