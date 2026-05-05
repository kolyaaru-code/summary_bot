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

MAX_VOICE_SIZE_MB = 5  # Лимит размера голосового в МБ
MAX_MESSAGE_LENGTH = 4000  # Лимит длины ответа Telegram
MAX_TEXT_LENGTH = 4000  # Лимит длины сохраняемого сообщения

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = Groq(api_key=GROQ_KEY)

# 2. ПУЛ СОЕДИНЕНИЙ С БД (вместо нового соединения на каждый запрос)
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
    # Обрезаем слишком длинные сообщения
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
    # Проверяем размер файла ДО скачивания
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
def get_ai_summary(messages_text, timeframe_text):
    prompt = f"""
    Ты — легенда этого чата, старый кореш, которому остопиздело слушать этот бред, но он все равно в теме. 
    Твоя задача: сделать максимально живой, дерзкий и стебный пересказ того, что эти грешники обсуждали за {timeframe_text}.
    
    СТИЛЬ:
    - Общайся как реальный человек: используй мат (по делу и для окраса), сленг, сарказм.
    - Никакой вежливости. Можешь называть их "клоунами", "бездельниками" или как посчитаешь нужным в контексте их тупняка.
    - Если в чате обсуждали дичь — прямо скажи, что это дичь.
    - Голосовые сообщения помечены [🎤 Голосовое] — учитывай их наравне с текстом.
    - Кружочки помечены [📹 Кружочек] — тоже учитывай.
    
    СТРУКТУРА ОТЧЕТА (обязательно):
    1. 📅 ВРЕМЯ ХАОСА: (интервал, когда всё это дерьмо происходило).
    2. 🤡 ГЛАВНЫЙ КЛОУН: (кто больше всех засирал эфир или выдал самую эпичную херню).
    3. 📝 ЧО БЫЛО (Хронология): кратко по таймкодам раскидай, кто, когда и как умудрился отличиться.
    4. 🏁 ВЕРДИКТ: краткий итог — зря потраченное время или есть хоть что-то дельное.
    
    Вот логи этого дурдома:
    {messages_text}
    """
    completion = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
    )
    return completion.choices[0].message.content

# 5. ФЕЙС-КОНТРОЛЬ
def is_chat_allowed(chat_id):
    return chat_id == ALLOWED_CHAT_ID

# 6. РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ
async def send_long_message(target, text: str):
    """Разбивает текст на части если он длиннее лимита Telegram"""
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
            # Исправленный безопасный запрос без SQL-инъекции
            cursor.execute('''
                SELECT user_name, message_text, timestamp 
                FROM history 
                WHERE chat_id = %s 
                AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
                ORDER BY timestamp ASC
                LIMIT 300
            ''', (message.chat.id, hours))
            rows = cursor.fetchall()
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
        raw_summary = get_ai_summary(formatted_chat, f"{hours} ч.")
        safe_summary = raw_summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await send_long_message(status_msg, f"<b>🔥 ПРОЖАРКА ЧАТА:</b>\n\n{safe_summary}")
    except Exception as e:
        print(f"Ошибка AI: {e}")
        await status_msg.edit_text("Нейросеть подавилась вашим общением.")

# 8. СБОР СООБЩЕНИЙ
@dp.message()
async def collect_messages(message: types.Message):
    if message.from_user.is_bot:
        return
    if not is_chat_allowed(message.chat.id):
        return

    author = message.from_user.full_name or message.from_user.username or "Аноним"

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
            print(f"[{author}] 🎤: не удалось распознать")

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
            print(f"[{author}] 📹: не удалось распознать")

async def main():
    init_db_pool()
    init_db()
    cleanup_old_messages()
    print("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())