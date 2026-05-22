# 自製 Speak 替代方案 — 設計文件

用 Gemini app 的 Live 語音對話 + Claude Code/Claude API/Gemini API，複刻 Speak 的核心學習價值，且完全客製。

-----

## 為什麼做這個

**長期目標**：去美國生活。這套系統的存在是為了支撐這個目標，所有取捨以此為準。

**中期里程碑**：考 TOEFL。但不是 day-1 目標 —— 系統先把基礎錯清乾淨（預估 2-3 個月）、流暢度穩了之後再加 TOEFL-specific 任務練習。日常使用不限於應試 mode，閒聊也行。

### 降低摩擦力

日常 AI 全包（生 role-play、生 drill 卡、選題、批改、撈錯題候選），使用者只負責「對話 + 錄音送分析 + 翻 drill + 看分析結果並逐條勾選 errors 的加/刪」。勾選刻意維持低摩擦 —— Opus 撈好候選、使用者打勾即可。修教材、調 prompt、調工作流都不在日常做，集中到月度 audit 一次處理。

Speak 本身就是被動模式 —— 系統給什麼你練什麼，這套照辦。日常摩擦力消耗意志、誘惑使用者每天微調系統反而失焦。

-----

## 系統運作方式

### 核心洞察

**白嫖 Gemini app Live**：免費無限用，不用接 Realtime API、不用付 OpenAI 錢。剩下只要解決「對話完怎麼複習」：自己錄音 → Gemini API 吃音檔 → 一次吐 transcript + errors + fluency notes。

**Role-play + Drill 兩腿走路**：role-play 抓新錯（覆蓋廣，scripted 翻譯逼出中階 vocab）、drill 強化舊錯（覆蓋深、無錄音門檻）。任一條腿單獨都不夠 —— 純 role-play 一次只 surface 3-5 條 errors，舊錯要靠場景自然帶到才會再現、很稀疏；純 drill 又練不到即時翻譯產出。

### 兩種練習模式

| | Role-play | Drill |
|---|---|---|
| 工具 | Gemini app Live | Web app（2.0）/ md 檔案自唸（1.0） |
| 時間 | ~3-5 分鐘 | ~10 分鐘（兩遍） |
| 錄音 | 要 | 不要 |
| 後續分析 | Gemini API 抓 transcript + errors | 無 |
| 格式 | scripted bilingual dialogue（AI 英文 + 你中文即時翻譯） | 中翻英 / 填空 recall |
| 目的 | 練中文意圖→英文即時翻譯 + 抓**新**錯 | 強化 errors.md **已知**錯 + 通勤可練 |
| 摩擦力 | 中（要錄音設備） | 極低（手機點開就能練） |

**Drill 結構：** 每天 10 張卡，混合 errors.md 已知錯與最近 sessions/ 的延伸學習，兩種子題型混搭（中翻英 + 填空 recall）。Cold start 階段不啟用，先用 role-play 累積資料。

詳細生成準則（題型選哪種、動態生成原則、檔案格式）見 `prompts/drill-generation.md`。

### 錯題池規則

同類合一、畢業就刪不留 archive。每次 session 後使用者自己判斷加哪些新錯、畢業哪些舊錯（不靠計數器或門檻）。完整整理準則見 `prompts/errors-generation.md`。

-----

## 1.0 — Claude Code (Opus)

純 markdown 檔案 + Opus 當 orchestrator + 一支簡短的 Python 腳本呼叫 Gemini API 做音檔批改。除了那支腳本以外不寫任何程式。

用 Claude Code 是為了在 loop 裡跟 Opus 磨合工作流，等判斷穩定再蒸餾成 2.0。訂閱 flat rate 順便涵蓋 Opus，不另收 token 費。

### 每日循環

1. **想練什麼？** 跟 Opus 說「今天想聊 X」或「隨機挑情境」。Opus 看 `data/errors.md` + `data/roleplays/index.md` 生成 role-play 並 append 索引。
2. **跟 Gemini Live 對話。** 打開 Gemini app 貼開場 prompt、另一台裝置開錄音，照 scripted dialogue 跑完（5-7 exchange, 3-5 分鐘）。
3. **丟給 Gemini API 分析。** `python analyze.py` → 互動選單選音檔 → 寫入 `data/sessions/YYYY-MM-DD-主題.json`，音檔自動刪。
4. **叫 Opus 整理。** 「看 data/sessions/ 最新一筆，更新 data/errors.md」—— 加 / 刪兩步：Opus 列出 Gemini 抓到的新錯候選讓你勾選收進、掃 transcript 列出這次講對的舊錯讓你勾選畢業。詳細見 `prompts/errors-generation.md`。

### Drill（隨時可用）

跟 Opus 說「生今天的 drill」→ 寫成 `data/drills/YYYY-MM-DD.md` 並 append 索引行。打開 md 自己照著唸，答案用 `<details>` 折疊。完整生成 spec 見 `prompts/drill-generation.md`。


### 檔案結構

```
free2speak/
├── CLAUDE.md              # 給 Opus 看的總指令
├── README.md              # 設計文件（本檔）
├── analyze.py             # 呼叫 Gemini API 做音檔分析（唯一的程式碼）
├── .gitignore             # 擋 audio / data/ / __pycache__
├── prompts/
│   ├── gemini-analysis.md       # analyze.py 讀取的音檔分析 prompt
│   ├── roleplay-generation.md   # role-play 生成 spec
│   ├── drill-generation.md      # drill 生成 spec
│   └── errors-generation.md     # errors.md 整理 spec
└── data/                  # 全部使用者資料（gitignored、靠 rsync / R2 備份）
    ├── errors.md          # 錯題本（active only）
    ├── sessions/          # 每次練習的 Gemini 分析結果
    │   └── YYYY-MM-DD-主題.json
    ├── roleplays/         # Opus 生成的每日 role-play
    │   ├── index.md       # one-line 索引（避免爬資料夾）
    │   └── YYYY-MM-DD-主題.md
    └── drills/            # Opus 生成的 drill 卡（中翻英 / 填空 recall）
        ├── index.md       # one-line 索引
        └── YYYY-MM-DD.md
```

`GEMINI_API_KEY` 設在 `~/.zshrc`（`export GEMINI_API_KEY=...`），不用 `.env`。

-----

## 2.0 — Web app + Opus API

當 1.0 工作流穩定後，把整理層搬成 API call + web 前端。整理層仍用 Opus（差別不在「Opus vs Sonnet」而在「Claude Code 互動式 vs API 程式化呼叫」），品質一致。

2.0 不是強制路徑 —— 1.0 跑得順、不在意 mobile access 的話，永遠停在 1.0 也合理。動手時機：跟 Opus 確認「工作流穩了，可以蒸餾成 2.0」。他是 1.0 的設計者跟執行者，最知道判斷邏輯有沒有穩定到能模板化。

### 架構

三個 node：

- **使用者裝置（手機 / 任何裝置）** —— web app（mobile-friendly），4 頁：今日 role-play、上傳音檔、當日復盤、Drill
- **PC（常駐 Docker container）** —— Python 後端：Opus API 做 role-play 生成 / drill 卡生成 / 錯題整理；Gemini API 做音檔批改。錯題整理是半自動 —— API 產出加/刪候選，使用者在當日復盤頁勾選確認後才寫入 `data/errors.md`。Bind mount `<host>/free2speak/data ↔ /app/data`（內含 `errors.md` + `sessions/` + `roleplays/` + `drills/`）
- **筆電（月度 audit + 備份目的地）** —— Claude Code Opus 讀 rsync 同步過來的 md

連線：使用者裝置 ⇄ PC（web）；PC → 筆電（既有 cron rsync）。

**Bind mount**：container 寫的 md 檔直接落在 host filesystem 上，既有的 cron rsync job 看得到、能正常備份；container 重啟也不丟資料。月度 audit 時筆電從 rsync 同步來的目錄打開 Claude Code 即可。

**蒸餾方法**：`prompts/` 底下三套模板（roleplay / drill / errors-generation）就是起點 —— 1.0 累積的 `roleplays/`、`drills/`、`sessions/` 提供真實使用 context，Opus refine 模板讓它們適合 API 程式化呼叫即可，不用從零寫。

-----

## 規格參考

### 常見主題參考

下面七種是常見起點，不是分類學 —— role-play 命名不必歸進這幾類。沒想練特定主題時 Opus 從中挑一個或自然延伸（DMV / 跟房東談 deposit / 處理 health insurance / 跟鄰居談停車等真實搬遷後場景都可以加進去）：

1. **restaurant** — 餐廳點餐 / 訂位
2. **travel** — 機場 / 旅館 check-in
3. **meeting** — 工作會議 / 簡報
4. **interview** — 面試 / 自我介紹
5. **smalltalk** — 日常閒聊（天氣、週末、興趣）
6. **complaint** — 客訴 / 退換貨
7. **social** — 社交破冰 / 約會

選題考慮：(a) 你最近生活 context、(b) errors 想練的 pattern（不硬湊）。register 不設限、跟著場景走（peer / 服務 / 職場 / 對長輩都練），細節見 `prompts/roleplay-generation.md` §2。

### Role-play 格式：scripted bilingual dialogue

格式細節、設計演進、範例見 `prompts/roleplay-generation.md`。

### errors.md 格式

每條欄位 spec、重新犯時的處理規則、範例條目見 `prompts/errors-generation.md`。

### Gemini 分析 prompt

固定模板見 `prompts/gemini-analysis.md` —— 給 Gemini 錄音 + prompt，吐 JSON（transcript / summary / errors / fluency_notes）。

-----

## 運營

### 月度 Opus audit

每月一次叫 Opus 在 Claude Code 做以下兩件事：

1. **生成模板審閱**：審 `prompts/` 三套模板 —— 必要時 refine、檢查 role-play 跟 drill 出題有沒有套路化、抓 meta-level 盲點（從沒練過的情境）。具體怎麼審讓 Opus 自己決定。
2. **長尾 errors 清理**：掃 `data/errors.md`，對比近期 `data/sessions/`，找出「長期沒出現在任何 transcript」的條目，逐條列給使用者 —— 是否已內化、場景本來就少見、還是需要繼續留著，使用者自己判斷要不要畢業。

errors 的日常加/刪流程不在 audit 範圍，那是每次 session 後處理的事。

### 成本對照

- **1.0**：Claude Code 訂閱已付，Opus 互動不另收費；Gemini API 音檔批改 $0（免費 tier）→ 近乎免費
- **2.0（Opus 4.7）**：上述 + 整理層 Opus API ~$6–7/mo → 一年 ~$78
- **2.0（Sonnet 4.6）**：~$4/mo → 一年 ~$48（僅供參考）
- **Speak**：NT$3,490–5,990/年 ≈ USD $107–184

1.0 近乎免費。2.0 遠比 Speak Plus 便宜，且完全客製、資料永遠在自己手上、可以隨意延伸新題型 / 新 prompt。2.0 整理層預設用 Opus 4.7；Sonnet 成本僅供參考。估算基於實測檔案大小 + 1.5× buffer + 列表價（Opus $5/$25、Sonnet $3/$15 per Mtok in/out），drill 載入最近 5 筆 sessions 是主要成本來源。

### 待辦：2.0 動工前還要決定的事

- Web app 4 頁的 UI 細節（卡片樣式、配色、動畫）
- Drill 卡要不要做「上一張 / 跳過」按鈕，還是只能線性走完
- rsync 跟 Claude Code audit 的目錄路徑安排（哪台是 source of truth、衝突怎麼處理）
