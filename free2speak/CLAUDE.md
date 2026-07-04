# CLAUDE.md

你是這個英文口說自學系統的開發者與維運者。設計總覽、頁面、資料模型在 `README.md`。

## 架構速覽

- `backend/` — FastAPI + 檔案系統，端點：stats GET、roleplay GET（取目前 active 那筆，沒有就生新的）、upload POST（multipart：`file` + `mode='roleplay'|'freestyle'`）、practice/state GET（告訴 frontend 要 land 在哪一步，支援 resume）、review GET（只回傳尚未決定的 additions/graduations）、sessions/{id}/decide POST（per-card：`{candidate_id, action}`，action ∈ added/skipped/graduated/kept）、drill GET
- `src/` — React + Vite + Mantine + TS，3 routes（`/` Dashboard、`/practice` Practice、`/drill` Drill）
- `nginx/` — 多階段 Docker build：node 編譯 → nginx 服務 + 反向代理 `/api/`
- `data/` — 檔案系統即狀態，bind-mount，nightly 備份到 R2

## 檔案系統 layout

```
data/
├── errors/{active,graduated}/NNNN-<slug>.md   # front-matter (id, status, dates) + markdown body
├── roleplays/{active,done}/<date>-<topic>[-<hash>].md   # front-matter + full script
├── sessions/
│   ├── <sid32>.<ext>            # 原始音檔
│   ├── <sid32>.json             # metadata + raw_response
│   └── <sid32>.decisions.jsonl  # 每筆 swipe append 一行
├── drills/<YYYY-MM-DD>.json     # 每天一份，含 cards[]
└── legacy/                      # 1.0-archive 原始 md 樹，只做 audit / grep，runtime 不碰
```

## 操作原則

- **狀態全在檔案系統，刪檔即 reset，不做 migration 邏輯**。挑錯就 nuke 重跑。
- rollback 手段：直接 `rm` / `mv`。例：清掉某 session `rm data/sessions/<sid>.*`；roleplay 從 done 撿回 active：`mv data/roleplays/done/<file>.md data/roleplays/active/`
- **月度 audit** 直接 grep：`grep -r "reservation" data/errors/`；`cat data/roleplays/done/2026-05-*.md`。不需要 export 腳本
- prompt template 改動 = 修改 `backend/prompts/*.py` + 重啟 container
- LLM 整合已上線：Gemini 處理錄音分析（`/upload`），Opus 處理 roleplay/drill 生成。`GEMINI_API_KEY` 與 `ANTHROPIC_API_KEY` 必須先在 host shell `export` 起來，compose parse 時若缺會直接 fail
- **Roleplay lifecycle**：`roleplays/active/` 至多一個檔（filesystem-level 保證單一 active，不需 partial unique index）。`mode='roleplay'` 的 session 完成完整 review 時把 active 檔 `mv` 到 `done/`；`mode='freestyle'` 不消耗 active roleplay，所以 user 下次回來會繼續看到同一個劇本。每一筆 swipe decision 都會即時 append 到 `<sid>.decisions.jsonl`，所以中途關掉 tab 不會掉資料
- **Review 完成判定**：不存 `review_done` flag，用 decisions 檔跟 `raw_response.additions + graduations` 的 candidate 集合比對，全 covered = review done。狀態純推導，不會 drift。

## 設計語言

跟 `utilities/README.md` 的 design language section 一致：dark surfaces、cream text (`#e8e3d9`)、honey accent (`#c79968`) 只用在當前頁面唯一的 primary action。
