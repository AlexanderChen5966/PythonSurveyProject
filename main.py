from flask import (
    Flask, redirect, request, render_template, session,
    url_for, jsonify, make_response, abort
)
from datetime import datetime, timedelta
import sqlite3
import secrets
import json
import os

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')

DATABASE = 'survey.db'
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')
TOKEN_TTL_HOURS = 2  # pending token 有效時間

# 計數模式：
#   COUNT_ON_ASSIGN=1 → 分配當下即計數（等同舊版行為，不需 SurveyCake 任何設定）
#   未設定（預設）      → 方案 A：填答者填完導回 /complete 才計數
COUNT_ON_ASSIGN = os.environ.get('COUNT_ON_ASSIGN', '') == '1'

# 初次建庫時匯入的預設問卷（之後皆由 /admin 管理）
DEFAULT_SURVEYS = [
    ("問卷一", "https://www.surveycake.com/s/o8Ywr", 500),
    ("問卷二", "https://www.surveycake.com/s/0yv8y", 500),
]

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS surveys (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            url       TEXT NOT NULL,
            max_count INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS responses (
            token        TEXT PRIMARY KEY,
            survey_id    INTEGER NOT NULL,
            status       TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    if c.execute("SELECT COUNT(*) FROM surveys").fetchone()[0] == 0:
        c.executemany(
            "INSERT INTO surveys (name, url, max_count) VALUES (?, ?, ?)",
            DEFAULT_SURVEYS
        )
        # 舊版 survey_counts 若存在，把既有計數轉成 completed 紀錄保留
        old = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='survey_counts'"
        ).fetchone()
        if old:
            now = datetime.now().isoformat(timespec='seconds')
            for row in c.execute("SELECT id, count FROM survey_counts").fetchall():
                for _ in range(row['count']):
                    c.execute(
                        "INSERT INTO responses (token, survey_id, status, created_at, completed_at) "
                        "VALUES (?, ?, 'completed', ?, ?)",
                        (f"migrated-{secrets.token_urlsafe(16)}", row['id'], now, now)
                    )
    conn.commit()
    conn.close()


def expire_stale_tokens(c):
    cutoff = (datetime.now() - timedelta(hours=TOKEN_TTL_HOURS)).isoformat(timespec='seconds')
    c.execute(
        "UPDATE responses SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
        (cutoff,)
    )


def survey_stats(c):
    """回傳所有問卷及其完成數"""
    return c.execute("""
        SELECT s.id, s.name, s.url, s.max_count, s.is_active,
               (SELECT COUNT(*) FROM responses r
                WHERE r.survey_id = s.id AND r.status = 'completed') AS completed
        FROM surveys s ORDER BY s.id
    """).fetchall()


# ---------- 填答者流程 ----------

@app.route('/')
def assign_survey():
    conn = get_db_connection()
    c = conn.cursor()
    expire_stale_tokens(c)

    # cookie 防重複：已有 token 的人不再發新的一份
    existing_token = request.cookies.get('survey_token')
    if existing_token:
        row = c.execute(
            "SELECT r.status, s.url FROM responses r JOIN surveys s ON s.id = r.survey_id "
            "WHERE r.token = ?", (existing_token,)
        ).fetchone()
        if row:
            if row['status'] == 'completed':
                conn.commit()
                conn.close()
                return "您已填答過問卷，謝謝參與！"
            if row['status'] == 'pending':
                conn.commit()
                conn.close()
                return redirect(f"{row['url']}?token={existing_token}")
            # expired：往下重新分配

    # 平均分配：挑完成數最低且未滿額的啟用問卷
    candidates = [s for s in survey_stats(c)
                  if s['is_active'] and s['completed'] < s['max_count']]
    if not candidates:
        conn.commit()
        conn.close()
        return "問卷已全部收滿，謝謝參與！"
    selected = min(candidates, key=lambda s: s['completed'])

    token = secrets.token_urlsafe(24)
    now = datetime.now().isoformat(timespec='seconds')
    if COUNT_ON_ASSIGN:
        # 舊版行為：分配即計數，不依賴 SurveyCake 結束頁導向
        c.execute(
            "INSERT INTO responses (token, survey_id, status, created_at, completed_at) "
            "VALUES (?, ?, 'completed', ?, ?)",
            (token, selected['id'], now, now)
        )
    else:
        c.execute(
            "INSERT INTO responses (token, survey_id, status, created_at) VALUES (?, ?, 'pending', ?)",
            (token, selected['id'], now)
        )
    conn.commit()
    conn.close()

    resp = make_response(redirect(f"{selected['url']}?token={token}"))
    resp.set_cookie('survey_token', token, max_age=60 * 60 * 24 * 30)
    return resp


@app.route('/complete')
def complete():
    token = request.args.get('token', '')
    if not token:
        return "缺少 token，無法確認填答。", 400

    conn = get_db_connection()
    c = conn.cursor()
    expire_stale_tokens(c)
    # 原子更新：只有 pending 的 token 會被標記完成，重複造訪不會重複計數
    c.execute(
        "UPDATE responses SET status = 'completed', completed_at = ? "
        "WHERE token = ? AND status = 'pending'",
        (datetime.now().isoformat(timespec='seconds'), token)
    )
    updated = c.rowcount
    already = None
    if not updated:
        already = c.execute(
            "SELECT status FROM responses WHERE token = ?", (token,)
        ).fetchone()
    conn.commit()
    conn.close()

    if updated:
        return "填答完成，已為您登記，謝謝參與！"
    if already and already['status'] == 'completed':
        return "此份填答已登記過，謝謝參與！"
    return "此連結已失效或無效。", 400


# ---------- 管理介面 ----------

def login_required(f):
    from functools import wraps

    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return wrapped


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin'] = True
            return redirect(url_for('admin_dashboard'))
        error = "密碼錯誤"
    return render_template('login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/admin')
@login_required
def admin_dashboard():
    conn = get_db_connection()
    c = conn.cursor()
    expire_stale_tokens(c)
    stats = survey_stats(c)
    pending = {row['survey_id']: row['n'] for row in c.execute(
        "SELECT survey_id, COUNT(*) AS n FROM responses "
        "WHERE status = 'pending' GROUP BY survey_id"
    ).fetchall()}
    conn.commit()
    conn.close()
    return render_template('admin.html', surveys=stats, pending=pending)


@app.route('/admin/surveys/new', methods=['GET', 'POST'])
@login_required
def survey_new():
    if request.method == 'POST':
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO surveys (name, url, max_count, is_active) VALUES (?, ?, ?, ?)",
            (request.form['name'], request.form['url'],
             int(request.form['max_count']), 1 if request.form.get('is_active') else 0)
        )
        conn.commit()
        conn.close()
        return redirect(url_for('admin_dashboard'))
    return render_template('survey_form.html', survey=None)


@app.route('/admin/surveys/<int:survey_id>/edit', methods=['GET', 'POST'])
@login_required
def survey_edit(survey_id):
    conn = get_db_connection()
    survey = conn.execute("SELECT * FROM surveys WHERE id = ?", (survey_id,)).fetchone()
    if survey is None:
        conn.close()
        abort(404)
    if request.method == 'POST':
        conn.execute(
            "UPDATE surveys SET name = ?, url = ?, max_count = ?, is_active = ? WHERE id = ?",
            (request.form['name'], request.form['url'], int(request.form['max_count']),
             1 if request.form.get('is_active') else 0, survey_id)
        )
        conn.commit()
        conn.close()
        return redirect(url_for('admin_dashboard'))
    conn.close()
    return render_template('survey_form.html', survey=survey)


@app.route('/admin/surveys/<int:survey_id>/delete', methods=['POST'])
@login_required
def survey_delete(survey_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM surveys WHERE id = ?", (survey_id,))
    conn.execute("DELETE FROM responses WHERE survey_id = ?", (survey_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/surveys/<int:survey_id>/adjust', methods=['POST'])
@login_required
def survey_adjust(survey_id):
    """手動校正：與 SurveyCake 後台對帳用"""
    delta = int(request.form['delta'])
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat(timespec='seconds')
    if delta > 0:
        c.execute(
            "INSERT INTO responses (token, survey_id, status, created_at, completed_at) "
            "VALUES (?, ?, 'completed', ?, ?)",
            (f"manual-{secrets.token_urlsafe(16)}", survey_id, now, now)
        )
    else:
        c.execute(
            "DELETE FROM responses WHERE token = ("
            "  SELECT token FROM responses WHERE survey_id = ? AND status = 'completed' "
            "  ORDER BY completed_at DESC LIMIT 1)",
            (survey_id,)
        )
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/export')
@login_required
def admin_export():
    conn = get_db_connection()
    data = {
        "exported_at": datetime.now().isoformat(timespec='seconds'),
        "surveys": [dict(r) for r in conn.execute("SELECT * FROM surveys").fetchall()],
        "responses": [dict(r) for r in conn.execute("SELECT * FROM responses").fetchall()],
    }
    conn.close()
    resp = make_response(json.dumps(data, ensure_ascii=False, indent=2))
    resp.headers['Content-Type'] = 'application/json; charset=utf-8'
    resp.headers['Content-Disposition'] = (
        f"attachment; filename=survey-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    return resp


@app.route('/admin/restore', methods=['GET', 'POST'])
@login_required
def admin_restore():
    error = None
    if request.method == 'POST':
        try:
            data = json.loads(request.form['payload'])
            surveys = data['surveys']
            responses = data['responses']
        except (json.JSONDecodeError, KeyError, TypeError):
            error = "格式錯誤：請貼上先前匯出的完整 JSON 內容"
        else:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DELETE FROM responses")
            c.execute("DELETE FROM surveys")
            for s in surveys:
                c.execute(
                    "INSERT INTO surveys (id, name, url, max_count, is_active) VALUES (?, ?, ?, ?, ?)",
                    (s['id'], s['name'], s['url'], s['max_count'], s['is_active'])
                )
            for r in responses:
                c.execute(
                    "INSERT INTO responses (token, survey_id, status, created_at, completed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (r['token'], r['survey_id'], r['status'], r['created_at'], r.get('completed_at'))
                )
            conn.commit()
            conn.close()
            return redirect(url_for('admin_dashboard'))
    return render_template('restore.html', error=error)


init_db()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=True)
