import asyncio
import os
import io
import psycopg2
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv
from groq import Groq

# 1. НАСТРОЙКИ
load_dotenv()
TOKEN = os.getenv("TG_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID"))
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = Groq(api_key=GROQ_KEY)

# 2. БАЗА ДАННЫХ
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''CREATE TABLE IF NOT EXISTS history 
                             (id SERIAL PRIMARY KEY, 
                              chat_id BIGINT,
                              user_name TEXT, 
                              message_text TEXT, 
                              timestamp TIMESTAMPTZ DEFAULT NOW())''')
        conn.commit()
    print("БД инициализирована")

def save_message(chat_id, user_name, text):
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                'INSERT INTO history (chat_id, user_name, message_text) VALUES (%s, %s, %s)',
                (chat_id, user_name, text)
            )
        conn.commit()

def cleanup_old_messages():
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM history WHERE timestamp < NOW() - INTERVAL '7 days'"
            )
            deleted = cursor.rowcount
        conn.commit()
    print(f"Очистка БД: удалено {deleted} старых сообщений")

# 3. ТРАНСКРИБАЦИЯ ГОЛОСА (новое!)
async def transcribe_audio(file_id: str, filename: str) -> str | None:
    try:
        # Скачиваем файл из Telegram в память
        buffer = io.BytesIO()
        await bot.download(file_id, destination=buffer)
        buffer.seek(0)
        buffer.name = filename  # Groq требует имя файла с расширением

        # Отправляем в Groq Whisper
        transcription = client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=buffer,
            language="ru"  # Подсказываем язык — быстрее и точнее
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

# 6. КОМАНДЫ
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

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT user_name, message_text, timestamp 
                FROM history 
                WHERE chat_id = %s AND timestamp >= NOW() - INTERVAL '%s hours'
                ORDER BY timestamp ASC
                LIMIT 300
            ''', (message.chat.id, hours))
            rows = cursor.fetchall()

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
        await status_msg.edit_text(f"<b>🔥 ПРОЖАРКА ЧАТА:</b>\n\n{safe_summary}", parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text("Нейросеть подавилась вашим общением.")

# 7. СБОР СООБЩЕНИЙ — ТЕКСТ
@dp.message()
async def collect_messages(message: types.Message):
    if message.from_user.is_bot:
        return
    if not is_chat_allowed(message.chat.id):
        return

    author = message.from_user.full_name or message.from_user.username or "Аноним"

    # Текстовое сообщение
    if message.text:
        save_message(message.chat.id, author, message.text)
        print(f"[{author}]: {message.text}")

    # Голосовое сообщение 🎤
    elif message.voice:
        text = await transcribe_audio(message.voice.file_id, "voice.ogg")
        if text:
            save_message(message.chat.id, author, f"[🎤 Голосовое]: {text}")
            print(f"[{author}] 🎤: {text}")
        else:
            print(f"[{author}] 🎤: не удалось распознать")

    # Кружочек 📹
    elif message.video_note:
        text = await transcribe_audio(message.video_note.file_id, "video_note.mp4")
        if text:
            save_message(message.chat.id, author, f"[📹 Кружочек]: {text}")
            print(f"[{author}] 📹: {text}")
        else:
            print(f"[{author}] 📹: не удалось распознать")

async def main():
    init_db()
    cleanup_old_messages()
    print("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())