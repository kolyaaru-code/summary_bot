import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv
from groq import Groq

# 1. НАСТРОЙКИ
load_dotenv()
TOKEN = os.getenv("TG_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID"))        # ← теперь из .env
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "0"))   # ← теперь из .env

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = Groq(api_key=GROQ_KEY)

# 2. БАЗА ДАННЫХ
def init_db():
    conn = sqlite3.connect("messages.db")
    try:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS history 
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                          chat_id INTEGER,
                          user_name TEXT, 
                          message_text TEXT, 
                          timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        conn.commit()
    finally:
        conn.close()  # ← закроется даже если произошла ошибка

def save_message(chat_id, user_name, text):
    conn = sqlite3.connect("messages.db")
    try:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO history (chat_id, user_name, message_text) VALUES (?, ?, ?)',
            (chat_id, user_name, text)
        )
        conn.commit()
    finally:
        conn.close()

def cleanup_old_messages():
    """Удаляет сообщения старше 7 дней — запускается при каждом старте бота"""
    conn = sqlite3.connect("messages.db")
    try:
        cursor = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('DELETE FROM history WHERE timestamp < ?', (cutoff,))
        deleted = cursor.rowcount
        conn.commit()
        print(f"Очистка БД: удалено {deleted} старых сообщений")
    finally:
        conn.close()

# 3. ИНТЕЛЛЕКТ
def get_ai_summary(messages_text, timeframe_text):
    prompt = f"""
    Ты — легенда этого чата, старый кореш, которому остопиздело слушать этот бред, но он все равно в теме. 
    Твоя задача: сделать максимально живой, дерзкий и стебный пересказ того, что эти грешники обсуждали за {timeframe_text}.
    
    СТИЛЬ:
    - Общайся как реальный человек: используй мат (по делу и для окраса), сленг, сарказм.
    - Никакой вежливости. Можешь называть их "клоунами", "бездельниками" или как посчитаешь нужным в контексте их тупняка.
    - Если в чате обсуждали дичь — прямо скажи, что это дичь.
    
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

# 4. ФЕЙС-КОНТРОЛЬ
def is_chat_allowed(chat_id):
    return chat_id == ALLOWED_CHAT_ID

# 5. КОМАНДЫ
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
        "⚠️ Максимум: 48 часов за один запрос"
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
            elif hours > 48:  # ← защита от /summary 9999
                await message.answer("⚠️ Максимум — 48 часов. Не жадничай.")
                return
        else:
            await message.answer("⚠️ Используй число. Например: /summary 3")
            return

    status_msg = await message.answer(f"⏳ Читаю ваш бред за последние {hours} ч...")

    now_utc = datetime.now(timezone.utc)
    time_limit_str = (now_utc - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')

    conn = sqlite3.connect("messages.db")
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_name, message_text, timestamp 
            FROM history 
            WHERE chat_id = ? AND timestamp >= ? 
            ORDER BY timestamp ASC
            LIMIT 300
        ''', (message.chat.id, time_limit_str))
        rows = cursor.fetchall()
    finally:
        conn.close()

    if not rows:
        await status_msg.edit_text("За это время сообщений нет. Либо вы спите, либо я сломался.")
        return

    formatted_chat = ""
    for r in rows:
        utc_dt = datetime.strptime(r[2], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        time_str = (utc_dt + timedelta(hours=TIMEZONE_OFFSET)).strftime('%H:%M')
        formatted_chat += f"[{time_str}] {r[0]}: {r[1]}\n"

    try:
        raw_summary = get_ai_summary(formatted_chat, f"{hours} ч.")
        safe_summary = raw_summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await status_msg.edit_text(f"<b>🔥 ПРОЖАРКА ЧАТА:</b>\n\n{safe_summary}", parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text("Нейросеть подавилась вашим общением.")

# 6. СБОР СООБЩЕНИЙ
@dp.message()
async def collect_messages(message: types.Message):
    if message.from_user.is_bot:  # ← бот больше не слушает сам себя
        return

    if is_chat_allowed(message.chat.id) and message.text:
        author = message.from_user.full_name or message.from_user.username or "Аноним"
        save_message(message.chat.id, author, message.text)
        print(f"[{author}]: {message.text}")

async def main():
    init_db()
    cleanup_old_messages()  # ← очистка старых сообщений при каждом старте
    print("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())