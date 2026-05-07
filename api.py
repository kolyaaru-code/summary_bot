import os
from flask import Flask, request, jsonify
from psycopg2 import pool
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID"))

app = Flask(__name__)
db_pool = pool.SimpleConnectionPool(1, 5, DATABASE_URL)

def get_conn():
    return db_pool.getconn()

def release_conn(conn):
    db_pool.putconn(conn)

@app.route('/stats')
def get_stats():
    user_id = request.args.get('user_id', type=int)
    chat_id = request.args.get('chat_id', type=int)
    if not user_id or chat_id != ALLOWED_CHAT_ID:
        return jsonify({'wins': 0, 'losses': 0})
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                'SELECT wins, losses FROM peepee_scores WHERE chat_id = %s AND user_id = %s',
                (chat_id, user_id)
            )
            row = cursor.fetchone()
            if row:
                return jsonify({'wins': row[0], 'losses': row[1]})
            return jsonify({'wins': 0, 'losses': 0})
    finally:
        release_conn(conn)

@app.route('/game-result', methods=['POST'])
def save_result():
    data = request.json
    user_id = data.get('user_id')
    user_name = data.get('user_name', 'Аноним')
    chat_id = data.get('chat_id')
    won = data.get('won', False)
    if not user_id or chat_id != ALLOWED_CHAT_ID:
        return jsonify({'ok': False})
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
        return jsonify({'ok': True})
    except Exception as e:
        print(f"Ошибка: {e}")
        conn.rollback()
        return jsonify({'ok': False})
    finally:
        release_conn(conn)

@app.route('/leaderboard')
def leaderboard():
    chat_id = request.args.get('chat_id', type=int)
    if chat_id != ALLOWED_CHAT_ID:
        return jsonify([])
    conn = get_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT user_id, user_name, wins, losses
                FROM peepee_scores
                WHERE chat_id = %s AND (wins + losses) > 0
                ORDER BY wins DESC, losses ASC
                LIMIT 20
            ''', (chat_id,))
            rows = cursor.fetchall()
            return jsonify([{
                'user_id': r[0], 'user_name': r[1],
                'wins': r[2], 'losses': r[3]
            } for r in rows])
    finally:
        release_conn(conn)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)