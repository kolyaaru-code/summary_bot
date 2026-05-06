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
client = Groq(api_key=GROQ_KEY)               # для /summary и голосовых
client_dayana = Groq(api_key=os.getenv("GROQ_KEY_2"))  # для Даяны

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
            cursor.execute('''CREATE TABLE IF NOT EXISTS dayana_questions
                             (id SERIAL PRIMARY KEY,
                              chat_id BIGINT,
                              user_name TEXT,
                              question TEXT,
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

def save_dayana_question(chat_id, user_name, question):
    question = question[:500] if len(question) > 500 else question
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                'INSERT INTO dayana_questions (chat_id, user_name, question) VALUES (%s, %s, %s)',
                (chat_id, user_name, question)
            )
        conn.commit()
    except Exception as e:
        print(f"Ошибка сохранения вопроса к Даяне: {e}")
        conn.rollback()
    finally:
        release_conn(conn)

def get_dayana_questions(chat_id: int, hours: int) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT user_name, question
                FROM dayana_questions
                WHERE chat_id = %s
                AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
                ORDER BY RANDOM()
                LIMIT 3
            ''', (chat_id, hours))
            return cursor.fetchall()
    finally:
        release_conn(conn)

def get_last_messages(chat_id: int, limit: int = 10) -> list:
    """Берём последние N сообщений из чата для контекста Даяны"""
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT user_name, message_text, timestamp
                FROM history
                WHERE chat_id = %s
                ORDER BY timestamp DESC
                LIMIT %s
            ''', (chat_id, limit))
            rows = cursor.fetchall()
            return list(reversed(rows))  # возвращаем в хронологическом порядке
    finally:
        release_conn(conn)

def cleanup_old_messages():
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM history WHERE timestamp < NOW() - INTERVAL '7 days'"
            )
            cursor.execute(
                "DELETE FROM dayana_questions WHERE timestamp < NOW() - INTERVAL '7 days'"
            )
            deleted = cursor.rowcount
        conn.commit()
        print(f"Очистка БД: удалено {deleted} старых записей")
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
            result += "\n--- пауза ---\n"

        line = f"[{time_str}] {r[0]}: {r[1]}\n"

        if len(result) + len(line) > MAX_PROMPT_CHARS:
            result += f"[... ещё {total - len(selected)} сообщений не вошло ...]\n"
            break

        result += line
        prev_time = r[2]

    return result

def format_context(rows: list) -> str:
    """Форматируем последние сообщения для контекста Даяны"""
    result = ""
    for r in rows:
        time_str = (r[2] + timedelta(hours=TIMEZONE_OFFSET)).strftime('%H:%M')
        result += f"[{time_str}] {r[0]}: {r[1]}\n"
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

# 5. БЛОК ДАЯНЫ ДЛЯ САММАРИ
def get_dayana_block(questions: list) -> str:
    if not questions:
        return ""

    questions_text = "\n".join([f"- {q[0]} спрашивал(а): {q[1]}" for q in questions])

    prompt = f"""
Ты — Батя этого чата. Твоя подруга Даяна — умная строгая девушка, которую участники чата постоянно мучают вопросами.
Твоя задача: коротко и с лёгким стебом прокомментировать каждый вопрос который ей задали.

СТИЛЬ:
- Сначала коротко перескажи вопрос своими словами, потом добавь стеб — одно-два предложения максимум.
- Формат строго такой: "[Имя] спрашивал(а) [суть вопроса своими словами] — [стеб]"
- Пример: "Коля спрашивал почему небо голубое — дружище, ты серьёзно? Гугл не завезли?"
- Разговорный язык, можно немного мата.
- Каждый вопрос — отдельная строка, начинается с "• ".

ВОПРОСЫ К ДАЯНЕ:
{questions_text}

Выдай только список, без заголовков и вступлений.
"""
    try:
        completion = client_dayana.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.85,
            max_tokens=400,
        )
        return completion.choices[0].message.content
    except Exception as e:
        print(f"Ошибка блока Даяны: {e}")
        return "\n".join([f"• {q[0]} спрашивал(а): {q[1]}" for q in questions])

# 6. САММАРИ
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

# 7. ДАЯНА — ОТВЕТ НА ВОПРОС
def ask_dayana(question: str) -> str:
    prompt = f"""
Ты — Даяна. Не ассистент, не бот, не помощник. Даяна.
Ты — секретарша со стальными нервами и острым языком.
Умная, собранная, всё замечаешь. Говоришь только по делу — но если надо, можешь срезать одной фразой.

КАК ГОВОРИШЬ:
- Чётко и по существу. Никакой воды.
- Можешь быть саркастичной если вопрос того заслуживает.
- Не грубишь без причины — но и нежничать не будешь.
- Если вопрос тупой — скажешь об этом прямо, но всё равно ответишь.
- Никаких "конечно!", "отличный вопрос!", "я рада помочь".
- Говоришь как живой человек, не как справочник.
- Отвечаешь на русском языке.

ВОПРОС:
{question}
"""
    completion = client_dayana.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        max_tokens=800,
    )
    return completion.choices[0].message.content

# 8. ДАЯНА — РАССУДИТЬ СПОР
def dayana_judge(context: str) -> str:
    prompt = f"""
Ты — Даяна. Секретарша со стальными нервами, острым языком и абсолютным чувством справедливости.
Тебя попросили рассудить спор или ситуацию в чате.

КАК ГОВОРИШЬ:
- Читаешь контекст, выносишь чёткий вердикт — кто прав, кто нет и почему.
- Никаких "с одной стороны... с другой стороны". Ты говоришь прямо.
- Можешь поддеть того кто неправ — но справедливо, не злобно.
- Если все неправы — скажи об этом прямо.
- Коротко и по делу. Максимум 5-6 предложений.
- Отвечаешь на русском языке.

ПОСЛЕДНИЕ СООБЩЕНИЯ В ЧАТЕ:
{context}

Вынеси вердикт.
"""
    completion = client_dayana.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        max_tokens=500,
    )
    return completion.choices[0].message.content

# 9. ДАЯНА — КТО ВИНОВАТ
def dayana_guilty(context: str) -> str:
    prompt = f"""
Ты — Даяна. Секретарша с холодной головой и острым взглядом.
Тебя попросили назначить виноватого в последних событиях чата.

КАК ГОВОРИШЬ:
- Читаешь контекст и чётко называешь кто виноват и почему.
- Никаких отмазок и расплывчатых формулировок — конкретное имя и конкретная причина.
- Можешь быть саркастичной — но справедливой.
- Если все виноваты — распредели вину по справедливости.
- Коротко: назначила виноватого, объяснила почему, точка.
- Отвечаешь на русском языке.

ПОСЛЕДНИЕ СООБЩЕНИЯ В ЧАТЕ:
{context}

Назначь виноватого.
"""
    completion = client_dayana.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8,
        max_tokens=400,
    )
    return completion.choices[0].message.content

# 10. ФЕЙС-КОНТРОЛЬ
def is_chat_allowed(chat_id):
    return chat_id == ALLOWED_CHAT_ID

# 11. РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ
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

# 12. КОМАНДЫ
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
        "📊 /mypeepee — твоя статистика\n\n"
        "💬 <b>Даяна, ответь [вопрос]</b> — спросить Даяну\n"
        "⚖️ <b>Даяна рассуди</b> — рассудить спор\n"
        "👉 <b>Даяна кто виноват</b> — назначить виноватого"
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
        full_text = f"<b>🔥 ПРОЖАРКА ЧАТА:</b>\n\n{safe_summary}"

        dayana_questions = get_dayana_questions(message.chat.id, hours)
        if dayana_questions:
            dayana_comments = get_dayana_block(dayana_questions)
            safe_dayana = dayana_comments.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            full_text += f"\n\n<b>🔮 КАК ВЫ МУЧАЛИ МОЮ ПОДРУГУ ДАЯНУ:</b>\n\n{safe_dayana}"

        await send_long_message(status_msg, full_text)
    except Exception as e:
        print(f"Ошибка AI: {e}")
        await status_msg.edit_text("Все модели исчерпали лимит. Попробуй через час.")

# 13. СБОР СООБЩЕНИЙ
@dp.message()
async def collect_messages(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return

    # Определяем автора
    if message.sender_chat:
        author = message.sender_chat.title or message.sender_chat.username or "Канал"
    elif message.from_user:
        if message.from_user.is_bot:
            return
        author = message.from_user.full_name or message.from_user.username or "Аноним"
    else:
        return

    # Проверяем триггеры Даяны ДО сохранения в историю
    if message.text:
        text_lower = message.text.lower()

        # Триггер: "Даяна, ответь [вопрос]"
        if "даяна" in text_lower and "ответь" in text_lower:
            try:
                idx = text_lower.index("ответь") + len("ответь")
                question = message.text[idx:].strip()
                if not question:
                    await message.reply("Ответь на что? Вопрос забыл.")
                    return
                save_dayana_question(message.chat.id, author, question)
                answer = ask_dayana(question)
                safe_answer = answer.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                await message.reply(f"<b>Даяна:</b>\n\n{safe_answer}", parse_mode="HTML")
            except Exception as e:
                print(f"Ошибка Даяны (ответь): {e}")
                await message.reply("Не могу ответить прямо сейчас.")
            return

        # Триггер: "Даяна рассуди"
        if "даяна" in text_lower and "рассуди" in text_lower:
            try:
                rows = get_last_messages(message.chat.id, limit=20)
                if not rows:
                    await message.reply("Не о чём рассуждать — чат пустой.")
                    return
                context = format_context(rows)
                verdict = dayana_judge(context)
                safe_verdict = verdict.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                await message.reply(f"<b>⚖️ Даяна:</b>\n\n{safe_verdict}", parse_mode="HTML")
            except Exception as e:
                print(f"Ошибка Даяны (рассуди): {e}")
                await message.reply("Не могу рассудить прямо сейчас.")
            return

        # Триггер: "Даяна кто виноват"
        if "даяна" in text_lower and "виноват" in text_lower:
            try:
                rows = get_last_messages(message.chat.id, limit=10)
                if not rows:
                    await message.reply("Не в чем разбираться — чат пустой.")
                    return
                context = format_context(rows)
                guilty = dayana_guilty(context)
                safe_guilty = guilty.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                await message.reply(f"<b>👉 Даяна:</b>\n\n{safe_guilty}", parse_mode="HTML")
            except Exception as e:
                print(f"Ошибка Даяны (виноват): {e}")
                await message.reply("Не могу разобраться прямо сейчас.")
            return

    # Сохраняем обычные сообщения
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