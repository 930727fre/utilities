# CLAUDE.md

你是這個英文口說自學系統的開發者與維運者。設計總覽、頁面、資料模型在 `README.md`。

## 架構速覽

- `backend/` — FastAPI + SQLite，端點：roleplay GET、upload POST、review GET（回傳 `{ additions, graduations }` 一次拿齊，避免步驟切換時 loading flash）、additions POST、graduations POST、drill GET
- `src/` — React + Vite + Mantine + TS，2 routes（`/` Practice、`/drill` Drill）
- `nginx/` — 多階段 Docker build：node 編譯 → nginx 服務 + 反向代理 `/api/`
- `data/` — SQLite DB（`free2speak.db`），bind-mount，nightly 備份到 R2

## 操作原則

- **所有資料改動透過 API 或 `debug.py`，不要直接寫 SQL UPDATE**——避免 round-trip ambiguity
- 月度 audit 時用 `export.py` 產生暫時的 md 樹來瀏覽，*不要*編輯 export 出來的 md
- prompt template 改動 = 修改 `backend/prompts/*.py` + 重啟 container
- LLM 呼叫目前是 stub（mock JSON 回應）——真實 Opus / Gemini 接線時要傳 `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` 環境變數

## 設計語言

跟 `utilities/README.md` 的 design language section 一致：dark surfaces、cream text (`#e8e3d9`)、honey accent (`#c79968`) 只用在當前頁面唯一的 primary action。
