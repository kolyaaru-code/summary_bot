import asyncio
import os
import io
import random
import time
import json
import base64
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv
from groq import Groq
from psycopg2 import pool
from aiohttp import web
import hmac
import hashlib

# 1. НАСТРОЙКИ
load_dotenv()
TOKEN = os.getenv("TG_TOKEN")
GROQ_KEY = os.getenv("GROQ_KEY")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID"))
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
RAILWAY_URL = os.getenv("RAILWAY_URL", "")
GAME_URL = "https://kolyaaru-code.github.io/summary_bot/"
CASINO_URL = "https://kolyaaru-code.github.io/summary_bot/casino.html"
TOKEN_TTL = 300
CASINO_START_BALANCE = 1000

MAX_VOICE_SIZE_MB = 5
MAX_MESSAGE_LENGTH = 4000
MAX_TEXT_LENGTH = 4000
MAX_PROMPT_CHARS = 9000

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = Groq(api_key=GROQ_KEY)
client_dayana = Groq(api_key=os.getenv("GROQ_KEY_2"))

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
            cursor.execute('''CREATE TABLE IF NOT EXISTS casino_balances
                             (id SERIAL PRIMARY KEY,
                              chat_id BIGINT,
                              user_id BIGINT,
                              user_name TEXT,
                              balance INTEGER DEFAULT 1000,
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
            return list(reversed(rows))
    finally:
        release_conn(conn)

def get_messages_around_timestamp(chat_id: int, anchor_ts, before: int = 15, after: int = 8) -> list:
    """Берём сообщения вокруг конкретного timestamp — якоря из reply"""
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                (SELECT user_name, message_text, timestamp
                 FROM history
                 WHERE chat_id = %s AND timestamp <= %s
                 ORDER BY timestamp DESC
                 LIMIT %s)
                UNION ALL
                (SELECT user_name, message_text, timestamp
                 FROM history
                 WHERE chat_id = %s AND timestamp > %s
                 ORDER BY timestamp ASC
                 LIMIT %s)
                ORDER BY timestamp ASC
            ''', (chat_id, anchor_ts, before, chat_id, anchor_ts, after))
            return cursor.fetchall()
    finally:
        release_conn(conn)

def cleanup_old_messages():
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM history WHERE timestamp < NOW() - INTERVAL '7 days'")
            cursor.execute("DELETE FROM dayana_questions WHERE timestamp < NOW() - INTERVAL '7 days'")
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
                1 if won else 0, 0 if won else 1,
                1 if won else 0, 0 if won else 1,
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

# 3. КАЗИНО — БД
def get_or_create_casino_balance(chat_id: int, user_id: int, user_name: str) -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                INSERT INTO casino_balances (chat_id, user_id, user_name, balance)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    user_name = EXCLUDED.user_name
                RETURNING balance
            ''', (chat_id, user_id, user_name, CASINO_START_BALANCE))
            conn.commit()
            return cursor.fetchone()[0]
    except Exception as e:
        print(f"Ошибка get_or_create_casino_balance: {e}")
        conn.rollback()
        return CASINO_START_BALANCE
    finally:
        release_conn(conn)

def update_casino_balance(chat_id: int, user_id: int, delta: int) -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                UPDATE casino_balances
                SET balance = balance + %s
                WHERE chat_id = %s AND user_id = %s
                RETURNING balance
            ''', (delta, chat_id, user_id))
            conn.commit()
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        print(f"Ошибка update_casino_balance: {e}")
        conn.rollback()
        return 0
    finally:
        release_conn(conn)

def get_casino_leaderboard(chat_id: int) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT user_name, balance
                FROM casino_balances
                WHERE chat_id = %s
                ORDER BY balance DESC
            ''', (chat_id,))
            return cursor.fetchall()
    finally:
        release_conn(conn)

# 4. КАЗИНО — ЛОГИКА СЛОТОВ
SLOT_SYMBOLS = ['🍒', '🍋', '7️⃣', '💎', '🍆']

def spin_slots(bet: int) -> dict:
    """
    Вероятности — казино жёстче:
      75%  — все разные (проигрыш, x0)
      15%  — два одинаковых (x1.2)
       6%  — 🍒🍒🍒 (x2.5)
      2.5% — 🍋🍋🍋 (x4)
       1%  — 7️⃣7️⃣7️⃣ (x12)
      0.4% — 💎💎💎 (x8)
      0.1% — 🍆🍆🍆 (x20, джекпот)
    Матожидание ~0.65$ на каждый вложенный доллар.
    """
    r = random.random()

    if r < 0.75:
        symbols = random.sample(SLOT_SYMBOLS, 3)
        multiplier = 0
        result_type = "lose"
    elif r < 0.90:
        sym = random.choice(SLOT_SYMBOLS)
        others = [s for s in SLOT_SYMBOLS if s != sym]
        third = random.choice(others)
        positions = [0, 1, 2]
        match_pos = random.sample(positions, 2)
        symbols = [third, third, third]
        symbols[match_pos[0]] = sym
        symbols[match_pos[1]] = sym
        remaining = [p for p in positions if p not in match_pos][0]
        symbols[remaining] = third
        multiplier = 1.2
        result_type = "pair"
    elif r < 0.96:
        symbols = ['🍒', '🍒', '🍒']
        multiplier = 2.5
        result_type = "win"
    elif r < 0.985:
        symbols = ['🍋', '🍋', '🍋']
        multiplier = 4
        result_type = "win"
    elif r < 0.995:
        symbols = ['7️⃣', '7️⃣', '7️⃣']
        multiplier = 12
        result_type = "bigwin"
    elif r < 0.999:
        symbols = ['💎', '💎', '💎']
        multiplier = 8
        result_type = "bigwin"
    else:
        symbols = ['🍆', '🍆', '🍆']
        multiplier = 20
        result_type = "jackpot"

    winnings = int(bet * multiplier)
    delta = winnings - bet
    return {
        "symbols": symbols,
        "multiplier": multiplier,
        "winnings": winnings,
        "delta": delta,
        "result_type": result_type
    }

def get_casino_comment(name: str, result_type: str, delta: int, new_balance: int) -> str:
    if result_type == "jackpot":
        return random.choice([
            f"ДЖЕКПОТ, СУКА!!! {name}, ты выиграл всё что можно было выиграть в этой помойке! Звони маме, пиши в резюме. Но мы оба знаем — ты это всё просрёшь обратно. Крути дальше.",
            f"🍆🍆🍆 ТРИ ПИСЮНА! {name}, это знак судьбы. Либо немедленно уходи победителем, либо оставайся и проиграй всё. Мы угадали что ты выберешь.",
            f"СТОП. {name} сорвал джекпот. Казино официально в панике. Наслаждайся моментом — он не повторится никогда в жизни.",
        ])
    if result_type == "bigwin":
        return random.choice([
            f"ЕБАТЬ ТЫ ФОРТОВЫЙ, {name}! Скорее депай ещё пока колесо фортуны не заметило свою ошибку. Такое везение случается раз в жизни — и ты уже потратил свой шанс.",
            f"Ничего себе, {name}! Большой куш! Самое время остановиться... но ты же не остановишься, да? Мы знаем тебя.",
            f"{name} поднял серьёзные бабки. Казино смотрит на тебя с уважением и ненавистью одновременно. Крути ещё — нам нужно вернуть своё.",
        ])
    if result_type == "win":
        return random.choice([
            f"О, {name} выиграл! Скорее депай ещё пока везёт, идиот. Удача — она как кошка: погладил раз, укусит два.",
            f"Смотри-ка, {name} в плюсе! Это называется 'начало конца'. Казино специально так делает — сначала даёт выиграть, потом забирает всё.",
            f"{name}, поздравляю с маленькой победой в большой войне с казино. Спойлер: казино выиграет войну.",
            f"Повезло {name}! Теперь ставь больше — раз пошла такая пьянка. Логика железная, да?",
        ])
    if result_type == "pair":
        return random.choice([
            f"Пара у {name}. Х1.2, красавчик. Это не выигрыш, это подачка. Казино кормит тебя с ладони как голубя.",
            f"{name}, пара — это казино говорит тебе 'иди сюда, хороший'. Не ведись. Хотя ты уже ведёшься.",
            f"Маленький плюсик для {name}. Аппарат прогревается. Следующий спин будет либо джекпот либо дно — угадай что вероятнее.",
        ])
    if new_balance >= 1000:
        return random.choice([
            f"Мимо, {name}. Бывает. Ты ещё в плюсе — есть что терять. Это самое опасное состояние для лудомана.",
            f"{name} слил ставку. Деньги ещё есть, значит казино своё ещё получит. Крути дальше.",
            f"Ай, {name}, не повезло. Зато ты богатый пока. Ключевое слово — пока.",
        ])
    elif new_balance >= 500:
        return random.choice([
            f"Хм, {name}... Денежки тают. Чуешь этот запах? Это твои сбережения горят. Красиво горят, надо признать.",
            f"{name}, осторожно — пахнет лудкой. Ты ещё не закрыл вкладку? Конечно нет. Понятно.",
            f"Баланс падает, {name}. Это нормально, говоришь себе. Всего одна удачная ставка и отыграюсь. Классика жанра.",
        ])
    elif new_balance >= 0:
        return random.choice([
            f"Почка ещё на месте, {name}? Проверь — скоро пригодится. Ты почти на нуле, дружище.",
            f"{name}, ты в опасной близости от дна. Большинство людей на этом месте остановились бы. Но ты же не большинство.",
            f"Осталось совсем чуть-чуть, {name}. Либо сейчас повезёт и отыграешься, либо... ну ты понимаешь. Крути.",
        ])
    elif new_balance >= -1000:
        return random.choice([
            f"ПОЗДРАВЛЯЮ, {name}! Ты официально в минусе! Почку уже оценил? На рынке сейчас неплохие цены.",
            f"{name} ушёл в минус. Это не конец — это начало настоящей лудки. Добро пожаловать в клуб.",
            f"Минус на балансе у {name}. Машину продавать ещё рано, но держи документы наготове.",
            f"О, {name} в красной зоне! Звони другу, занимай бабки — казино ждёт. Долг — не проблема, проблема — не отыграться.",
        ])
    elif new_balance >= -5000:
        return random.choice([
            f"{name}, ты уже серьёзно в яме. Хату заложил? Машину продал? Нет? Значит ещё есть что терять. Крути.",
            f"Глубокий минус у {name}. На этом уровне нормальные люди звонят на горячую линию психологической помощи. Ты не нормальный — ты наш человек.",
            f"СТОП, {name}. Нет, серьёзно, стоп. Подумай. Подумал? Хорошо. А теперь крути — думать вредно для азарта.",
            f"{name} зарылся как крот. На этой глубине уже темно и страшно. Но джекпот где-то здесь, правда же? Правда?",
        ])
    else:
        return random.choice([
            f"ЛЕГЕНДА. {name} установил антирекорд казино. Это надо уметь. Напиши завещание и крути дальше — нам нужен новый рекорд.",
            f"{name}, ты на такой глубине что уже видно магму. Казино тебя уважает. Казино тебя боготворит. Казино на тебе построило новое крыло.",
            f"На этом уровне долга, {name}, уже не стыдно. Это искусство. Это дно такой красоты что хочется плакать и аплодировать одновременно.",
            f"Друг, {name}... мы уже даже стебаться не можем. Это за гранью. Это легенда. Твоё имя впишут в историю этого казино золотыми буквами.",
        ])

# 5. УМНАЯ ОБРЕЗКА СООБЩЕНИЙ
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
    result = ""
    for r in rows:
        time_str = (r[2] + timedelta(hours=TIMEZONE_OFFSET)).strftime('%H:%M')
        result += f"[{time_str}] {r[0]}: {r[1]}\n"
    return result

# 6. ТРАНСКРИБАЦИЯ ГОЛОСА
async def transcribe_audio(file_id: str, filename: str, file_size: int) -> str | None:
    size_mb = file_size / (1024 * 1024)
    if size_mb > MAX_VOICE_SIZE_MB:
        return f"[файл слишком большой для распознавания: {size_mb:.1f} МБ]"
    try:
        buffer = io.BytesIO()
        await bot.download(file_id, destination=buffer)
        buffer.seek(0)
        buffer.name = filename
        transcription = client.audio.transcriptions.create(
            model="whisper-large-v3-turbo", file=buffer,
        )
        return transcription.text
    except Exception as e:
        print(f"Ошибка транскрибации: {e}")
        return None

# 7. БЛОК ДАЯНЫ ДЛЯ САММАРИ
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
            temperature=0.85, max_tokens=400,
        )
        return completion.choices[0].message.content
    except Exception as e:
        print(f"Ошибка блока Даяны: {e}")
        return "\n".join([f"• {q[0]} спрашивал(а): {q[1]}" for q in questions])

# 8. САММАРИ
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
                temperature=0.85, max_tokens=1500,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if "rate_limit_exceeded" in str(e):
                print(f"Модель {model} исчерпала лимит, переключаюсь...")
                continue
            raise
    raise Exception("Все модели исчерпали лимит. Попробуй позже.")

# 9. ДАЯНА — ОТВЕТ НА ВОПРОС
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
        temperature=0.8, max_tokens=800,
    )
    return completion.choices[0].message.content

# 10. ДАЯНА — РАССУДИТЬ СПОР (умный промпт + qwen3)
def dayana_judge(context: str, hint: str = None) -> str:
    hint_block = f"\nЧТО ИМЕННО НУЖНО РАССУДИТЬ (подсказка от участника):\n{hint}\n" if hint else ""
    prompt = f"""
Ты — Даяна. Секретарша со стальными нервами, острым языком и абсолютным чувством справедливости.
Тебя попросили рассудить спор в чате.

ШАГ 1 — РАЗБЕРИСЬ САМА (не пиши это вслух, просто подумай):
- Кто участвует в споре? Назови имена.
- В чём именно суть разногласия — одним предложением.
- Кто первым начал и кто эскалировал?
- Есть ли в переписке посторонние сообщения не по теме спора? Игнорируй их.
- У кого из сторон более весомые аргументы?

ШАГ 2 — ВЫНЕСИ ВЕРДИКТ (это и пиши):
- Начни с короткого атмосферного действия в asterisk. Каждый раз разное.
- Формат: *[действие]* — и дальше сразу текст.
- Пример: *закуривает сигарету Kiss, смотрит в окно* — Так, разберёмся...
- Назови стороны по имени — не "один участник" а конкретно кто.
- Чёткий вердикт: кто прав, кто нет, почему. Никаких "с одной стороны".
- Можешь поддеть того кто неправ — но справедливо.
- Если все неправы — скажи прямо и объясни почему оба идиоты.
- Максимум 5-6 предложений. Коротко и жёстко.
- Отвечаешь на русском языке.
{hint_block}
ПЕРЕПИСКА ИЗ ЧАТА:
{context}

Вынеси вердикт.
"""
    for model in ["qwen/qwen3-32b", "llama-3.3-70b-versatile"]:
        try:
            completion = client_dayana.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=600,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if "rate_limit" in str(e).lower() or "model" in str(e).lower():
                print(f"Даяна рассуди: модель {model} недоступна, переключаюсь...")
                continue
            raise
    raise Exception("Все модели недоступны")

# 11. ДАЯНА — КТО ВИНОВАТ (умный промпт + qwen3)
def dayana_guilty(context: str, hint: str = None) -> str:
    hint_block = f"\nПОДСКАЗКА О СИТУАЦИИ:\n{hint}\n" if hint else ""
    prompt = f"""
Ты — Даяна. Секретарша с холодной головой, острым взглядом и нулевой терпимостью к отмазкам.
Тебя попросили найти виноватого.

ШАГ 1 — ПРОАНАЛИЗИРУЙ (не пиши, просто подумай):
- Что произошло? Восстанови хронологию.
- Кто что сделал или сказал — конкретно.
- Игнорируй сообщения не по теме — в чате всегда есть посторонний шум.
- Кто объективно облажался или спровоцировал?

ШАГ 2 — НАЗНАЧЬ ВИНОВАТОГО (это и пиши):
- Конкретное имя. Не "некоторые участники" — а кто именно.
- Чёткая причина: что именно он сделал не так.
- Можешь быть саркастичной — но справедливой, не злобной.
- Если вина распределена — назови главного виноватого и объясни градацию.
- Коротко: 3-4 предложения максимум.
- Отвечаешь на русском языке.
{hint_block}
ПЕРЕПИСКА ИЗ ЧАТА:
{context}

Назначь виноватого.
"""
    for model in ["qwen/qwen3-32b", "llama-3.3-70b-versatile"]:
        try:
            completion = client_dayana.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=500,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if "rate_limit" in str(e).lower() or "model" in str(e).lower():
                print(f"Даяна виноват: модель {model} недоступна, переключаюсь...")
                continue
            raise
    raise Exception("Все модели недоступны")

# 12. УТИЛИТЫ
def is_chat_allowed(chat_id):
    return chat_id == ALLOWED_CHAT_ID

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

# 13. ТОКЕНЫ
def generate_game_token(user_id: int, user_name: str) -> str:
    payload = {"user_id": user_id, "user_name": user_name, "ts": int(time.time())}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    sig = hmac.new(TOKEN.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"

def verify_game_token(token: str) -> dict | None:
    try:
        payload_b64, sig = token.rsplit(".", 1)
        expected = hmac.new(TOKEN.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        if int(time.time()) - payload.get("ts", 0) > TOKEN_TTL:
            return None
        return payload
    except Exception:
        return None

# 14. ВЕБ — CORS
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

# 15. ВЕБ — ЭНДПОИНТЫ
async def handle_result(request):
    try:
        data = await request.json()
        payload = verify_game_token(data.get("token", ""))
        if not payload:
            return web.json_response({"error": "Unauthorized"}, status=401)
        won = data.get("won")
        if won is None:
            return web.json_response({"error": "Missing fields"}, status=400)
        update_peepee_score(ALLOWED_CHAT_ID, int(payload["user_id"]), payload["user_name"], bool(won))
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_scores(request):
    try:
        rows = get_peepee_scores(ALLOWED_CHAT_ID)
        scores = [{"name": r[0], "wins": r[1], "losses": r[2]} for r in rows]
        return web.json_response({"scores": scores})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_casino_spin(request):
    try:
        data = await request.json()
        payload = verify_game_token(data.get("token", ""))
        if not payload:
            return web.json_response({"error": "Unauthorized"}, status=401)
        bet = int(data.get("bet", 10))
        if bet not in [10, 50, 100, 500]:
            return web.json_response({"error": "Invalid bet"}, status=400)
        user_id = int(payload["user_id"])
        user_name = payload["user_name"]
        get_or_create_casino_balance(ALLOWED_CHAT_ID, user_id, user_name)
        result = spin_slots(bet)
        new_balance = update_casino_balance(ALLOWED_CHAT_ID, user_id, result["delta"])
        comment = get_casino_comment(user_name, result["result_type"], result["delta"], new_balance)
        return web.json_response({
            "ok": True,
            "symbols": result["symbols"],
            "multiplier": result["multiplier"],
            "winnings": result["winnings"],
            "delta": result["delta"],
            "result_type": result["result_type"],
            "new_balance": new_balance,
            "comment": comment,
        })
    except Exception as e:
        print(f"Ошибка casino_spin: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_casino_leaderboard(request):
    try:
        rows = get_casino_leaderboard(ALLOWED_CHAT_ID)
        board = [{"name": r[0], "balance": r[1]} for r in rows]
        return web.json_response({"board": board})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_casino_balance(request):
    try:
        token = request.rel_url.query.get("token", "")
        payload = verify_game_token(token)
        if not payload:
            return web.json_response({"error": "Unauthorized"}, status=401)
        user_id = int(payload["user_id"])
        user_name = payload["user_name"]
        balance = get_or_create_casino_balance(ALLOWED_CHAT_ID, user_id, user_name)
        return web.json_response({"balance": balance, "user_name": user_name})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# 16. КОМАНДЫ БОТА
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
        "🎰 /casino — слоты (стартовые $1000)\n\n"
        "💬 <b>Даяна, ответь [вопрос]</b> — спросить Даяну\n"
        "⚖️ <b>Даяна рассуди</b> — рассудить спор\n"
        "   └ ответь на сообщение из спора для точного контекста\n"
        "   └ или: Даяна рассуди [суть спора]\n"
        "👉 <b>Даяна кто виноват</b> — назначить виноватого\n"
        "   └ ответь на сообщение или добавь подсказку"
    )
    await message.answer(help_text, parse_mode="HTML")

@dp.message(Command("game"))
async def cmd_game(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return
    user = message.from_user
    if not user:
        return
    user_name = user.full_name or user.username or "Анон"
    token = generate_game_token(user.id, user_name)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(
            text=f"🎮 Играть — ссылка для {user_name}",
            url=f"{GAME_URL}?token={token}"
        )
    ]])
    await message.answer(
        f"🍆 <b>{user_name}</b>, найди писюн!\n"
        f"<i>Ссылка действует 5 минут — только для тебя</i>",
        reply_markup=keyboard, parse_mode="HTML"
    )

@dp.message(Command("casino"))
async def cmd_casino(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return
    user = message.from_user
    if not user:
        return
    user_name = user.full_name or user.username or "Анон"
    token = generate_game_token(user.id, user_name)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(
            text=f"🎰 Играть — ссылка для {user_name}",
            url=f"{CASINO_URL}?token={token}"
        )
    ]])
    await message.answer(
        f"🎰 <b>{user_name}</b>, добро пожаловать в казино!\n"
        f"<i>Стартовый баланс $1000. Удачи — она тебе понадобится.</i>\n"
        f"<i>Ссылка действует 5 минут — только для тебя</i>",
        reply_markup=keyboard, parse_mode="HTML"
    )

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
        if i < 3: medal = medals[i]
        elif i == len(rows) - 1 and len(rows) > 3: medal = "💩"
        else: medal = "▪️"
        text += f"{medal} {name} — {wins} нашёл / {losses} мимо ({pct}%)\n"
    if len(rows) > 1:
        text += f"\n🤡 Главный мимострел: <b>{rows[-1][0]}</b>"
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
            cursor.execute('SELECT wins, losses FROM peepee_scores WHERE chat_id = %s AND user_id = %s',
                           (message.chat.id, user_id))
            row = cursor.fetchone()
    finally:
        release_conn(conn)
    if not row:
        await message.answer(f"{name}, ты ещё не играл. Введи /game!")
        return
    wins, losses = row
    total = wins + losses
    pct = int((wins / total) * 100) if total > 0 else 0
    if pct >= 60: verdict = "Настоящий охотник 🏆"
    elif pct >= 40: verdict = "Так себе, но бывает 🤷"
    else: verdict = "Позор семьи 💩"
    await message.answer(
        f"🍆 <b>Статистика {name}:</b>\n\nНашёл: {wins}\nПромазал: {losses}\nТочность: {pct}%\n\nВердикт: {verdict}",
        parse_mode="HTML"
    )

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
            if hours < 1: hours = 1
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
                SELECT user_name, message_text, timestamp FROM history
                WHERE chat_id = %s AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
                ORDER BY timestamp ASC
            ''', (message.chat.id, hours))
            all_rows = cursor.fetchall()
    except Exception as e:
        await status_msg.edit_text("Ошибка при чтении базы данных. Попробуй ещё раз.")
        return
    finally:
        release_conn(conn)
    if not all_rows:
        await status_msg.edit_text("За это время сообщений нет. Либо вы спите, либо я сломался.")
        return
    try:
        raw_summary = get_ai_summary(build_prompt_text(all_rows), f"{hours} ч.", len(all_rows))
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

# 17. СБОР СООБЩЕНИЙ
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
        text_lower = message.text.lower()

        # ── Даяна ответь ──
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

        # ── Даяна рассуди ──
        if "даяна" in text_lower and "рассуди" in text_lower:
            try:
                # Подсказка — текст после "рассуди"
                idx = text_lower.index("рассуди") + len("рассуди")
                hint = message.text[idx:].strip() or None

                # Если ответили на конкретное сообщение — берём контекст вокруг него
                if message.reply_to_message and message.reply_to_message.date:
                    anchor_ts = message.reply_to_message.date
                    rows = get_messages_around_timestamp(message.chat.id, anchor_ts, before=15, after=8)
                    context_note = "📌 Контекст вокруг указанного сообщения"
                else:
                    rows = get_last_messages(message.chat.id, limit=25)
                    context_note = "📋 Последние сообщения чата"

                if not rows:
                    await message.reply("Не о чём рассуждать — чат пустой.")
                    return

                verdict = dayana_judge(format_context(rows), hint)
                safe_verdict = verdict.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                await message.reply(f"<b>⚖️ Даяна:</b>\n\n{safe_verdict}", parse_mode="HTML")
            except Exception as e:
                print(f"Ошибка Даяны (рассуди): {e}")
                await message.reply("Не могу рассудить прямо сейчас.")
            return

        # ── Даяна кто виноват ──
        if "даяна" in text_lower and "виноват" in text_lower:
            try:
                # Подсказка — текст после "виноват"
                idx = text_lower.index("виноват") + len("виноват")
                hint = message.text[idx:].strip() or None

                # Если ответили на конкретное сообщение — берём контекст вокруг него
                if message.reply_to_message and message.reply_to_message.date:
                    anchor_ts = message.reply_to_message.date
                    rows = get_messages_around_timestamp(message.chat.id, anchor_ts, before=12, after=6)
                else:
                    rows = get_last_messages(message.chat.id, limit=20)

                if not rows:
                    await message.reply("Не в чем разбираться — чат пустой.")
                    return

                guilty = dayana_guilty(format_context(rows), hint)
                safe_guilty = guilty.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                await message.reply(f"<b>👉 Даяна:</b>\n\n{safe_guilty}", parse_mode="HTML")
            except Exception as e:
                print(f"Ошибка Даяны (виноват): {e}")
                await message.reply("Не могу разобраться прямо сейчас.")
            return

    # Сохраняем сообщения
    if message.text:
        save_message(message.chat.id, author, message.text)
        print(f"[{author}]: {message.text}")
    elif message.voice:
        text = await transcribe_audio(message.voice.file_id, "voice.ogg", message.voice.file_size or 0)
        save_message(message.chat.id, author, f"[🎤 Голосовое]: {text}" if text else "[🎤 Голосовое]: не удалось распознать")
    elif message.video_note:
        text = await transcribe_audio(message.video_note.file_id, "video_note.mp4", message.video_note.file_size or 0)
        save_message(message.chat.id, author, f"[📹 Кружочек]: {text}" if text else "[📹 Кружочек]: не удалось распознать")

async def main():
    init_db_pool()
    init_db()
    cleanup_old_messages()
    print("Бот запущен и готов к работе!")

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_post("/result", handle_result)
    app.router.add_get("/scores", handle_scores)
    app.router.add_post("/casino/spin", handle_casino_spin)
    app.router.add_get("/casino/leaderboard", handle_casino_leaderboard)
    app.router.add_get("/casino/balance", handle_casino_balance)
    for path in ["/result", "/scores", "/casino/spin", "/casino/leaderboard", "/casino/balance"]:
        app.router.add_route("OPTIONS", path, lambda r: web.Response())

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Веб-сервер запущен на порту {port}")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())