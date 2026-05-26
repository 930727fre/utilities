# CLAUDE.md

你是這個英文口說自學系統的開發者與維運者。設計總覽、頁面、資料模型在 `README.md`。

## 架構速覽

- `backend/` — FastAPI + SQLite，端點：stats GET、roleplay GET（取目前 active 那筆，沒有就生新的）、upload POST（multipart：`file` + `mode='roleplay'|'freestyle'`）、practice/state GET（告訴 frontend 要 land 在哪一步，支援 resume）、review GET（只回傳尚未決定的 additions/graduations）、sessions/{id}/decide POST（per-card：`{candidate_id, action}`，action ∈ added/skipped/graduated/kept）、drill GET
- `src/` — React + Vite + Mantine + TS，3 routes（`/` Dashboard、`/practice` Practice、`/drill` Drill）
- `nginx/` — 多階段 Docker build：node 編譯 → nginx 服務 + 反向代理 `/api/`
- `data/` — SQLite DB（`free2speak.db`），bind-mount，nightly 備份到 R2

## 操作原則

- **所有資料改動透過 API 或 `debug.py`，不要直接寫 SQL UPDATE**——避免 round-trip ambiguity
- 月度 audit 時用 `export.py` 產生暫時的 md 樹來瀏覽，*不要*編輯 export 出來的 md
- prompt template 改動 = 修改 `backend/prompts/*.py` + 重啟 container
- LLM 整合已上線：Gemini 處理錄音分析（`/upload`），Opus 處理 roleplay/drill 生成。`GEMINI_API_KEY` 與 `ANTHROPIC_API_KEY` 必須先在 host shell `export` 起來，compose parse 時若缺會直接 fail
- **Roleplay lifecycle**: 同時只有一個 `roleplays.status='active'`（partial unique index 強制）。`mode='roleplay'` 的 session 完成完整 review 時把它翻成 `'done'`；`mode='freestyle'` 不消耗 active roleplay，所以 user 下次回來會繼續看到同一個劇本。每一筆 swipe decision 都會即時 POST 到 `/sessions/{id}/decide`，所以中途關掉 tab 不會掉資料

## 設計語言

跟 `utilities/README.md` 的 design language section 一致：dark surfaces、cream text (`#e8e3d9`)、honey accent (`#c79968`) 只用在當前頁面唯一的 primary action。
