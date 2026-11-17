"""
TikTok Live Recorder chạy trên GitHub Actions.
- Đọc danh sách từ tiktokers.txt (tự cập nhật lại mỗi vòng, không cần khởi động lại)
- Kiểm tra live định kỳ, ai live thì record (nhiều người song song)
- Record xong tự upload Google Drive:
    TikTok Lives / @username / YYYY-MM / YYYY-MM-DD_HH-MM-SS_@username.mp4
- Chạy tối đa ~5h45m rồi tự dừng gọn, lượt kế tiếp của workflow sẽ chạy tiếp.
"""
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ============ CẤU HÌNH ============
TZ = timezone(timedelta(hours=7))                       # giờ Việt Nam
START = time.time()
STOP_NEW_AFTER = int(os.getenv("STOP_NEW_MIN", 335)) * 60   # sau mốc này không bắt đầu record mới
HARD_STOP_AFTER = int(os.getenv("HARD_STOP_MIN", 345)) * 60 # sau mốc này dừng record để kịp upload
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))       # giây giữa các lần kiểm tra live
MIN_FILE_MB = 2                                         # file nhỏ hơn mức này bị bỏ
MIN_FREE_GB = 2                                         # ổ còn dưới mức này thì không record thêm
RECORD_DIR = "recordings"
DRIVE_ROOT = "TikTok Lives"
LIST_FILE = "tiktokers.txt"
COOKIES_FILE = "cookies.txt"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# ==================================

os.makedirs(RECORD_DIR, exist_ok=True)
drive_lock = threading.Lock()


def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


# ---------- Danh sách tiktoker ----------
def parse_username(text):
    text = text.split("#")[0].strip()
    if not text:
        return None
    m = re.search(r"tiktok\.com/@([\w.\-]+)", text) or re.match(r"^@?([\w.\-]+)$", text)
    return m.group(1).lower() if m else None


def load_list():
    content = None
    repo = os.getenv("GITHUB_REPOSITORY")
    branch = os.getenv("GITHUB_REF_NAME", "main")
    if repo:  # lấy bản mới nhất từ GitHub để cập nhật nóng
        try:
            url = f"https://raw.githubusercontent.com/{repo}/{branch}/{LIST_FILE}?t={int(time.time())}"
            with urllib.request.urlopen(url, timeout=15) as r:
                content = r.read().decode("utf-8")
        except Exception as e:
            log(f"Không tải được danh sách mới, dùng bản local ({e})")
    if content is None and os.path.exists(LIST_FILE):
        content = open(LIST_FILE, encoding="utf-8").read()
    names = []
    for line in (content or "").splitlines():
        u = parse_username(line)
        if u and u not in names:
            names.append(u)
    return names


# ---------- Record ----------
def ytdlp_cmd(username):
    cmd = ["yt-dlp", "--no-warnings", "--no-part"]
    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    return cmd + [f"https://www.tiktok.com/@{username}/live"]


def is_live(username):
    try:
        r = subprocess.run(ytdlp_cmd(username) + ["--simulate", "-q"],
                           capture_output=True, text=True, timeout=90)
        return r.returncode == 0
    except Exception as e:
        log(f"[{username}] lỗi kiểm tra: {e}")
        return False


def record_and_upload(username):
    ts = datetime.now(TZ)
    base = f"{ts:%Y-%m-%d_%H-%M-%S}_@{username}"
    out_tpl = os.path.join(RECORD_DIR, base + ".%(ext)s")
    err_path = os.path.join(RECORD_DIR, base + ".log")
    log(f"▶ BẮT ĐẦU record @{username}")

    with open(err_path, "w") as errf:
        p = subprocess.Popen(ytdlp_cmd(username) + ["-o", out_tpl, "--hls-use-mpegts"],
                             stdout=subprocess.DEVNULL, stderr=errf)
        while p.poll() is None:
            if time.time() - START > HARD_STOP_AFTER:
                log(f"⏹ Hết thời gian lượt chạy, dừng @{username} để upload")
                p.send_signal(signal.SIGINT)
                try:
                    p.wait(60)
                except subprocess.TimeoutExpired:
                    p.kill()
                break
            time.sleep(5)

    files = [f for f in glob.glob(os.path.join(RECORD_DIR, base + ".*"))
             if not f.endswith((".log", ".ytdl", ".part"))]
    if not files:
        tail = open(err_path).read()[-500:]
        log(f"✗ @{username}: không có file. Log yt-dlp: {tail}")
        return
    src = max(files, key=os.path.getsize)

    # đóng gói lại thành mp4 (không nén lại)
    final = os.path.join(RECORD_DIR, base + ".mp4")
    tmp = os.path.join(RECORD_DIR, base + ".remux.mp4")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-c", "copy",
                        "-movflags", "+faststart", tmp])
    if r.returncode == 0 and os.path.exists(tmp):
        os.remove(src)
        os.replace(tmp, final)
    else:
        final = src
        if os.path.exists(tmp):
            os.remove(tmp)

    size_mb = os.path.getsize(final) / 1024 / 1024
    if size_mb < MIN_FILE_MB:
        log(f"✗ @{username}: file quá nhỏ ({size_mb:.1f}MB), bỏ qua")
        os.remove(final)
        return

    log(f"⏫ Upload {os.path.basename(final)} ({size_mb:.0f}MB)")
    for attempt in range(1, 6):
        try:
            link = upload_file(username, final, ts)
            log(f"✓ Xong @{username}: {link}")
            os.remove(final)
            return
        except Exception as e:
            log(f"✗ Upload lần {attempt} lỗi: {e}")
            time.sleep(20 * attempt)
    log(f"✗✗ Upload thất bại hoàn toàn, file {final} bị mất khi runner kết thúc")


# ---------- Google Drive ----------
def drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_folder(svc, name, parent=None):
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    q = f"name='{safe}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent:
        q += f" and '{parent}' in parents"
    found = svc.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
    if found:
        return found[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        meta["parents"] = [parent]
    return svc.files().create(body=meta, fields="id").execute()["id"]


def upload_file(username, path, ts):
    svc = drive_service()
    with drive_lock:  # tránh tạo trùng thư mục khi nhiều luồng cùng upload
        root = get_folder(svc, DRIVE_ROOT)
        user_f = get_folder(svc, f"@{username}", root)
        month_f = get_folder(svc, f"{ts:%Y-%m}", user_f)
    meta = {"name": os.path.basename(path), "parents": [month_f],
            "description": f"TikTok live @{username} - bắt đầu {ts:%Y-%m-%d %H:%M:%S} (GMT+7)"}
    media = MediaFileUpload(path, mimetype="video/mp4", resumable=True, chunksize=64 * 1024 * 1024)
    req = svc.files().create(body=meta, media_body=media, fields="id,webViewLink")
    resp = None
    while resp is None:
        _, resp = req.next_chunk(num_retries=5)
    return resp["webViewLink"]


# ---------- Vòng lặp chính ----------
def main():
    for k in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN"):
        if not os.getenv(k):
            sys.exit(f"Thiếu secret {k}. Xem README để thêm vào GitHub Secrets.")
    drive_service().files().list(pageSize=1).execute()  # kiểm tra đăng nhập Drive ngay từ đầu
    log("Đăng nhập Google Drive OK. Bắt đầu theo dõi.")

    active = {}
    while time.time() - START < STOP_NEW_AFTER:
        users = load_list()
        active = {u: t for u, t in active.items() if t.is_alive()}
        todo = [u for u in users if u not in active]
        if todo:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(is_live, todo))
            for u, live in zip(todo, results):
                if not live:
                    continue
                free_gb = shutil.disk_usage(RECORD_DIR).free / 1024 ** 3
                if free_gb < MIN_FREE_GB:
                    log(f"⚠ Ổ đĩa còn {free_gb:.1f}GB, bỏ qua @{u}")
                    continue
                t = threading.Thread(target=record_and_upload, args=(u,), daemon=False)
                t.start()
                active[u] = t
        log(f"Theo dõi {len(users)} người | đang record: {', '.join(active) or 'không ai'}")
        time.sleep(CHECK_INTERVAL)

    log("Hết thời gian lượt chạy, chờ các bản record đang dở hoàn tất...")
    for t in active.values():
        t.join()
    log("Kết thúc lượt chạy. Lượt kế tiếp sẽ tự tiếp tục.")


if __name__ == "__main__":
    main()
