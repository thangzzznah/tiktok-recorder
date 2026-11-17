# TikTok Live Recorder chạy trên GitHub (miễn phí, không cài gì)

Toàn bộ thao tác làm trên trình duyệt web.

## Cách hoạt động
- GitHub Actions chạy `recorder.py` liên tục (mỗi lượt ~5h45m, lượt sau tự nối tiếp).
- Mỗi 60 giây kiểm tra các tiktoker trong `tiktokers.txt`; ai live thì record, xong tự upload Drive.
- Thư mục Drive: `TikTok Lives / @username / YYYY-MM / YYYY-MM-DD_HH-MM-SS_@username.mp4` (giờ Việt Nam).

---

## BƯỚC 1: Tạo repo GitHub
1. Đăng ký/đăng nhập https://github.com
2. Bấm **New repository** → đặt tên (vd `tiktok-recorder`) → chọn **Public** → **Create repository**.
3. Bấm **uploading an existing file**, kéo thả cả thư mục dự án này vào (gồm `recorder.py`, `requirements.txt`, `tiktokers.txt`, `README.md` và thư mục `.github`) → **Commit changes**.
4. Nếu thư mục `.github` không lên được: **Add file → Create new file**, gõ tên `.github/workflows/record.yml` (gõ dấu `/` sẽ tự tạo thư mục), dán nội dung file `record.yml` vào, rồi Commit.

## BƯỚC 2: Lấy quyền truy cập Google Drive (làm 1 lần)
**2.1 Tạo project và bật API**
1. Vào https://console.cloud.google.com → tạo **New Project**.
2. Menu **APIs & Services → Library** → tìm **Google Drive API** → **Enable**.

**2.2 Màn hình đồng ý (consent screen)**
1. **APIs & Services → OAuth consent screen** (hoặc "Google Auth platform") → chọn **External** → điền tên app + email của bạn.
2. Thêm email của bạn vào **Test users**.
3. **Quan trọng:** bấm **Publish app** (chuyển sang "In production"). Nếu để chế độ Testing, token sẽ hết hạn sau 7 ngày và hệ thống ngừng upload.

**2.3 Tạo OAuth client**
1. **Credentials → Create Credentials → OAuth client ID**.
2. Loại: **Web application**.
3. Ở **Authorized redirect URIs** thêm đúng: `https://developers.google.com/oauthplayground`
4. Create → copy **Client ID** và **Client secret**.

**2.4 Lấy Refresh Token**
1. Vào https://developers.google.com/oauthplayground
2. Bấm biểu tượng bánh răng ⚙ (góc phải) → tick **Use your own OAuth credentials** → dán Client ID và Client secret.
3. Ở khung bên trái, ô nhập "Input your own scopes" dán: `https://www.googleapis.com/auth/drive.file` → bấm **Authorize APIs**.
4. Đăng nhập Google. Nếu thấy cảnh báo "Google hasn't verified this app" → **Advanced → Go to ... (unsafe)** → cho phép.
5. Bấm **Exchange authorization code for tokens** → copy giá trị **Refresh token**.

## BƯỚC 3: Thêm Secrets vào GitHub
Trong repo: **Settings → Secrets and variables → Actions → New repository secret**, tạo 3 secret (đúng tên):

| Tên | Giá trị |
|---|---|
| `GDRIVE_CLIENT_ID` | Client ID |
| `GDRIVE_CLIENT_SECRET` | Client secret |
| `GDRIVE_REFRESH_TOKEN` | Refresh token |

Tuỳ chọn: `TIKTOK_COOKIES` = nội dung file cookies.txt (định dạng Netscape) nếu cần record live 18+ hoặc live bị giới hạn khu vực / bị chặn.

## BƯỚC 4: Thêm tiktoker và bật chạy
1. Mở file `tiktokers.txt` → bấm biểu tượng bút chì để sửa. Mỗi dòng 1 người (link hoặc `@tên`):
   ```
   @tiktoker_a
   https://www.tiktok.com/@tiktoker_b/live
   ```
   → **Commit changes**.
2. Tab **Actions** → nếu có nút xanh "I understand my workflows, go ahead and enable them" thì bấm.
3. Chọn workflow **TikTok Live Recorder** → **Run workflow** để chạy ngay. Từ đó nó tự chạy liên tục.
4. Xem hoạt động: Actions → bấm vào lượt đang chạy → mở bước **Theo dõi và record** để xem log trực tiếp.

Thêm/bớt tiktoker sau này: chỉ cần sửa `tiktokers.txt`, tự áp dụng trong vài phút, không cần chạy lại.

---

## Giới hạn cần biết
- **Gián đoạn ~vài phút mỗi 6 giờ** lúc chuyển lượt chạy; live đang record sẽ được cắt thành 2 file.
- **Có thể trễ phút đầu** khi tiktoker bắt đầu live (kiểm tra mỗi 60 giây).
- **Dung lượng ổ runner ~14GB**: nếu nhiều người live dài cùng lúc có thể đầy ổ, hệ thống tự bỏ qua người mới khi còn dưới 2GB.
- **Drive miễn phí chỉ có 15GB**: live dài rất nặng (1 giờ ≈ 1-3GB). Hãy dọn thường xuyên hoặc nâng dung lượng.
- **Repo public nên danh sách tiktoker và log đều công khai**; mật khẩu/token nằm trong Secrets nên không lộ.
- **Repo không có hoạt động trong 60 ngày thì GitHub tắt lịch chạy tự động**: vào Actions bật lại, hoặc sửa `tiktokers.txt` là chạy lại.
- **TikTok có thể chặn IP của GitHub**: nếu log báo không record được dù đang live, thử thêm `TIKTOK_COOKIES`. Nếu vẫn không được thì cần VPS.
- Đây là cách dùng Actions hơi ngoài mục đích thông thường, GitHub có thể giới hạn nếu bị coi là lạm dụng.
- Chỉ record nội dung bạn được phép lưu và sử dụng.

## Sự cố thường gặp
| Triệu chứng | Cách xử lý |
|---|---|
| Log báo `Thiếu secret ...` | Kiểm tra tên 3 secret ở Bước 3 |
| `invalid_grant` | Chưa Publish app (Bước 2.2) hoặc refresh token cũ: lấy lại token ở Bước 2.4 |
| Không record dù đang live | Thử lại sau; thêm `TIKTOK_COOKIES`; yt-dlp được cập nhật mỗi lượt chạy |
| Drive không thấy thư mục cũ | Quyền `drive.file` chỉ thấy thư mục do chính hệ thống tạo; đừng tự tạo thư mục `TikTok Lives` bằng tay |
