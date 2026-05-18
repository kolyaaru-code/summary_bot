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

# Настройки игры "Я никогда не"
NEVER_JOIN_TIMEOUT = 45    # секунд на сбор игроков после первого нажатия
NEVER_VOTE_TIMEOUT = 25    # секунд на голосование за раунд
NEVER_ROUNDS = 6           # раундов в игре

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = Groq(api_key=GROQ_KEY)
client_dayana = Groq(api_key=os.getenv("GROQ_KEY_2"))

# Состояния игр "Я никогда не" — хранятся в памяти
# chat_id -> game state dict
never_games: dict = {}

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
        print(f"Ошибка сохранения сообщения: {e}")
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
                SELECT user_name, question FROM dayana_questions
                WHERE chat_id = %s AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
                ORDER BY RANDOM() LIMIT 3
            ''', (chat_id, hours))
            return cursor.fetchall()
    finally:
        release_conn(conn)

def get_last_messages(chat_id: int, limit: int = 10) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT user_name, message_text, timestamp FROM history
                WHERE chat_id = %s ORDER BY timestamp DESC LIMIT %s
            ''', (chat_id, limit))
            return list(reversed(cursor.fetchall()))
    finally:
        release_conn(conn)

def get_messages_around_timestamp(chat_id: int, anchor_ts, before: int = 15, after: int = 8) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                (SELECT user_name, message_text, timestamp FROM history
                 WHERE chat_id = %s AND timestamp <= %s ORDER BY timestamp DESC LIMIT %s)
                UNION ALL
                (SELECT user_name, message_text, timestamp FROM history
                 WHERE chat_id = %s AND timestamp > %s ORDER BY timestamp ASC LIMIT %s)
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
            ''', (chat_id, user_id, user_name,
                  1 if won else 0, 0 if won else 1,
                  1 if won else 0, 0 if won else 1))
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
                SELECT user_name, wins, losses FROM peepee_scores
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
                ON CONFLICT (chat_id, user_id) DO UPDATE SET user_name = EXCLUDED.user_name
                RETURNING balance
            ''', (chat_id, user_id, user_name, CASINO_START_BALANCE))
            conn.commit()
            return cursor.fetchone()[0]
    except Exception as e:
        print(f"Ошибка casino balance: {e}")
        conn.rollback()
        return CASINO_START_BALANCE
    finally:
        release_conn(conn)

def update_casino_balance(chat_id: int, user_id: int, delta: int) -> int:
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                UPDATE casino_balances SET balance = balance + %s
                WHERE chat_id = %s AND user_id = %s RETURNING balance
            ''', (delta, chat_id, user_id))
            conn.commit()
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        print(f"Ошибка update casino balance: {e}")
        conn.rollback()
        return 0
    finally:
        release_conn(conn)

def get_casino_leaderboard(chat_id: int) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT user_name, balance FROM casino_balances
                WHERE chat_id = %s ORDER BY balance DESC
            ''', (chat_id,))
            return cursor.fetchall()
    finally:
        release_conn(conn)

# 4. КАЗИНО — СЛОТЫ
SLOT_SYMBOLS = ['🍒', '🍋', '7️⃣', '💎', '🍆']

def spin_slots(bet: int) -> dict:
    """
    Режим "казино всегда побеждает":
      78%  — все разные (проигрыш, x0)
      13%  — два одинаковых (x1.1)
       5%  — 🍒🍒🍒 (x2)
      2.5% — 🍋🍋🍋 (x3.5)
       1%  — 7️⃣7️⃣7️⃣ (x10)
      0.4% — 💎💎💎 (x7)
      0.1% — 🍆🍆🍆 (x20, джекпот)
    Матожидание ~0.57$. Казино забирает 43%.
    """
    r = random.random()
    if r < 0.78:
        symbols = random.sample(SLOT_SYMBOLS, 3)
        multiplier = 0
        result_type = "lose"
    elif r < 0.91:
        sym = random.choice(SLOT_SYMBOLS)
        others = [s for s in SLOT_SYMBOLS if s != sym]
        third = random.choice(others)
        positions = [0, 1, 2]
        match_pos = random.sample(positions, 2)
        symbols = [third, third, third]
        symbols[match_pos[0]] = sym
        symbols[match_pos[1]] = sym
        symbols[[p for p in positions if p not in match_pos][0]] = third
        multiplier = 1.1
        result_type = "pair"
    elif r < 0.96:
        symbols = ['🍒', '🍒', '🍒']
        multiplier = 2
        result_type = "win"
    elif r < 0.985:
        symbols = ['🍋', '🍋', '🍋']
        multiplier = 3.5
        result_type = "win"
    elif r < 0.995:
        symbols = ['7️⃣', '7️⃣', '7️⃣']
        multiplier = 10
        result_type = "bigwin"
    elif r < 0.999:
        symbols = ['💎', '💎', '💎']
        multiplier = 7
        result_type = "bigwin"
    else:
        symbols = ['🍆', '🍆', '🍆']
        multiplier = 20
        result_type = "jackpot"

    winnings = int(bet * multiplier)
    delta = winnings - bet
    return {"symbols": symbols, "multiplier": multiplier, "winnings": winnings,
            "delta": delta, "result_type": result_type}

def get_casino_comment(name: str, result_type: str, delta: int, new_balance: int) -> str:
    if result_type == "jackpot":
        return random.choice([
            f"ДЖЕКПОТ, СУКА!!! {name}, ты выиграл всё что можно было выиграть в этой помойке! Звони маме, пиши в резюме. Но мы оба знаем — ты это всё просрёшь обратно. Крути дальше.",
            f"🍆🍆🍆 ТРИ ПИСЮНА! {name}, это знак судьбы. Либо немедленно уходи победителем, либо оставайся и проиграй всё. Мы угадали что ты выберешь.",
            f"СТОП. {name} сорвал джекпот. Казино официально в панике. Наслаждайся моментом — он не повторится никогда в жизни.",
        ])
    if result_type == "bigwin":
        return random.choice([
            f"ЕБАТЬ ТЫ ФОРТОВЫЙ, {name}! Скорее депай ещё пока колесо фортуны не заметило свою ошибку.",
            f"Ничего себе, {name}! Большой куш! Самое время остановиться... но ты же не остановишься, да?",
            f"{name} поднял серьёзные бабки. Казино смотрит на тебя с уважением и ненавистью одновременно.",
        ])
    if result_type == "win":
        return random.choice([
            f"О, {name} выиграл! Скорее депай ещё пока везёт, идиот. Удача — она как кошка: погладил раз, укусит два.",
            f"Смотри-ка, {name} в плюсе! Это называется 'начало конца'.",
            f"{name}, поздравляю с маленькой победой в большой войне с казино. Спойлер: казино выиграет войну.",
        ])
    if result_type == "pair":
        return random.choice([
            f"Пара у {name}. Х1.1, красавчик. Это не выигрыш, это подачка. Казино кормит тебя с ладони как голубя.",
            f"{name}, пара — это казино говорит тебе 'иди сюда, хороший'. Не ведись.",
            f"Маленький плюсик для {name}. Аппарат прогревается.",
        ])
    if new_balance >= 1000:
        return random.choice([
            f"Мимо, {name}. Ты ещё в плюсе — есть что терять. Это самое опасное состояние для лудомана.",
            f"{name} слил ставку. Деньги ещё есть, значит казино своё ещё получит.",
            f"Ай, {name}, не повезло. Зато ты богатый пока. Ключевое слово — пока.",
        ])
    elif new_balance >= 500:
        return random.choice([
            f"Хм, {name}... Денежки тают. Чуешь этот запах? Это твои сбережения горят.",
            f"{name}, осторожно — пахнет лудкой. Ты ещё не закрыл вкладку? Конечно нет.",
            f"Баланс падает, {name}. Всего одна удачная ставка и отыграюсь. Классика жанра.",
        ])
    elif new_balance >= 0:
        return random.choice([
            f"Почка ещё на месте, {name}? Проверь — скоро пригодится. Ты почти на нуле.",
            f"{name}, ты в опасной близости от дна. Большинство людей здесь остановились бы.",
            f"Осталось совсем чуть-чуть, {name}. Крути.",
        ])
    elif new_balance >= -1000:
        return random.choice([
            f"ПОЗДРАВЛЯЮ, {name}! Ты официально в минусе! Почку уже оценил?",
            f"{name} ушёл в минус. Это не конец — это начало настоящей лудки.",
            f"О, {name} в красной зоне! Звони другу, занимай бабки — казино ждёт.",
        ])
    elif new_balance >= -5000:
        return random.choice([
            f"{name}, ты уже серьёзно в яме. Хату заложил? Машину продал? Нет? Значит ещё есть что терять.",
            f"Глубокий минус у {name}. На этом уровне нормальные люди звонят на горячую линию.",
            f"СТОП, {name}. Нет, серьёзно, стоп. Подумал? Хорошо. А теперь крути.",
        ])
    else:
        return random.choice([
            f"ЛЕГЕНДА. {name} установил антирекорд казино. Напиши завещание и крути дальше.",
            f"{name}, ты на такой глубине что уже видно магму. Казино на тебе построило новое крыло.",
            f"На этом уровне долга, {name}, уже не стыдно. Это искусство.",
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
        return f"[файл слишком большой: {size_mb:.1f} МБ]"
    try:
        buffer = io.BytesIO()
        await bot.download(file_id, destination=buffer)
        buffer.seek(0)
        buffer.name = filename
        transcription = client.audio.transcriptions.create(model="whisper-large-v3-turbo", file=buffer)
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
        volume_instruction = "Сообщений мало — будь краток."
    elif message_count < 50:
        volume_instruction = "Средняя активность — стандартный разбор."
    else:
        volume_instruction = "Чат бурлил — можешь развернуться, но без воды."
    prompt = f"""
Ты — Батя этого чата. Не модератор, не ведущий, не журналист. Батя.
Ты знаешь всех в лицо, помнишь кто что говорил месяц назад и не даёшь никому забыть об этом.

ВВОДНЫЕ:
- Период: {timeframe_text}
- Сообщений: {message_count}
- {volume_instruction}

КАК ГОВОРИШЬ:
- Матом — естественно, как в разговоре с друзьями.
- Каждого называешь по имени.
- Если кто-то нёс хуйню — говоришь прямо.
- Короткие удары. Никакой воды.
- Пишешь живо, резко, с характером.

ЧТО ДОЛЖНО БЫТЬ:
- Атмосфера чата
- Кто отличился и как
- Что обсуждали и чем кончилось
- Незакрытые споры
- Вердикт одной фразой

ЗАПРЕЩЕНО: мягкие формулировки, одинаковая структура, вата, хвалить без подъёба.

Голосовые [🎤] и кружочки [📹] — полноценные сообщения.

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
    raise Exception("Все модели исчерпали лимит.")

# 9. ДАЯНА — ВОПРОС
def ask_dayana(question: str) -> str:
    prompt = f"""
Ты — Даяна. Секретарша со стальными нервами и острым языком.
Чётко, по существу, без воды. Можешь срезать одной фразой.
Никаких "конечно!", "отличный вопрос!". Говоришь как живой человек.
Отвечаешь на русском языке.

ВОПРОС: {question}
"""
    completion = client_dayana.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.8, max_tokens=800,
    )
    return completion.choices[0].message.content

# 10. ДАЯНА — РАССУДИТЬ
def dayana_judge(context: str, hint: str = None) -> str:
    hint_block = f"\nЧТО НУЖНО РАССУДИТЬ (подсказка):\n{hint}\n" if hint else ""
    prompt = f"""
Ты — Даяна. Секретарша со стальными нервами, острым языком и абсолютным чувством справедливости.

ШАГ 1 — РАЗБЕРИСЬ (про себя, не пиши):
- Кто участвует в споре? Назови имена.
- В чём суть разногласия — одним предложением.
- Кто первым начал и кто эскалировал?
- Игнорируй посторонние сообщения не по теме.
- У кого более весомые аргументы?

ШАГ 2 — ВЫНЕСИ ВЕРДИКТ (это и пиши):
- Начни с атмосферного действия: *[действие]* — текст.
- Пример: *закуривает сигарету Kiss* — Так, разберёмся...
- Называй стороны по именам. Чёткий вердикт. Никаких "с одной стороны".
- Можешь поддеть неправого — справедливо.
- Максимум 5-6 предложений. Отвечаешь на русском.
{hint_block}
ПЕРЕПИСКА:
{context}

Вынеси вердикт.
"""
    for model in ["qwen/qwen3-32b", "llama-3.3-70b-versatile"]:
        try:
            completion = client_dayana.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7, max_tokens=600,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if "rate_limit" in str(e).lower() or "model" in str(e).lower():
                print(f"Даяна рассуди: {model} недоступна, переключаюсь...")
                continue
            raise
    raise Exception("Все модели недоступны")

# 11. ДАЯНА — ВИНОВАТ
def dayana_guilty(context: str, hint: str = None) -> str:
    hint_block = f"\nПОДСКАЗКА:\n{hint}\n" if hint else ""
    prompt = f"""
Ты — Даяна. Секретарша с холодной головой и нулевой терпимостью к отмазкам.

ШАГ 1 — ПРОАНАЛИЗИРУЙ (про себя):
- Что произошло? Хронология.
- Кто что сделал — конкретно.
- Игнорируй посторонний шум.
- Кто объективно облажался?

ШАГ 2 — НАЗНАЧЬ ВИНОВАТОГО (это и пиши):
- Конкретное имя. Конкретная причина.
- Саркастично — но справедливо.
- Если вина распределена — назови главного.
- 3-4 предложения. Отвечаешь на русском.
{hint_block}
ПЕРЕПИСКА:
{context}

Назначь виноватого.
"""
    for model in ["qwen/qwen3-32b", "llama-3.3-70b-versatile"]:
        try:
            completion = client_dayana.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7, max_tokens=500,
            )
            return completion.choices[0].message.content
        except Exception as e:
            if "rate_limit" in str(e).lower() or "model" in str(e).lower():
                print(f"Даяна виноват: {model} недоступна, переключаюсь...")
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

# ═══════════════════════════════════════════════
# 14. ИГРА "Я НИКОГДА НЕ"
# ═══════════════════════════════════════════════

NEVER_CATEGORIES = [
    ("🔞 Интимное", "секс, отношения, измены, пикантные ситуации между людьми"),
    ("✈️ Путешествия", "страны, приключения в поездках, экзотика, странные ситуации за границей"),
    ("🍕 Еда и вкусы", "странная еда, гастрономические подвиги, отвращение, экзотические блюда"),
    ("🤝 С друзьями", "совместные безумства, предательство друга, дружеские споры и пари"),
    ("🪂 Экстрим", "прыжки, скорость, риск, адреналин, опасные ситуации"),
    ("💼 Работа и деньги", "увольнения, долги, крупные траты, халтура, странные подработки"),
    ("🍺 Вечеринки и алкоголь", "пьяные истории, стыдные моменты на вечеринках, утро после"),
    ("😈 Мелкие грехи", "мелкое воровство, ложь, нарушение правил, мелкое мошенничество"),
    ("😱 Страхи и фобии", "то от чего реально страшно, панические ситуации, столкновение со страхом"),
    ("💔 Отношения и бывшие", "расставания, ревность, странные свидания, бывшие партнёры"),
    ("👮 На грани закона", "реальные приводы в полицию, разговоры с копами, ушёл от наказания"),
    ("🤡 Публичный позор", "упал на людях, сказал что-то не то не тому, опозорился на важном событии"),
]

def generate_never_phrase(players: list, chat_context: str = "", used_phrases: list = [], category: tuple = None) -> str:
    players_str = ", ".join(players)
    context_block = f"\nИСТОРИЯ ЧАТА ДЛЯ ВДОХНОВЕНИЯ:\n{chat_context}\n" if chat_context else ""
    used_block = f"\nЭТИ ФРАЗЫ УЖЕ БЫЛИ — НЕ ПОВТОРЯЙ:\n" + "\n".join(f"- {p}" for p in used_phrases) + "\n" if used_phrases else ""
    cat_name, cat_desc = category if category else ("Разное", "любая тема")

    prompt = f"""
Ты придумываешь фразы для игры "Я никогда не" для взрослой компании друзей 18+.

ИГРОКИ (знай их имена, но НЕ вставляй в фразу): {players_str}

КАТЕГОРИЯ ЭТОГО РАУНДА: {cat_name}
ТЕМА: {cat_desc}

ПРАВИЛА:
- Фраза начинается со слов "Я никогда не"
- Строго по теме категории — не отклоняйся
- Универсальная — про опыт или действие, НЕ про конкретного человека из чата
- Провокационно и смешно — чтобы половина хотела признаться
- Никаких имён игроков в самой фразе
- Одна конкретная ситуация, не абстрактная
{used_block}{context_block}
Выдай ТОЛЬКО одну фразу начиная со слов "Я никогда не". Без кавычек, без пояснений.
"""
    for model in ["llama-3.3-70b-versatile", "qwen/qwen3-32b"]:
        try:
            completion = client_dayana.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.95,
                max_tokens=100,
            )
            phrase = completion.choices[0].message.content.strip().strip('"\'«»')
            if not phrase.lower().startswith("я никогда не"):
                phrase = "Я никогда не " + phrase
            return phrase
        except Exception as e:
            if "rate_limit" in str(e).lower():
                continue
            raise
    return "Я никогда не делал что-то о чём потом жалел"


def build_never_join_text(game: dict) -> str:
    """Текст сообщения в фазе сбора игроков"""
    players = list(game["players"].values())
    count = len(players)
    if count == 0:
        players_str = "<i>пока никого нет...</i>"
    else:
        players_str = ", ".join(f"<b>{p}</b>" for p in players)

    return (
        f"🎮 <b>Игра «Я никогда не»</b>\n\n"
        f"Нажми кнопку чтобы войти в игру!\n"
        f"Игра стартует через {NEVER_JOIN_TIMEOUT} сек после первого игрока.\n\n"
        f"👥 Игроки ({count}): {players_str}"
    )


def build_never_vote_text(game: dict) -> str:
    """Текст сообщения во время голосования"""
    phrase = game["current_phrase"]
    round_n = game["round"]
    total = game["max_rounds"]
    categories = game.get("categories", [])
    cat_label = categories[round_n - 1][0] if categories and round_n <= len(categories) else ""

    did_names = [game["players"][uid] for uid in game["votes"] if game["votes"][uid] == "did" and uid in game["players"]]
    never_names = [game["players"][uid] for uid in game["votes"] if game["votes"][uid] == "never" and uid in game["players"]]

    did_str = ", ".join(f"<b>{n}</b>" for n in did_names) if did_names else "—"
    never_str = ", ".join(f"<b>{n}</b>" for n in never_names) if never_names else "—"

    # Новые игроки которые ещё не проголосовали
    voted = set(game["votes"].keys())
    all_players = set(game["players"].keys())
    pending = [game["players"][uid] for uid in (all_players - voted)]
    pending_str = f"\n⏳ Ещё не ответили: {', '.join(pending)}" if pending else ""

    return (
        f"🔥 <b>Раунд {round_n}/{total}</b> {cat_label}\n\n"
        f"<b>{phrase}</b>\n\n"
        f"🙋 Делал ({len(did_names)}): {did_str}\n"
        f"🙅 Не делал ({len(never_names)}): {never_str}"
        f"{pending_str}"
    )


def build_never_results_text(game: dict) -> str:
    """Итоги всей игры"""
    scores = game["scores"]  # user_id -> кол-во "делал"
    players = game["players"]

    if not players:
        return "Никто не играл 🤷"

    # Сортируем по количеству "делал" (больше = опытнее жизни)
    sorted_players = sorted(players.items(), key=lambda x: scores.get(x[0], 0), reverse=True)
    total_rounds = game["max_rounds"]

    text = f"🏁 <b>Игра «Я никогда не» завершена!</b>\n\n"
    text += f"<b>Итоги за {total_rounds} раундов:</b>\n\n"

    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, name) in enumerate(sorted_players):
        did_count = scores.get(uid, 0)
        pct = int((did_count / total_rounds) * 100)
        medal = medals[i] if i < 3 else "▪️"
        verdict = "прожжённый" if pct >= 70 else ("бывалый" if pct >= 40 else "скромняга")
        text += f"{medal} <b>{name}</b> — делал {did_count}/{total_rounds} раз ({pct}%) — {verdict}\n"

    # Самый честный (больше всех "делал")
    if sorted_players:
        winner_uid, winner_name = sorted_players[0]
        loser_uid, loser_name = sorted_players[-1]
        winner_count = scores.get(winner_uid, 0)
        loser_count = scores.get(loser_uid, 0)

        if winner_count > 0:
            text += f"\n🏆 <b>{winner_name}</b> — самый опытный. Жил на полную."
        if loser_count == 0:
            text += f"\n😇 <b>{loser_name}</b> — либо святой, либо врёт."
        elif len(sorted_players) > 1 and loser_count < winner_count:
            text += f"\n😂 <b>{loser_name}</b> — скромняга чата. Или просто стеснялся признаваться."

    return text


def build_never_keyboard_join(game: dict) -> types.InlineKeyboardMarkup:
    """Клавиатура в фазе сбора"""
    count = len(game["players"])
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"🙋 Я играю! ({count})", callback_data="never_join")],
        [types.InlineKeyboardButton(text="▶️ Начать сейчас", callback_data="never_start")],
    ])


def build_never_keyboard_vote(game: dict) -> types.InlineKeyboardMarkup:
    """Клавиатура для голосования + кнопка присоединиться"""
    did_count = sum(1 for v in game["votes"].values() if v == "did")
    never_count = sum(1 for v in game["votes"].values() if v == "never")
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text=f"🙋 Делал ({did_count})", callback_data="never_did"),
            types.InlineKeyboardButton(text=f"🙅 Не делал ({never_count})", callback_data="never_never"),
        ],
        [types.InlineKeyboardButton(text="➕ Я тоже играю!", callback_data="never_join")],
    ])


async def never_auto_start(chat_id: int, message_id: int):
    """Таймер автостарта после сбора игроков"""
    await asyncio.sleep(NEVER_JOIN_TIMEOUT)
    game = never_games.get(chat_id)
    if not game or game.get("phase") != "joining":
        return
    if len(game["players"]) < 2:
        try:
            await bot.edit_message_text(
                "😕 Недостаточно игроков. Нужно хотя бы 2 человека.\nНапиши /never чтобы начать заново.",
                chat_id=chat_id, message_id=message_id,
                reply_markup=None
            )
        except Exception:
            pass
        never_games.pop(chat_id, None)
        return
    await never_start_game(chat_id, message_id)


async def never_start_game(chat_id: int, message_id: int):
    """Запускает игру — переходим к первому раунду"""
    game = never_games.get(chat_id)
    if not game:
        return

    # Отменяем таймер сбора если он ещё тикает
    if game.get("join_task") and not game["join_task"].done():
        game["join_task"].cancel()

    game["phase"] = "playing"
    game["round"] = 0
    game["scores"] = {uid: 0 for uid in game["players"]}
    game["join_message_id"] = message_id

    await never_next_round(chat_id)


async def never_next_round(chat_id: int):
    """Запускает следующий раунд"""
    game = never_games.get(chat_id)
    if not game:
        return

    game["round"] += 1
    if game["round"] > game["max_rounds"]:
        await never_finish(chat_id)
        return

    game["phase"] = "voting"
    game["votes"] = {}

    # Берём немного истории чата для вдохновения AI
    try:
        rows = get_last_messages(chat_id, limit=15)
        chat_context = format_context(rows)
    except Exception:
        chat_context = ""

    players_list = list(game["players"].values())

    # Генерируем фразу через AI
    try:
        phrase = generate_never_phrase(players_list, chat_context, game["used_phrases"])
        game["used_phrases"].append(phrase)
    except Exception as e:
        print(f"Ошибка генерации фразы: {e}")
        phrase = "Я никогда не делал что-то о чём потом жалел"

    game["current_phrase"] = phrase

    text = build_never_vote_text(game)
    keyboard = build_never_keyboard_vote(game)

    try:
        # Редактируем существующее сообщение или шлём новое
        if game.get("current_message_id"):
            await bot.edit_message_text(
                text, chat_id=chat_id,
                message_id=game["current_message_id"],
                reply_markup=keyboard, parse_mode="HTML"
            )
        else:
            msg = await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
            game["current_message_id"] = msg.message_id
    except Exception as e:
        print(f"Ошибка отправки раунда: {e}")
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
            game["current_message_id"] = msg.message_id
        except Exception:
            pass

    # Запускаем таймер голосования
    task = asyncio.create_task(never_vote_timer(chat_id, game["round"]))
    game["vote_task"] = task


async def never_vote_timer(chat_id: int, round_n: int):
    """Таймер окончания голосования"""
    await asyncio.sleep(NEVER_VOTE_TIMEOUT)
    game = never_games.get(chat_id)
    if not game or game.get("phase") != "voting" or game.get("round") != round_n:
        return
    await never_next_round(chat_id)


async def never_finish(chat_id: int):
    """Завершает игру и показывает итоги"""
    game = never_games.get(chat_id)
    if not game:
        return

    text = build_never_results_text(game)
    msg_id = game.get("current_message_id")

    try:
        if msg_id:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=msg_id,
                reply_markup=None, parse_mode="HTML"
            )
        else:
            await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка финала игры: {e}")
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            pass

    never_games.pop(chat_id, None)


# ── CALLBACK: КНОПКИ ИГРЫ ──
@dp.callback_query(lambda c: c.data in ("never_join", "never_start", "never_did", "never_never"))
async def never_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    user_name = callback.from_user.full_name or callback.from_user.username or "Анон"
    action = callback.data
    game = never_games.get(chat_id)

    if not game:
        await callback.answer("Игра уже закончилась. Начни новую: /never", show_alert=False)
        return

    # ── Присоединиться ──
    if action == "never_join":
        game["players"][user_id] = user_name

        # Инициализируем счёт для нового игрока если игра уже идёт
        if "scores" in game and user_id not in game["scores"]:
            game["scores"][user_id] = 0

        if game["phase"] == "joining":
            # Запускаем таймер при первом игроке
            if len(game["players"]) == 1 and not game.get("join_task"):
                task = asyncio.create_task(
                    never_auto_start(chat_id, callback.message.message_id)
                )
                game["join_task"] = task

            try:
                await callback.message.edit_text(
                    build_never_join_text(game),
                    reply_markup=build_never_keyboard_join(game),
                    parse_mode="HTML"
                )
            except Exception:
                pass
            await callback.answer(f"Ты в игре, {user_name}! 🙋", show_alert=False)

        elif game["phase"] == "voting":
            # Можно присоединиться во время игры
            try:
                await callback.message.edit_text(
                    build_never_vote_text(game),
                    reply_markup=build_never_keyboard_vote(game),
                    parse_mode="HTML"
                )
            except Exception:
                pass
            await callback.answer(f"Добро пожаловать, {user_name}! Голосуй! 🎮", show_alert=False)
        return

    # ── Начать сейчас ──
    if action == "never_start":
        if game["phase"] != "joining":
            await callback.answer("Игра уже идёт!", show_alert=False)
            return
        if len(game["players"]) < 2:
            await callback.answer("Нужно хотя бы 2 игрока!", show_alert=True)
            return
        await callback.answer("Поехали! 🚀", show_alert=False)
        await never_start_game(chat_id, callback.message.message_id)
        return

    # ── Голосование ──
    if action in ("never_did", "never_never"):
        if game["phase"] != "voting":
            await callback.answer("Голосование закончилось!", show_alert=False)
            return

        # Если человек ещё не в игре — добавляем
        if user_id not in game["players"]:
            game["players"][user_id] = user_name
            if "scores" in game:
                game["scores"][user_id] = 0

        vote = "did" if action == "never_did" else "never"
        prev_vote = game["votes"].get(user_id)

        if prev_vote == vote:
            await callback.answer("Ты уже так проголосовал!", show_alert=False)
            return

        game["votes"][user_id] = vote

        # Обновляем счёт
        if vote == "did":
            game["scores"][user_id] = game["scores"].get(user_id, 0) + 1
            # Если переголосовал с "never" — убираем предыдущий счёт
            if prev_vote == "never":
                pass  # счёт уже не менялся за "never"
        elif vote == "never" and prev_vote == "did":
            # Переголосовал с "did" на "never" — убираем очко
            game["scores"][user_id] = max(0, game["scores"].get(user_id, 0) - 1)

        try:
            await callback.message.edit_text(
                build_never_vote_text(game),
                reply_markup=build_never_keyboard_vote(game),
                parse_mode="HTML"
            )
        except Exception:
            pass

        phrase = "Делал 🙋" if vote == "did" else "Не делал 🙅"
        await callback.answer(phrase, show_alert=False)

        # Если все проголосовали — переходим к следующему раунду досрочно
        all_voted = all(uid in game["votes"] for uid in game["players"])
        if all_voted and len(game["players"]) >= 2:
            if game.get("vote_task") and not game["vote_task"].done():
                game["vote_task"].cancel()
            await asyncio.sleep(2)  # небольшая пауза чтобы все увидели результат
            await never_next_round(chat_id)
        return

    await callback.answer()


# ═══════════════════════════════════════════════
# 15. ИГРА "УГАДАЙ АВТОРА"
# ═══════════════════════════════════════════════

QUOTE_JOIN_TIMEOUT = 45
QUOTE_VOTE_TIMEOUT = 30
QUOTE_ROUNDS = 6
QUOTE_MIN_MSG_LENGTH = 15   # минимальная длина цитаты
QUOTE_MAX_MSG_LENGTH = 200  # максимальная длина цитаты

quote_games: dict = {}

def get_random_quotes(chat_id: int, players: dict, count: int, used_ids: list) -> list:
    """Берём случайные сообщения из БД только от игроков, не повторяя использованные"""
    if not players:
        return []
    player_names = list(players.values())
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            placeholders_names = ",".join(["%s"] * len(player_names))
            placeholders_used = ",".join(["%s"] * len(used_ids)) if used_ids else "NULL"
            query = f"""
                SELECT id, user_name, message_text FROM history
                WHERE chat_id = %s
                AND user_name IN ({placeholders_names})
                AND array_length(regexp_split_to_array(trim(message_text), '\\s+'), 1) >= 4
                AND LENGTH(message_text) <= %s
                AND message_text NOT LIKE '[%%'
                {"AND id NOT IN (" + placeholders_used + ")" if used_ids else ""}
                ORDER BY RANDOM()
                LIMIT %s
            """
            params = [chat_id] + player_names + [QUOTE_MAX_MSG_LENGTH]
            if used_ids:
                params += used_ids
            params.append(count)
            cursor.execute(query, params)
            return cursor.fetchall()  # (id, user_name, message_text)
    finally:
        release_conn(conn)


def build_quote_join_text(game: dict) -> str:
    players = list(game["players"].values())
    count = len(players)
    players_str = ", ".join(f"<b>{p}</b>" for p in players) if players else "<i>пока никого нет...</i>"
    return (
        f"💬 <b>Игра «Угадай автора»</b>\n\n"
        f"Я буду показывать реальные цитаты из этого чата — угадывайте кто написал!\n"
        f"Старт через {QUOTE_JOIN_TIMEOUT} сек после первого игрока.\n\n"
        f"👥 Игроки ({count}): {players_str}"
    )


def build_quote_vote_text(game: dict) -> str:
    quote = game["current_quote"]
    round_n = game["round"]
    total = game["max_rounds"]
    votes = game["votes"]       # user_id -> guessed_name
    players = game["players"]   # user_id -> name

    # Считаем сколько проголосовало
    voted_count = len(votes)
    total_players = len(players)

    # Показываем кто уже проголосовал (без раскрытия ответа)
    voted_names = [players[uid] for uid in votes if uid in players]
    voted_str = ", ".join(voted_names) if voted_names else "—"

    pending = [players[uid] for uid in players if uid not in votes]
    pending_str = f"\n⏳ Ещё не ответили: {', '.join(pending)}" if pending else ""

    return (
        f"💬 <b>Раунд {round_n}/{total}</b>\n\n"
        f"<b>Кто написал это сообщение?</b>\n\n"
        f"<i>«{quote}»</i>\n\n"
        f"✅ Проголосовали ({voted_count}/{total_players}): {voted_str}"
        f"{pending_str}"
    )


def build_quote_reveal_text(game: dict) -> str:
    """Текст после раскрытия автора"""
    quote = game["current_quote"]
    author = game["current_author"]
    round_n = game["round"]
    total = game["max_rounds"]
    votes = game["votes"]
    players = game["players"]

    # Кто угадал
    correct = [players[uid] for uid, guess in votes.items() if guess == author and uid in players]
    wrong = [players[uid] for uid, guess in votes.items() if guess != author and uid in players]

    correct_str = ", ".join(f"<b>{n}</b>" for n in correct) if correct else "никто"
    wrong_str = ", ".join(f"<b>{n}</b>" for n in wrong) if wrong else "—"

    # Разбивка кто за кого голосовал
    guesses_lines = []
    # Группируем по варианту ответа
    guess_groups = {}
    for uid, guess in votes.items():
        if uid in players:
            guess_groups.setdefault(guess, []).append(players[uid])
    for guess_name, voters in guess_groups.items():
        marker = "✅" if guess_name == author else "❌"
        guesses_lines.append(f"  {marker} думали что <b>{guess_name}</b>: {', '.join(voters)}")

    guesses_str = "\n".join(guesses_lines) if guesses_lines else "никто не проголосовал"

    return (
        f"💬 <b>Раунд {round_n}/{total} — ответ!</b>\n\n"
        f"<i>«{quote}»</i>\n\n"
        f"✍️ Автор: <b>{author}</b>\n\n"
        f"{guesses_str}\n\n"
        f"🎯 Угадали: {correct_str}\n"
        f"💀 Промазали: {wrong_str}"
    )


def build_quote_results_text(game: dict) -> str:
    players = game["players"]
    scores = game["scores"]
    total_rounds = game["max_rounds"]

    if not players:
        return "Никто не играл 🤷"

    sorted_players = sorted(players.items(), key=lambda x: scores.get(x[0], 0), reverse=True)

    text = f"🏁 <b>Игра «Угадай автора» завершена!</b>\n\n"
    text += f"<b>Итоги за {total_rounds} раундов:</b>\n\n"

    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, name) in enumerate(sorted_players):
        count = scores.get(uid, 0)
        medal = medals[i] if i < 3 else "▪️"
        if i == len(sorted_players) - 1 and len(sorted_players) > 1:
            medal = "💀"
        pct = int((count / total_rounds) * 100)
        verdict = "Телепат 🧠" if pct >= 80 else ("Знает своих 👀" if pct >= 50 else "Не угадывает 🤷")
        text += f"{medal} <b>{name}</b> — угадал {count}/{total_rounds} ({pct}%) — {verdict}\n"

    if sorted_players:
        winner_uid, winner_name = sorted_players[0]
        winner_count = scores.get(winner_uid, 0)
        loser_uid, loser_name = sorted_players[-1]
        loser_count = scores.get(loser_uid, 0)
        if winner_count > 0:
            text += f"\n🏆 <b>{winner_name}</b> — знает всех насквозь. Телепат или стукач?"
        if loser_count == 0:
            text += f"\n💀 <b>{loser_name}</b> — либо не знает людей, либо специально валит."

    return text


def build_quote_keyboard_join(game: dict) -> types.InlineKeyboardMarkup:
    count = len(game["players"])
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=f"🙋 Я играю! ({count})", callback_data="quote_join")],
        [types.InlineKeyboardButton(text="▶️ Начать сейчас", callback_data="quote_start")],
    ])


def build_quote_keyboard_vote(game: dict, voter_id: int) -> types.InlineKeyboardMarkup:
    """Кнопки с именами игроков — все включая себя (мог написать сам)"""
    players = game["players"]
    current_guess = game["votes"].get(voter_id)
    buttons = []
    row = []
    for uid, name in players.items():
        label = f"✅ {name}" if current_guess == name else name
        row.append(types.InlineKeyboardButton(
            text=label,
            callback_data=f"quote_vote_{name[:20]}"  # имя как ключ
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([types.InlineKeyboardButton(text="➕ Я тоже играю!", callback_data="quote_join")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def quote_auto_start(chat_id: int, message_id: int):
    await asyncio.sleep(QUOTE_JOIN_TIMEOUT)
    game = quote_games.get(chat_id)
    if not game or game.get("phase") != "joining":
        return
    if len(game["players"]) < 2:
        try:
            await bot.edit_message_text(
                "😕 Недостаточно игроков. Нужно хотя бы 2 человека.\nНапиши /quote чтобы начать заново.",
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except Exception:
            pass
        quote_games.pop(chat_id, None)
        return
    await quote_start_game(chat_id, message_id)


async def quote_start_game(chat_id: int, message_id: int):
    game = quote_games.get(chat_id)
    if not game:
        return
    if game.get("join_task") and not game["join_task"].done():
        game["join_task"].cancel()
    game["phase"] = "playing"
    game["round"] = 0
    game["scores"] = {uid: 0 for uid in game["players"]}
    await quote_next_round(chat_id)


async def quote_next_round(chat_id: int):
    game = quote_games.get(chat_id)
    if not game:
        return

    game["round"] += 1
    if game["round"] > game["max_rounds"]:
        await quote_finish(chat_id)
        return

    game["phase"] = "voting"
    game["votes"] = {}

    # Берём случайную цитату от одного из игроков
    quotes = get_random_quotes(chat_id, game["players"], 1, game["used_ids"])

    if not quotes:
        # Если цитат не хватает — сбрасываем список использованных и пробуем снова
        game["used_ids"] = []
        quotes = get_random_quotes(chat_id, game["players"], 1, [])

    if not quotes:
        # Совсем нет сообщений — пропускаем раунд
        await bot.send_message(chat_id, f"⚠️ Раунд {game['round']}: не удалось найти цитаты. Пишите больше в чат! Пропускаем...")
        await asyncio.sleep(2)
        await quote_next_round(chat_id)
        return

    row = quotes[0]
    msg_id, author, text = row
    game["used_ids"].append(msg_id)
    game["current_quote"] = text
    game["current_author"] = author

    vote_text = build_quote_vote_text(game)
    keyboard = build_quote_keyboard_vote(game, 0)  # 0 = нет voter_id для общего показа

    try:
        if game.get("current_message_id"):
            await bot.edit_message_text(
                vote_text, chat_id=chat_id,
                message_id=game["current_message_id"],
                reply_markup=keyboard, parse_mode="HTML"
            )
        else:
            msg = await bot.send_message(chat_id, vote_text, reply_markup=keyboard, parse_mode="HTML")
            game["current_message_id"] = msg.message_id
    except Exception as e:
        print(f"Ошибка отправки раунда quote: {e}")
        try:
            msg = await bot.send_message(chat_id, vote_text, reply_markup=keyboard, parse_mode="HTML")
            game["current_message_id"] = msg.message_id
        except Exception:
            pass

    task = asyncio.create_task(quote_vote_timer(chat_id, game["round"]))
    game["vote_task"] = task


async def quote_vote_timer(chat_id: int, round_n: int):
    await asyncio.sleep(QUOTE_VOTE_TIMEOUT)
    game = quote_games.get(chat_id)
    if not game or game.get("phase") != "voting" or game.get("round") != round_n:
        return
    await quote_reveal(chat_id)


async def quote_reveal(chat_id: int):
    """Показываем правильный ответ и начисляем очки"""
    game = quote_games.get(chat_id)
    if not game:
        return

    author = game["current_author"]
    votes = game["votes"]
    players = game["players"]

    # Начисляем очки
    for uid, guess in votes.items():
        if guess == author and uid in game["scores"]:
            game["scores"][uid] += 1

    reveal_text = build_quote_reveal_text(game)

    try:
        if game.get("current_message_id"):
            await bot.edit_message_text(
                reveal_text, chat_id=chat_id,
                message_id=game["current_message_id"],
                reply_markup=None, parse_mode="HTML"
            )
    except Exception:
        try:
            await bot.send_message(chat_id, reveal_text, parse_mode="HTML")
        except Exception:
            pass

    # Пауза перед следующим раундом
    await asyncio.sleep(4)
    await quote_next_round(chat_id)


async def quote_finish(chat_id: int):
    game = quote_games.get(chat_id)
    if not game:
        return
    text = build_quote_results_text(game)
    msg_id = game.get("current_message_id")
    try:
        if msg_id:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                        reply_markup=None, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            pass
    quote_games.pop(chat_id, None)


@dp.callback_query(lambda c: c.data.startswith("quote_"))
async def quote_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    user_name = callback.from_user.full_name or callback.from_user.username or "Анон"
    action = callback.data
    game = quote_games.get(chat_id)

    if not game:
        await callback.answer("Игра уже закончилась. Начни новую: /quote", show_alert=False)
        return

    # ── Присоединиться ──
    if action == "quote_join":
        game["players"][user_id] = user_name
        if "scores" in game and user_id not in game["scores"]:
            game["scores"][user_id] = 0

        if game["phase"] == "joining":
            if len(game["players"]) == 1 and not game.get("join_task"):
                task = asyncio.create_task(quote_auto_start(chat_id, callback.message.message_id))
                game["join_task"] = task
            try:
                await callback.message.edit_text(
                    build_quote_join_text(game),
                    reply_markup=build_quote_keyboard_join(game),
                    parse_mode="HTML"
                )
            except Exception:
                pass
            await callback.answer(f"Ты в игре, {user_name}! 🙋", show_alert=False)

        elif game["phase"] == "voting":
            try:
                await callback.message.edit_text(
                    build_quote_vote_text(game),
                    reply_markup=build_quote_keyboard_vote(game, user_id),
                    parse_mode="HTML"
                )
            except Exception:
                pass
            await callback.answer(f"Добро пожаловать, {user_name}! Угадывай! 🎮", show_alert=False)
        return

    # ── Начать сейчас ──
    if action == "quote_start":
        if game["phase"] != "joining":
            await callback.answer("Игра уже идёт!", show_alert=False)
            return
        if len(game["players"]) < 2:
            await callback.answer("Нужно хотя бы 2 игрока!", show_alert=True)
            return
        await callback.answer("Поехали! 🚀", show_alert=False)
        await quote_start_game(chat_id, callback.message.message_id)
        return

    # ── Голосование ──
    if action.startswith("quote_vote_"):
        if game["phase"] != "voting":
            await callback.answer("Голосование закончилось!", show_alert=False)
            return

        if user_id not in game["players"]:
            game["players"][user_id] = user_name
            if "scores" in game:
                game["scores"][user_id] = 0

        guessed_name = action.replace("quote_vote_", "")

        # Проверяем что такой игрок существует
        valid_names = list(game["players"].values())
        if guessed_name not in valid_names:
            await callback.answer("Такого игрока нет", show_alert=True)
            return

        game["votes"][user_id] = guessed_name

        try:
            await callback.message.edit_text(
                build_quote_vote_text(game),
                reply_markup=build_quote_keyboard_vote(game, user_id),
                parse_mode="HTML"
            )
        except Exception:
            pass

        await callback.answer(f"Ты думаешь что это {guessed_name} 🤔", show_alert=False)

        # Если все проголосовали — раскрываем досрочно
        all_voted = all(uid in game["votes"] for uid in game["players"])
        if all_voted and len(game["players"]) >= 2:
            if game.get("vote_task") and not game["vote_task"].done():
                game["vote_task"].cancel()
            await asyncio.sleep(1)
            await quote_reveal(chat_id)
        return

    await callback.answer()


# 16. ВЕБ — CORS
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

# 16. ВЕБ — ЭНДПОИНТЫ
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
            "ok": True, "symbols": result["symbols"], "multiplier": result["multiplier"],
            "winnings": result["winnings"], "delta": result["delta"],
            "result_type": result["result_type"], "new_balance": new_balance, "comment": comment,
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
        balance = get_or_create_casino_balance(ALLOWED_CHAT_ID, int(payload["user_id"]), payload["user_name"])
        return web.json_response({"balance": balance, "user_name": payload["user_name"]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# 17. КОМАНДЫ БОТА
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
        "🎮 /game — найди писюн\n"
        "🍆 /peepee — рейтинг охотников\n"
        "📊 /mypeepee — твоя статистика\n\n"
        "🎰 /casino — слоты (стартовые $1000)\n\n"
        "🎲 /never — игра «Я никогда не» (6 раундов)\n"
        "💬 /quote — игра «Угадай автора» (цитаты из чата)\n\n"
        "    /neverstop — остановить «Я никогда не»\n"
        "    /quotestop — остановить «Угадай автора»\n\n"        "💬 <b>Даяна, ответь [вопрос]</b> — спросить Даяну\n"
        "⚖️ <b>Даяна рассуди</b> — рассудить спор\n"
        "👉 <b>Даяна кто виноват</b> — назначить виноватого"
    )
    await message.answer(help_text, parse_mode="HTML")

@dp.message(Command("never"))
async def cmd_never(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return
    chat_id = message.chat.id

    # Если игра уже идёт — сообщаем
    if chat_id in never_games:
        game = never_games[chat_id]
        if game["phase"] == "joining":
            await message.answer("Уже идёт сбор игроков! Нажми кнопку ниже чтобы войти.")
        else:
            await message.answer(
                f"Игра уже идёт — раунд {game['round']}/{game['max_rounds']}.\n"
                f"Нажми «Я тоже играю!» на текущем сообщении чтобы присоединиться."
            )
        return

    # Создаём новую игру
    never_games[chat_id] = {
        "phase": "joining",
        "players": {},          # user_id -> name
        "votes": {},            # user_id -> "did" | "never"
        "scores": {},           # user_id -> int
        "current_phrase": "",
        "round": 0,
        "max_rounds": NEVER_ROUNDS,
        "current_message_id": None,
        "join_task": None,
        "vote_task": None,
        "used_phrases": [],
        "categories": random.sample(NEVER_CATEGORIES, NEVER_ROUNDS),
    }

    game = never_games[chat_id]
    text = build_never_join_text(game)
    keyboard = build_never_keyboard_join(game)
    msg = await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    game["current_message_id"] = msg.message_id

@dp.message(Command("neverstop"))
async def cmd_neverstop(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return
    chat_id = message.chat.id
    game = never_games.pop(chat_id, None)
    if game:
        if game.get("join_task"): game["join_task"].cancel()
        if game.get("vote_task"): game["vote_task"].cancel()
        await message.answer("🛑 Игра «Я никогда не» остановлена.")
    else:
        await message.answer("Никакой игры сейчас нет.")

@dp.message(Command("quote"))
async def cmd_quote(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return
    chat_id = message.chat.id

    if chat_id in quote_games:
        game = quote_games[chat_id]
        if game["phase"] == "joining":
            await message.answer("Уже идёт сбор игроков! Нажми кнопку ниже чтобы войти.")
        else:
            await message.answer(
                f"Игра уже идёт — раунд {game['round']}/{game['max_rounds']}.\n"
                f"Нажми «Я тоже играю!» на текущем сообщении."
            )
        return

    quote_games[chat_id] = {
        "phase": "joining",
        "players": {},
        "votes": {},
        "scores": {},
        "current_quote": "",
        "current_author": "",
        "round": 0,
        "max_rounds": QUOTE_ROUNDS,
        "current_message_id": None,
        "join_task": None,
        "vote_task": None,
        "used_ids": [],
    }

    game = quote_games[chat_id]
    msg = await message.answer(
        build_quote_join_text(game),
        reply_markup=build_quote_keyboard_join(game),
        parse_mode="HTML"
    )
    game["current_message_id"] = msg.message_id

@dp.message(Command("quotestop"))
async def cmd_quotestop(message: types.Message):
    if not is_chat_allowed(message.chat.id):
        return
    chat_id = message.chat.id
    game = quote_games.pop(chat_id, None)
    if game:
        if game.get("join_task"): game["join_task"].cancel()
        if game.get("vote_task"): game["vote_task"].cancel()
        await message.answer("🛑 Игра «Угадай автора» остановлена.")
    else:
        await message.answer("Никакой игры сейчас нет.")

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
        f"🍆 <b>{user_name}</b>, найди писюн!\n<i>Ссылка действует 5 минут — только для тебя</i>",
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
        await status_msg.edit_text("Ошибка при чтении базы данных.")
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

# 18. СБОР СООБЩЕНИЙ
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

        if "даяна" in text_lower and "рассуди" in text_lower:
            try:
                idx = text_lower.index("рассуди") + len("рассуди")
                hint = message.text[idx:].strip() or None
                if message.reply_to_message and message.reply_to_message.date:
                    rows = get_messages_around_timestamp(message.chat.id, message.reply_to_message.date, 15, 8)
                else:
                    rows = get_last_messages(message.chat.id, limit=25)
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

        if "даяна" in text_lower and "виноват" in text_lower:
            try:
                idx = text_lower.index("виноват") + len("виноват")
                hint = message.text[idx:].strip() or None
                if message.reply_to_message and message.reply_to_message.date:
                    rows = get_messages_around_timestamp(message.chat.id, message.reply_to_message.date, 12, 6)
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