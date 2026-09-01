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
#   未設定（預設）      → 方案 A：填答者填完（回 /complete 或 SurveyCake Webhook）才計數
COUNT_ON_ASSIGN = os.environ.get('COUNT_ON_ASSIGN', '') == '1'

# SurveyCake Webhook（專業版）：填答者送出後 SurveyCake POST 通知本服務。
# 每份問卷各有一組 Hash key / IV key，由後台 Webhook 設定頁取得，設為環境變數（勿寫進程式）。
# SURVEYCAKE_KEYS 為 JSON 陣列，例如：
#   [{"hash":"...","iv":"..."}, {"hash":"...","iv":"..."}]
# 解密時逐組嘗試，哪組解得出合法 JSON 就用哪組（不需預先對應 svid）。
def _load_surveycake_keys():
    raw = os.environ.get('SURVEYCAKE_KEYS', '')
    if raw:
        try:
            return [(k['hash'], k['iv']) for k in json.loads(raw)]
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    # 向後相容：單組金鑰
    h, i = os.environ.get('SURVEYCAKE_HASH_KEY', ''), os.environ.get('SURVEYCAKE_IV_KEY', '')
    return [(h, i)] if h and i else []


SURVEYCAKE_KEYS = _load_surveycake_keys()
SURVEYCAKE_DOMAIN = os.environ.get('SURVEYCAKE_DOMAIN', 'https://www.surveycake.com')
TOKEN_ALIAS = os.environ.get('TOKEN_ALIAS', 'token')  # 隱藏題的代號
# SurveyCake 以「aka_<代號>」作為帶入預設值的網址參數名稱，故發連結時用 aka_token
TOKEN_PARAM = os.environ.get('TOKEN_PARAM', f'aka_{TOKEN_ALIAS}')

# 初次建庫時匯入的預設問卷（之後皆由 /admin 管理）
DEFAULT_SURVEYS = [
    ("問卷一", "https://www.surveycake.com/s/0A0n3", 500),
    ("問卷二", "https://www.surveycake.com/s/L78kR", 500),
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
    """回傳所有問卷、完成數(completed)與已分配數(assigned=pending+completed)"""
    return c.execute("""
        SELECT s.id, s.name, s.url, s.max_count, s.is_active,
               (SELECT COUNT(*) FROM responses r
                WHERE r.survey_id = s.id AND r.status = 'completed') AS completed,
               (SELECT COUNT(*) FROM responses r
                WHERE r.survey_id = s.id AND r.status IN ('pending','completed')) AS assigned
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
                return redirect(f"{row['url']}?{TOKEN_PARAM}={existing_token}")
            # expired：往下重新分配

    # 平均分配：在啟用且未滿額的問卷中，挑「已分配數」最少的一份。
    # 用 assigned（pending+completed）而非 completed，避免完成回報有時間差時
    # 尖峰流量全部灌到同一份；tie-break 用 completed、id 保持穩定。
    candidates = [s for s in survey_stats(c)
                  if s['is_active'] and s['completed'] < s['max_count']]
    if not candidates:
        conn.commit()
        conn.close()
        return "問卷已全部收滿，謝謝參與！"
    selected = min(candidates, key=lambda s: (s['assigned'], s['completed'], s['id']))

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

    resp = make_response(redirect(f"{selected['url']}?{TOKEN_PARAM}={token}"))
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


def mark_completed(token):
    """把 pending 的 token 原子標記為 completed；回傳是否本次成功更新"""
    conn = get_db_connection()
    c = conn.cursor()
    expire_stale_tokens(c)
    c.execute(
        "UPDATE responses SET status = 'completed', completed_at = ? "
        "WHERE token = ? AND status = 'pending'",
        (datetime.now().isoformat(timespec='seconds'), token)
    )
    updated = c.rowcount
    conn.commit()
    conn.close()
    return updated > 0


def decrypt_surveycake(encrypted_b64):
    """AES-128-CBC 解密 SurveyCake Query API 回傳的 base64 內容。
    逐組金鑰嘗試，回傳第一組能解出、且含 result 的 JSON。"""
    import base64
    from Crypto.Cipher import AES

    cipher_bytes = base64.b64decode(encrypted_b64)
    for hash_key, iv_key in SURVEYCAKE_KEYS:
        try:
            raw = AES.new(hash_key.encode('utf-8'), AES.MODE_CBC,
                          iv_key.encode('utf-8')).decrypt(cipher_bytes)
            text = raw.decode('utf-8', errors='ignore')
            end = text.rfind('}')  # 去掉尾端補位雜訊，截到最後一個 '}'
            if end == -1:
                continue
            payload = json.loads(text[:end + 1])
            if isinstance(payload, dict) and 'result' in payload:
                return payload
        except (ValueError, json.JSONDecodeError):
            continue
    return None


def extract_token(payload):
    """從解密後的填答 JSON 取出代號為 TOKEN_ALIAS 的隱藏題答案"""
    target = TOKEN_ALIAS.strip().lower()
    for item in payload.get('result', []):
        keys = [str(item.get(k, '')).strip().lower()
                for k in ('alias', 'label', 'subject')]
        if target in keys:
            ans = item.get('answer') or []
            if ans and str(ans[0]).strip():
                return str(ans[0]).strip()
    return None


@app.route('/webhook', methods=['POST'])
def webhook():
    """SurveyCake 送出後的伺服器對伺服器通知（比結束頁導向更可靠）"""
    import requests

    svid = request.form.get('svid') or request.args.get('svid')
    hash_ = request.form.get('hash') or request.args.get('hash')
    if not svid or not hash_:
        return "missing svid/hash", 400
    if not SURVEYCAKE_KEYS:
        return "webhook keys not configured", 500

    api = f"{SURVEYCAKE_DOMAIN}/webhook/v0/{svid}/{hash_}"
    try:
        resp = requests.get(api, timeout=10)
        resp.raise_for_status()
        payload = decrypt_surveycake(resp.content)
    except Exception as e:
        app.logger.warning("webhook decrypt failed: %s", e)
        return "decrypt failed", 400

    if not payload:
        return "empty payload", 400
    token = extract_token(payload)
    if not token:
        # 收到通知但找不到 token（隱藏題代號設定有誤），仍回 200 讓 SurveyCake 不重送。
        # 印出各題的 alias/label/subject/answer 以便診斷實際欄位名稱。
        struct = [{"sn": it.get("sn"), "alias": it.get("alias"),
                   "label": it.get("label"), "subject": it.get("subject"),
                   "answer": it.get("answer")}
                  for it in payload.get('result', [])]
        app.logger.warning("webhook: token not found svid=%s TOKEN_ALIAS=%r result=%s",
                           svid, TOKEN_ALIAS, json.dumps(struct, ensure_ascii=False))
        return "token not found", 200

    counted = mark_completed(token)  # 重複／已過期都安全：只有 pending 會被計數
    app.logger.warning("webhook: token FOUND svid=%s token=%s counted=%s",
                       svid, token, counted)
    return "ok", 200


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
