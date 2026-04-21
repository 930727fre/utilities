# yt-whisper — 專案規格書（定稿）

## TODO
- 下載與轉錄流程可正常運作，但中斷與恢復機制設計不完善。狀態管理（job status transitions、worker crash recovery、partial file cleanup）需要重新設計。

---

## 核心目標
建立一個自動化流水線，將 YouTube 影片下載為 MP4 並利用 GPU 加速轉錄字幕，提供網頁端進度監控、檔案管理與串流播放。

---

## 1. 技術棧

| 組件 | 工具 | 說明 |
| :--- | :--- | :--- |
| 容器化 | Docker & Docker Compose | 封裝環境，包含 CUDA 支援 |
| 後端 API | FastAPI | 處理 Web 請求，支援非同步操作 |
| 任務調度 | Celery + Redis | 排隊機制，確保 GPU 一次只處理一個任務 |
| 下載引擎 | yt-dlp | 確保輸出為 MP4 |
| AI 轉錄 | openai-whisper | CUDA 加速，RTX 3060 最佳化 |
| 前端 | HTML / JS / Tailwind CSS | 任務儀表板 + 播放器頁面（深色模式） |

---

## 2. 固定核心參數

```python
# yt-dlp
format              = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]"
merge_output_format = "mp4"

# openai-whisper
model_size    = "medium"
device        = "cuda"
beam_size     = 5
language      = None   # auto detect
```

---

## 3. 目錄結構

```
/yt-whisper
├── docker-compose.yml
├── Dockerfile
├── main.py            # FastAPI 主程式（API 路由 + 播放器頁面）
├── tasks.py           # Celery 任務（yt-dlp + Whisper）
├── storage.py         # jobs.json 讀寫（含 file lock）
├── requirements.txt
├── static/
│   └── index.html     # 任務監控儀表板
└── data/              # 所有資料（git ignored，volume 掛載）
    ├── jobs.json      # 任務狀態記錄
    ├── downloads/     # MP4 與 SRT 檔案
    └── models/        # Whisper 模型快取
```

---

## 4. Docker 架構

### 4.1 容器組成

```
docker-compose
├── redis     # 訊息佇列（不需 GPU）
├── web       # FastAPI，port 8000（不需 GPU）
└── worker    # Celery Worker，獨佔 GPU
```

### 4.2 Dockerfile

- 基礎映像：`nvidia/cuda:12.2.0-base-ubuntu22.04`
- 系統依賴：`ffmpeg`、`python3.11`
- `web` 與 `worker` 共用同一個 Dockerfile，以啟動指令區分

| 容器 | 啟動指令 |
| :--- | :--- |
| `web` | `uvicorn main:app --host 0.0.0.0 --port 8000` |
| `worker` | `celery -A tasks worker --loglevel=info --concurrency=1 -P solo` |

### 4.3 Volume 掛載

| 主機路徑 | 容器路徑 | 掛載容器 | 說明 |
| :--- | :--- | :--- | :--- |
| `./data` | `/app/data` | web、worker | jobs.json + MP4 + SRT |
| `./data/models` | `/root/.cache/whisper` | worker | Whisper 模型快取 |

### 4.4 GPU 設定

僅 `worker` 容器需要：

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

**主機前置條件：**
- NVIDIA Driver 525+
- nvidia-container-toolkit 已安裝
- `docker info` 可看到 `Runtimes: nvidia`

### 4.5 常用指令

```bash
# 初始化（首次 clone 後執行）
mkdir -p data/downloads
mkdir -p data/models

# 建置並啟動
docker compose up --build -d

# 查看 worker log
docker compose logs -f worker

# 停止（檔案保留）
docker compose down
```

---

## 5. 資料結構（jobs.json）

每筆任務記錄：

```json
{
  "job_id": "uuid4-string",
  "url": "https://youtube.com/watch?v=xxx",
  "title": "影片標題",
  "status": "PENDING | DOWNLOADING | TRANSCRIBING | SUCCESS | FAILED | DELETED",
  "progress": {
    "download_pct": 0.0,
    "download_speed": "2.3 MiB/s",
    "transcribe_pct": 0.0
  },
  "files": {
    "mp4": "downloads/xxx.mp4",
    "srt": "downloads/xxx.srt"
  },
  "error": null,
  "created_at": "2025-01-01T00:00:00Z",
  "updated_at": "2025-01-01T00:00:00Z"
}
```

**注意：** `web` 與 `worker` 同時掛載 `jobs.json`，所有寫入操作須加 file lock（`filelock` 套件）防止競爭。

---

## 6. API 端點

| Method | Path | 說明 | 回傳 |
| :--- | :--- | :--- | :--- |
| `GET` | `/` | 主儀表板頁面 | HTML |
| `POST` | `/api/jobs` | 提交新 URL | `{ job_id, status }` |
| `GET` | `/api/jobs` | 列出所有任務 | `[ job, ... ]` |
| `GET` | `/api/jobs/{id}` | 單一任務詳情 | job object |
| `DELETE` | `/api/jobs/{id}` | 刪除任務與檔案 | `{ ok: true }` |
| `GET` | `/player/{id}` | 播放器頁面（新分頁） | HTML |
| `GET` | `/api/stream/{id}/video` | MP4 串流（Range Request） | video/mp4 |
| `GET` | `/api/stream/{id}/subtitle` | SRT 即時轉 VTT 回傳 | text/vtt |

---

## 7. 任務狀態流轉

```
POST /api/jobs
    │
    ▼
PENDING（寫入 jobs.json，推入 Redis Queue）
    │
    ▼  Worker 認領
DOWNLOADING（每秒更新 download_pct / speed）
    │
    ▼  yt-dlp 完成
TRANSCRIBING（每段更新 transcribe_pct）
    │
    ▼  產生 .srt
SUCCESS
    │
    ▼  使用者點刪除
DELETED（revoke task → 刪檔 → 更新 JSON）
```

**FAILED 觸發條件：** yt-dlp 失敗、Whisper OOM、任何未捕獲 Exception。

---

## 8. 串流播放器

### 8.1 進入方式
- 主列表任務卡片，狀態 `SUCCESS` 時顯示「▶ 播放」按鈕
- 點擊後 `window.open('/player/{job_id}', '_blank')` 開新分頁

### 8.2 字幕處理
- 瀏覽器 `<track>` 只支援 VTT，不另存檔案
- `/api/stream/{id}/subtitle` 即時將 `.srt` 轉換為 VTT 回傳

SRT → VTT 轉換規則：
1. 首行加上 `WEBVTT`
2. 時間格式：`00:00:01,000` → `00:00:01.000`（逗號換句號）

### 8.3 播放器 HTML 結構

```html
<video controls autoplay>
  <source src="/api/stream/{id}/video" type="video/mp4">
  <track kind="subtitles" src="/api/stream/{id}/subtitle" default>
</video>
```

- `default`：字幕預設開啟
- `autoplay`：開新分頁後自動播放
- Fallback：若 autoplay 被瀏覽器攔截，顯示大型「▶ 點擊播放」按鈕

### 8.4 Range Request
FastAPI 需實作 `206 Partial Content` 支援 seek 功能。

---

## 9. 前端儀表板規格

**每筆任務卡片顯示：**
- 影片標題 + 原始 URL
- 狀態色標：灰（PENDING）/ 藍（DOWNLOADING）/ 黃（TRANSCRIBING）/ 綠（SUCCESS）/ 紅（FAILED）
- 下載進度條（含速度，僅 DOWNLOADING 顯示）
- 轉錄進度條（僅 TRANSCRIBING 顯示）
- 完成後：`.mp4` 下載連結、`.srt` 下載連結、「▶ 播放」按鈕
- 刪除按鈕（任何狀態皆可，點後二次確認）

**輪詢機制：** 每 2 秒呼叫 `GET /api/jobs` 更新畫面。

**外觀：** 全站深色模式（dark background，Tailwind `dark` class 或直接以深色為預設配色）。

---

## 10. 邊界情況處理

| 情境 | 處理方式 |
| :--- | :--- |
| 重複提交同一 URL | 允許，每次產生新 job_id |
| 刪除「PENDING」任務 | 標記 DELETED，Worker 認領前檢查狀態後放棄 |
| 刪除「TRANSCRIBING」任務 | `celery.control.revoke(task_id, terminate=True)` |
| Worker 重啟後狀態殘留 | 啟動時掃描 JSON，DOWNLOADING / TRANSCRIBING 重置為 FAILED |
| `jobs.json` 不存在 | 程式啟動時自動建立 `[]` |
| `/player/{id}` 不存在 | 回傳 404 頁面 |
| 任務存在但非 SUCCESS | 顯示「影片尚未就緒」 |
| `.srt` 遺失 | subtitle 端點回傳 404，影片仍可播放 |
| `.mp4` 遺失 | video 端點回傳 404，播放器顯示錯誤 |
| `jobs.json` 寫入競爭 | 所有寫入加 file lock |
| Whisper 模型首次下載 | 約 1.5GB，掛載 `./models` 避免重複下載 |