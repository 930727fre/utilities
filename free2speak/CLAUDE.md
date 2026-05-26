# CLAUDE.md

你是這個英文口說自學系統的開發者與維運者。設計總覽、頁面、資料模型在 `README.md`。

## 架構速覽

- `backend/` — FastAPI + SQLite，端點：stats GET、roleplay GET、upload POST、review GET（回傳 `{ additions, graduations }` 一次拿齊，避免步驟切換時 loading flash）、additions POST、graduations POST、drill GET
- `src/` — React + Vite + Mantine + TS，3 routes（`/` Dashboard、`/practice` Practice、`/drill` Drill）
- `nginx/` — 多階段 Docker build：node 編譯 → nginx 服務 + 反向代理 `/api/`
- `data/` — SQLite DB（`free2speak.db`），bind-mount，nightly 備份到 R2

## 操作原則

- **所有資料改動透過 API 或 `debug.py`，不要直接寫 SQL UPDATE**——避免 round-trip ambiguity
- 月度 audit 時用 `export.py` 產生暫時的 md 樹來瀏覽，*不要*編輯 export 出來的 md
- prompt template 改動 = 修改 `backend/prompts/*.py` + 重啟 container
- LLM 整合已上線：Gemini 處理錄音分析（`/upload`），Opus 處理 roleplay/drill 生成。`GEMINI_API_KEY` 與 `ANTHROPIC_API_KEY` 必須先在 host shell `export` 起來，compose parse 時若缺會直接 fail

## 設計語言

跟 `utilities/README.md` 的 design language section 一致：dark surfaces、cream text (`#e8e3d9`)、honey accent (`#c79968`) 只用在當前頁面唯一的 primary action。
