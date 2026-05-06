import asyncio
import os
import io
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

# 3. ТРАНСКРИБАЦИЯ ГОЛОСА
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

# 4. ИНТЕЛЛЕКТ
def get_ai_summary(messages_text, timeframe_text, message_count: int):
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
- Если кто-то нёс хуйею — говоришь прямо: нёс хуйню.
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

# 5. ФЕЙС-КОНТРОЛЬ
def is_chat_allowed(chat_id):
    return chat_id == ALLOWED_CHAT_ID

# 6. РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ
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

# 7. КОМАНДЫ
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
        "🎤 Голосовые и кружочки тоже учитываются"
    )
    await message.answer(help_text, parse_mode="HTML")

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
            # Сначала берём все сообщения за период
            cursor.execute('''
                SELECT user_name, message_text, timestamp 
                FROM history 
                WHERE chat_id = %s 
                AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
                ORDER BY timestamp ASC
            ''', (message.chat.id, hours))
            all_rows = cursor.fetchall()

        # Равномерная выборка — берём не больше 150 сообщений
        MAX_MESSAGES = 200
        if len(all_rows) <= MAX_MESSAGES:
            rows = all_rows
        else:
            # Берём каждое N-е сообщение равномерно по всему периоду
            step = len(all_rows) / MAX_MESSAGES
            rows = [all_rows[int(i * step)] for i in range(MAX_MESSAGES)]
    except Exception as e:
        await status_msg.edit_text("Ошибка при чтении базы данных. Попробуй ещё раз.")
        print(f"Ошибка запроса к БД: {e}")
        return
    finally:
        release_conn(conn)

    if not rows:
        await status_msg.edit_text("За это время сообщений нет. Либо вы спите, либо я сломался.")
        return

    formatted_chat = ""
    for r in rows:
        time_str = (r[2] + timedelta(hours=TIMEZONE_OFFSET)).strftime('%H:%M')
        formatted_chat += f"[{time_str}] {r[0]}: {r[1]}\n"

    try:
        raw_summary = get_ai_summary(formatted_chat, f"{hours} ч.", len(rows))
        safe_summary = raw_summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await send_long_message(status_msg, f"<b>🔥 ПРОЖАРКА ЧАТА:</b>\n\n{safe_summary}")
    except Exception as e:
        print(f"Ошибка AI: {e}")
        await status_msg.edit_text("Все модели исчерпали лимит. Попробуй через час.")

# 8. СБОР СООБЩЕНИЙ
@dp.message()
async def collect_messages(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return

    # Определяем автора — человек, канал или аноним
    if message.sender_chat:
        author = message.sender_chat.title or message.sender_chat.username or "Канал"
    elif message.from_user:
        if message.from_user.is_bot:
            return
        author = message.from_user.full_name or message.from_user.username or "Аноним"
    else:
        return

    # Текст
    if message.text:
        save_message(message.chat.id, author, message.text)
        print(f"[{author}]: {message.text}")

    # Голосовое
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

    # Кружочек
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