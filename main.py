from flask import Flask, redirect
import sqlite3
import random

app = Flask(__name__)
DATABASE = 'survey.db'

SURVEY_URLS = {
    1: "https://www.surveycake.com/s/Mo6nl",
    2: "https://www.surveycake.com/s/VGwvN",
    3: "https://www.surveycake.com/s/2lMKr",
    4: "https://www.surveycake.com/s/gGKd8"
}

MAX_COUNT_PER_SURVEY = 200 # 每種問卷 200 份

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS survey_counts (
            id INTEGER PRIMARY KEY,
            count INTEGER NOT NULL
        )
    """)
    for i in range(1, 5):
        c.execute("INSERT OR IGNORE INTO survey_counts (id, count) VALUES (?, ?)", (i, 0))
    conn.commit()
    conn.close()

@app.route('/')
def assign_survey():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, count FROM survey_counts WHERE count < ?", (MAX_COUNT_PER_SURVEY,))
    available = c.fetchall()

    if not available:
        conn.close()
        return "已經收滿800份問卷，謝謝參與！"

    selected = random.choice(available)
    survey_id = selected['id']
    c.execute("UPDATE survey_counts SET count = count + 1 WHERE id = ?", (survey_id,))
    conn.commit()
    conn.close()

    return redirect(SURVEY_URLS[survey_id])

if __name__ == "__main__":
    init_db()
    app.run(host='0.0.0.0', port=3000)
