"""
TikTok Live Auto Recorder -> Google Drive
- Web UI: thêm/xóa/bật/tắt tiktoker (nhập link hoặc @username)
- Monitor: kiểm tra live định kỳ, tự record bằng yt-dlp
- Upload: record xong tự upload lên Drive theo thư mục:
    TikTok Lives / @username / YYYY-MM / YYYY-MM-DD_HH-MM-SS_@username.mp4
"""
import glob
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime

from flask import Flask, redirect, render_template_string, request
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ================= CẤU HÌNH =================
CHECK_INTERVAL = 45            # giây giữa các lần kiểm tra live
RECORD_DIR = "recordings"      # thư mục tạm lưu file
DB_PATH = "data.db"
DRIVE_ROOT_NAME = "TikTok Lives"
DELETE_AFTER_UPLOAD = True     # xóa file local sau khi upload xong
MIN_FILE_MB = 2                # file nhỏ hơn mức này coi như lỗi, bỏ qua
COOKIES_FILE = "cookies.txt"   # (tuỳ chọn) cookie TikTok, cần cho live 18+/bị giới hạn
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# ============================================

app = Flask(__name__)
os.makedirs(RECORD_DIR, exist_ok=True)
db_lock = threading.Lock()
recording_now = {}             # username -> Popen
upload_q = queue.Queue()
folder_cache = {}


# ---------------- DB ----------------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db_lock, db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS tiktokers(
            id INTEGER PRIMARY KEY, username TEXT UNIQUE, url TEXT,
            enabled INTEGER DEFAULT 1, created TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS recordings(
            id INTEGER PRIMARY KEY, username TEXT, path TEXT, started TEXT,
            status TEXT, drive_link TEXT)""")


def parse_username(text):
    m = re.search(r"tiktok\.com/@([\w.\-]+)", text) or re.match(r"@?([\w.\-]+)$", text.strip())
    return m.group(1).lower() if m else None


# ---------------- Recorder ----------------
def ytdlp_base(username):
    cmd = ["yt-dlp", "--no-warnings", "--no-part"]
    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    return cmd + [f"https://www.tiktok.com/@{username}/live"]


def is_live(username):
    r = subprocess.run(ytdlp_base(username) + ["--simulate", "-q"],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def record(username):
    ts = datetime.now()
    base = f"{ts:%Y-%m-%d_%H-%M-%S}_@{username}"
    raw = os.path.join(RECORD_DIR, base + ".%(ext)s")
    with db_lock, db() as c:
        rid = c.execute("INSERT INTO recordings(username,path,started,status) VALUES(?,?,?,?)",
                        (username, "", ts.isoformat(), "recording")).lastrowid
    p = subprocess.Popen(ytdlp_base(username) + ["-o", raw],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    recording_now[username] = p
    p.wait()
    recording_now.pop(username, None)

    files = glob.glob(os.path.join(RECORD_DIR, base + ".*"))
    if not files:
        _set_status(rid, "failed")
        return
    src = files[0]
    final = os.path.join(RECORD_DIR, base + ".mp4")
    if src != final:  # remux sang mp4 (không re-encode)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-c", "copy",
                            "-movflags", "+faststart", final])
        if r.returncode == 0:
            os.remove(src)
        else:
            final = src
    if os.path.getsize(final) < MIN_FILE_MB * 1024 * 1024:
        os.remove(final)
        _set_status(rid, "too_short")
        return
    with db_lock, db() as c:
        c.execute("UPDATE recordings SET path=?, status='queued' WHERE id=?", (final, rid))
    upload_q.put((rid, username, final, ts))


def _set_status(rid, status, link=None):
    with db_lock, db() as c:
        c.execute("UPDATE recordings SET status=?, drive_link=COALESCE(?,drive_link) WHERE id=?",
                  (status, link, rid))


def monitor_loop():
    while True:
        with db_lock, db() as c:
            users = [r["username"] for r in c.execute("SELECT username FROM tiktokers WHERE enabled=1")]
        for u in users:
            if u in recording_now:
                continue
            try:
                if is_live(u):
                    threading.Thread(target=record, args=(u,), daemon=True).start()
                    time.sleep(2)
            except Exception as e:
                print(f"[check {u}] {e}")
        time.sleep(CHECK_INTERVAL)


# ---------------- Google Drive ----------------
def drive_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_folder(svc, name, parent=None):
    key = (name, parent)
    if key in folder_cache:
        return folder_cache[key]
    safe = name.replace("'", "\\'")
    q = f"name='{safe}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent:
        q += f" and '{parent}' in parents"
    res = svc.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
    if res:
        fid = res[0]["id"]
    else:
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            meta["parents"] = [parent]
        fid = svc.files().create(body=meta, fields="id").execute()["id"]
    folder_cache[key] = fid
    return fid


def upload_file(username, path, ts):
    svc = drive_service()
    root = get_folder(svc, DRIVE_ROOT_NAME)
    user_f = get_folder(svc, f"@{username}", root)
    month_f = get_folder(svc, f"{ts:%Y-%m}", user_f)
    media = MediaFileUpload(path, mimetype="video/mp4", resumable=True, chunksize=32 * 1024 * 1024)
    meta = {"name": os.path.basename(path), "parents": [month_f],
            "description": f"TikTok live @{username} - bắt đầu {ts:%Y-%m-%d %H:%M:%S}"}
    req = svc.files().create(body=meta, media_body=media, fields="id,webViewLink")
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["webViewLink"]


def upload_worker():
    while True:
        rid, username, path, ts = upload_q.get()
        for attempt in range(1, 6):
            try:
                _set_status(rid, "uploading")
                link = upload_file(username, path, ts)
                _set_status(rid, "done", link)
                if DELETE_AFTER_UPLOAD:
                    os.remove(path)
                break
            except Exception as e:
                print(f"[upload {path}] lần {attempt}: {e}")
                _set_status(rid, "upload_retry")
                time.sleep(30 * attempt)
        else:
            _set_status(rid, "upload_failed")


# ---------------- Web UI ----------------
PAGE = """
<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>TikTok Live Recorder</title>
<meta http-equiv=refresh content=15>
<style>
body{font-family:system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#222}
input{padding:8px;border:1px solid #bbb;border-radius:6px;min-width:260px}
button{padding:8px 14px;border:0;border-radius:6px;background:#111;color:#fff;cursor:pointer}
button.s{background:#e5e5e5;color:#111}
table{width:100%;border-collapse:collapse;margin:12px 0 28px}
td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left;font-size:14px}
.rec{color:#d00;font-weight:600}.ok{color:#080}
</style>
<h1>TikTok Live Recorder</h1>
<form method=post action=/add>
  <input name=text placeholder="Link tiktok hoặc @username" required>
  <button>Thêm</button>
</form>
<h3>Danh sách theo dõi</h3>
<table><tr><th>Tiktoker<th>Trạng thái<th>Tự động record<th></tr>
{% for t in tiktokers %}
<tr><td><a href="https://www.tiktok.com/@{{t.username}}" target=_blank>@{{t.username}}</a>
<td>{% if t.username in rec %}<span class=rec>● ĐANG RECORD</span>{% else %}chờ live{% endif %}
<td>{{ 'Bật' if t.enabled else 'Tắt' }}
<td><form method=post action=/toggle/{{t.id}} style=display:inline><button class=s>{{ 'Tắt' if t.enabled else 'Bật' }}</button></form>
<form method=post action=/delete/{{t.id}} style=display:inline onsubmit="return confirm('Xóa?')"><button class=s>Xóa</button></form>
</tr>{% endfor %}</table>
<h3>Lịch sử record (30 gần nhất)</h3>
<table><tr><th>Tiktoker<th>Bắt đầu<th>Trạng thái<th>Drive</tr>
{% for r in recs %}
<tr><td>@{{r.username}}<td>{{r.started[:19].replace('T',' ')}}<td>{{r.status}}
<td>{% if r.drive_link %}<a href="{{r.drive_link}}" target=_blank class=ok>Mở</a>{% endif %}</tr>
{% endfor %}</table>
"""


@app.route("/")
def index():
    with db_lock, db() as c:
        t = c.execute("SELECT * FROM tiktokers ORDER BY username").fetchall()
        r = c.execute("SELECT * FROM recordings ORDER BY id DESC LIMIT 30").fetchall()
    return render_template_string(PAGE, tiktokers=t, recs=r, rec=set(recording_now))


@app.post("/add")
def add():
    u = parse_username(request.form["text"])
    if u:
        with db_lock, db() as c:
            c.execute("INSERT OR IGNORE INTO tiktokers(username,url,created) VALUES(?,?,?)",
                      (u, f"https://www.tiktok.com/@{u}/live", datetime.now().isoformat()))
    return redirect("/")


@app.post("/toggle/<int:i>")
def toggle(i):
    with db_lock, db() as c:
        c.execute("UPDATE tiktokers SET enabled=1-enabled WHERE id=?", (i,))
    return redirect("/")


@app.post("/delete/<int:i>")
def delete(i):
    with db_lock, db() as c:
        c.execute("DELETE FROM tiktokers WHERE id=?", (i,))
    return redirect("/")


if __name__ == "__main__":
    init_db()
    drive_service()  # đăng nhập Google lần đầu (mở trình duyệt) trước khi chạy
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=upload_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
