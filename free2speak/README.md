# free2speak

我的英文口說自學系統。**Code 是 scaffolding — 真正的產品是 LLM/agent 對我英文 bottleneck 的精準診斷 + 每次 session 都逼出一個 contrastive noticing artifact**。

未來讀 code 前請先讀下面這一節。

## Agent 必讀：我的英文 profile

**One-line 診斷**：
你是 upper-intermediate 英文使用者，主要 bottleneck 是「中文查表 → 錯 register 英文」（Chinglish at register/collocation level），靠 daily free2speak session + 每天 30 分鐘 native podcast/影集 input + 對陌生 native chunk 建立信任 + freestyle 主動 recycle 剛學的 phrase 這四件事複利，12-24 個月能質變成 native texture。

**Chinglish path fingerprint**：
- 中文情境詞 → 查字典最直接對應的英文 → 選錯 register
- 已知 pattern：預約→reservation（應 appointment）、修改→modification（應 change）、白天→on the day（應 during the day）、撞到→collide（應 conflict）、回饋→feedback（應 hear back / response）、核准→validated（應 approved）、上線→online（應 launch / deployment）
- 特徵：文法對、詞認識、register 落在 formal / 公文 / 物理層面，不是日常對話

**每張 card 的品質底線**：一張 card 進 error book 前（不論 Sonnet 自動產的還是 discuss mode 討論後手動寫的）要符合這幾條，不符合就打回重寫或 skip：

1. `l1_diagnosis` 要具體指名「我這次講錯是因為中文 X 直譯成英文 Y」— 不是抽象規則描述、不是講英文用法而已
2. `native` 要日常慣用（`hear back`），不要 stilted 翻譯腔（`receive information`）
3. `register` 一個短 phrase 就好，不要 slash-list 排排站（`medical / dental` 對，`medical / customer / general / insurance` 錯）
4. 一張卡對應一條 rule，不能混（missing plural + uncountable-noun 誤用要拆兩張）

## Tool 幹嘛

我在 Gemini Live 演一段 roleplay → 錄音上傳 → Gemini 逐字稿 → Claude Sonnet 4.6 分析 → 我 swipe / 討論後進 error book → drill 每天 recall。

- **Gemini 2.5 Flash**：只做逐字稿（temp=0.0）
- **Claude Sonnet 4.6**：拿逐字稿做分析（temp=0.2）。stringify quirk 時 auto retry → Opus 4.7 fallback
- **Claude Opus 4.7**：生 roleplay 跟 drill

## 兩種 review mode

**auto**（default）：upload → Sonnet 自動分析 → 前端 tinder-swipe。日常 90% 走這條。

**discuss**：upload 時勾 `[✓] discuss with Claude` toggle。session 只存 transcript。之後我拿 session_id 找 Claude 討論 → `apply_review.py` 從 stdin JSON 寫進 error book。適合品質要求高的 session。

## Daily flow

1. `/` Practice → 看 active roleplay → 按 `Copy Gemini prompt`
2. Gemini Live 貼 prompt 演完 → 錄音
3. Upload → auto or discuss
4. Swipe（或 ping Claude 討論後 apply_review）→ 進 error book
5. `/drill` 每天 10 張卡 recall

## Data layout

檔案系統即狀態，rm/mv 就是 rollback。

```
data/
├── errors/{active,graduated}/NNNN-<slug>.md
├── roleplays/{active,done}/<date>-<topic>*.md
├── sessions/<YYYY-MM-DDTHH-MM-SS-hash8>.{json,decisions.jsonl,ext}
├── drills/<YYYY-MM-DD>.json
└── legacy/                    # 1.0 archive, runtime 不碰
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
