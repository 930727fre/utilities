# free2speak

我的英文口說自學系統。**Code 是 scaffolding — 真正的產品是 LLM/agent 對我英文 bottleneck 的精準診斷 + 每次 session 都逼出一個 contrastive noticing artifact**。

未來讀 code 前請先讀下面這一節。

## Agent 必讀：我的英文 profile

**One-line 診斷**：
你是 upper-intermediate 英文使用者，主要 bottleneck 是「中文查表 → 錯 register 英文」（Chinglish at register/collocation level），靠 daily free2speak session + 每天 30 分鐘 native podcast/影集 input + 對陌生 native chunk 建立信任 + freestyle 主動 recycle 剛學的 phrase 這四件事複利，12-24 個月能質變成 native texture。

**Chinglish path fingerprint**：
- 中文情境詞 → 查字典最直接對應的英文 → 該英文詞本身沒錯，但**用在錯的 register / context**
- 已知 pattern（左邊是中文情境，中間是我會查到的英文，右邊是該情境的 native 說法）：
  - 醫療「預約」→ `reservation`（其實 reservation 是餐廳/飯店/機票）→ 應 `appointment`
  - 日常客服「修改預約」→ `modification`（其實 modification 是公文/法律語體）→ 應 `change`
  - 白天時段「白天接不到電話」→ `on the day`（其實 on the day 指某特定日子）→ 應 `during the day`
  - 時間「跟會議撞到」→ `collide`（其實 collide 專指物理碰撞）→ 應 `conflict with`
  - 保險等回音「沒回饋」→ `feedback`（其實 feedback 是意見評分）→ 應 `hear back` / `no response`
  - 保險理賠「核准」→ `validated`（其實 validated 是驗證資料/停車票）→ 應 `approved` / `covered`
  - 系統上線事件「上線後」→ `after online`（其實 online 是狀態形容詞，不是事件名詞）→ 應 `after launch` / `after deployment`
- 特徵：文法對、詞認識、每個錯詞在**別的 context 都是對的英文**，只是這個場景該用別的詞

**每張 card 的品質底線**：一張 card 進 error book 前（不論 Sonnet 自動產的還是 discuss mode 討論後手動寫的）要符合這幾條，不符合就打回重寫或 skip：

1. `l1_diagnosis` 要具體指名「我這次講錯是因為中文 X 直譯成英文 Y」— 不是抽象規則描述、不是講英文用法而已。（例外：discuss mode 主動教的新 chunk、或道地度升級建議這兩種卡沒有真實的「講錯」，`l1_diagnosis` 改寫成「直覺會講成 X，但地道講法是 Y，因為...」，`you_said` 誠實留白或標註來源）
2. `native` 要日常慣用（`hear back`），不要 stilted 翻譯腔（`receive information`）
3. `register` 一個短 phrase 就好，不要 slash-list 排排站（`medical appointments` 對，`medical / customer / general / insurance` 錯）
4. 一張卡對應一條 rule，不能混（missing plural + uncountable-noun 誤用要拆兩張）

## Tool 幹嘛

我在 Gemini Live 演一段 roleplay → 錄音上傳 → Gemini 逐字稿 → Claude Sonnet 4.6 分析 → 我 swipe / 討論後進 error book → drill 每天 recall。

- **Gemini 2.5 Flash**：只做逐字稿（temp=0.0）
- **Claude Sonnet 4.6**：拿逐字稿做分析（temp=0.2）。stringify quirk 時 auto retry → Opus 4.7 fallback
- **Claude Opus 4.7**：生 roleplay 跟 drill

## 兩種 review mode

**discuss**（我目前的主力）：upload 時勾 `[✓] discuss with Claude` toggle。session 只存 transcript。之後我拿 session_id 找 Claude 討論 → `apply_review.py` 從 stdin JSON 寫進 error book。對每張卡的品質底線比較踏實，適合我對 auto pipeline 還沒完全信任的階段。

Review 流程（2026-09-18 演練驗證，取代舊的「先讀完整段列清單」做法）：**逐句對照**。Claude 吐一組 `[AI] ... [ME] ...`，兩人討論這句有沒有問題（可能零個、一個、或疊好幾個），討論完才進下一組。完全空洞的禮貌回應（"Okay, thank you" 這種沒有實質內容的句子）可以跳過不用停。用 2026-05-15 舊 session 演練過：這個方法比讀完整段再列清單抓得更全，包括抓出舊卡復發、以及同一句裡疊著的多個問題。

Review 紀律（2026-09-18 加，因為漏抓過東西）：
- 逐句進行時，先把整本 active error book 掃過一次找 recurrence，不能只靠印象比對——舊卡復發（sticky error）跟新錯誤一樣重要，常常被漏掉。
- 改寫某句話的 native 版本時，同一句話裡如果還有別的問題，要單獨列成另一個候選，不能順手修掉、不明講——每個問題都要讓我看見、自己判斷要不要收，不能被我的 rewrite 悄悄吃掉。

**auto**（fallback）：upload → Sonnet 自動分析 → 前端 tinder-swipe。快、不綁 Claude availability。目前用在 Claude 不在的時段、或想快速消化一場 session 但不追細節時。

> 2026-09-16 更新：實際用下來，discuss mode 的價值很大一部分來自即時判斷力（該不該收、是不是單純口誤、你當下的真實意圖是什麼），這種東西很難塞進固定 prompt。auto 有沒有可能永遠不會反轉成主力、甚至被放棄，先保留 fallback 角色觀察，不強求它進化。

## discuss mode 的延伸：道地度升級建議

不只抓錯誤，也可以抓「文法對、意思也對，但母語使用者有更常用講法」的句子——跟錯誤修正卡同格式（`you_said` + `native` + `l1_diagnosis`），差別是 `you_said` 不是錯的，`l1_diagnosis` 改寫成「這樣講沒錯，但更道地的講法是...」。

品質門檻：native 版本要「很明顯大多數母語使用者都會這樣講」，不是「其中一種更好的選項」——排除掉見仁見智的用字選擇。這條門檻本身就會自然限制數量，不特別設上限。目前只在 discuss mode 手動做，不放進 Sonnet 的 auto-analyze prompt。

## Daily flow

1. `/` Practice → 看 active roleplay → 按 `Copy Gemini prompt`
2. Gemini Live 貼 prompt 演完 → 錄音
3. Upload → auto or discuss
4. Swipe（或 ping Claude 討論後 apply_review）→ 進 error book
5. `/drill` 每天 recall 10 張卡

## Data layout

檔案系統即狀態，rm/mv 就是 rollback。

```
data/
├── errors/{active,graduated}/NNNN-<slug>.md
├── roleplays/{active,done}/<date>-<topic>*.md
├── sessions/<YYYY-MM-DDTHH-MM-SS-hash8>.{json,decisions.jsonl,ext}
└── drills/<YYYY-MM-DD>.json
```

`errors/` 跟 `roleplays/` 用 YAML front-matter + markdown body，可 vim 直接編。session JSON / drill JSON 是機器產物。

Audit trail：error card 前 front-matter 有 `source_session_id` + `source_candidate_id`，跳回 raw_response 原文用：

```bash
sess=$(grep '^source_session_id' 0056-*.md | awk '{print $2}')
cid=$(grep '^source_candidate_id' 0056-*.md | awk '{print $2}')
jq --arg cid "$cid" '.raw_response.additions[] | select(.id == $cid)' data/sessions/$sess.json
```

Manual origin 的卡（discuss mode）`source_candidate_id: null`。

## Deploy

```bash
export ANTHROPIC_API_KEY=...
export GEMINI_API_KEY=...
docker compose up -d --build
```

CF tunnel 指到 `free2speak-frontend:80`。

## 常用 rollback / edit

```bash
rm data/sessions/<sid>.*                              # 刪 session
mv data/errors/graduated/*.md data/errors/active/     # un-graduate
mv data/roleplays/done/<file>.md data/roleplays/active/  # 撿回 roleplay
rm data/drills/<YYYY-MM-DD>.json                       # 那天 drill 重生
vim data/errors/active/NNNN-*.md                      # 直接編卡

# 對某 session 重跑 Claude 分析（同 transcript, 不重錄）
docker exec free2speak-backend python /app/reanalyze.py <session_id>
```

## Scope 邊界

Code + prompt 會隨我英文程度跟需求一直改（這是 tool 的重點）。但這幾條 scope 邊界不動：

- **不加 Phase C**（transcribe library 做 input curation） — error book 到 150+ 再說。這是「還沒到」不是「不會做」
- **不擴 free2speak 成通用學習 app** — 通用維度 Duolingo/tutor/podcast/影集 各自解決，這個 tool 專攻 personalized L1 diagnosis
- **不 productize** — 這 tool 的價值來自「agent 知道『我』」— error book 綁我、prompt 追我的階段、換人用整條 pipeline 的 signal 就崩了

## 觸發回頭調整的訊號

- 連續 3 個 session Claude 分析品質下滑 → 抓 pattern 調 prompt
- Opus fallback 每次都觸發 → 換 primary model 或改 quote convention（`"..."` → `**...**`）
- Error book 破 150 → 認真做 Phase C
- 我英文變好到 Claude native 建議 spot-check 大部分 pass → 開始減少 daily practice 頻率

## 未來構想（尚未實作）

- **用 roleplay 重講過去的內容，測 disfluency 有沒有進步、抓出「學不起來」的 error**：複雜內容的破碎感（卡頓、重組句子）很大一部分來自內容規劃、不是語言能力，同內容重講第二次通常會明顯順很多。想拿這個做 before/after 比較，但不能用 freestyle 重講——沒有腳本鎖住內容順序，AI 追問方向會飄，兩次講的根本不是同一件事，比較不公平。要做的話，用某場舊 session 的逐字稿重建成固定 AI 台詞 + 中文語意提示（不是逐字腳本，只給語意，逼我重新組織英文），格式比照現有 roleplay。
  - 更重要的動機：分辨哪些 error 是「提點一次就記得」（例如 master's degree，#0078，有信心下次會對）、哪些是「一直提醒還是一直錯」（例如 follow up with，已經在 error book 出現兩次：#0036、#0067）。重講舊內容會自然把這種 sticky error 逼出來，比單純看 error book 累積次數更直接。
  - 實作方式要走正式功能（backend endpoint + 前端按鈕，例如「重新練習這場舊 session」），不要每次靠 Claude 手動寫檔案——手動寫法沒有 UI、沒有 dedup，時間久了 `data/roleplays/` 會亂。有空的時候再排進去做。
