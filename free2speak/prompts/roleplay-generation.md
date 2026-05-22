# Role-play 生成指令

生成新 role-play 檔案時遵守以下原則。給 Claude Code 互動式生成 role-play 用。

---

**格式：完整雙語 dialogue script**。AI 的每一句用英文寫死、使用者的每一句用中文寫死。使用者看 Chinese instruction 即時翻成英文說出來 —— 翻譯這個過程就是練習。

### 0. 場景選擇優先順序

1. **自然 fit 為主** —— 場景本身要可信、是真實會發生的對話
2. **error-driven 為次要 filter** —— 看 errors.md 最近的 cluster，挑一個自然會用到那些 pattern 的場景。**不要硬湊** —— 寧可 miss 某次 pattern 練習，也不要編出扭曲場景（例：「室友吵架時剛好提到他家貓對堅果嚴重過敏」）

Cold start 階段（errors.md 還少時）走 random / variety；累積到一定量後再轉 error-driven 篩選。

**Roleplays index 永遠 append、不刪舊行**：生完新檔 append 一行，索引行格式 `YYYY-MM-DD — 情境 — 一句話 rationale`（情境 = 場景 slug，同檔名主題）。生成時看尾端最近 ~10 行避免場景重複即可；完整 index 留著供月度 audit 看軌跡。

### 1. Scope：5-7 個 exchanges，3-5 分鐘

一個 role-play = 5-7 輪 AI/User 互動。AI 第一句直接進入主題或用一句日常開場，不要 prologue。

### 2. Register

場域跟著場景走，不限制 register。日常 peer（室友 / 朋友 / 餐廳店員 / 鄰居）、職場（面試 / 開會 / 簡報）、服務場合（DMV / 房東 / 保險）都可以生。

場景 fit 是唯一的標準：這個情境真實嗎、使用者在美國生活會遇到嗎？會 = 可以生。

### 3. Chinese instruction 怎麼寫

每輪 Chinese instruction 寫 1-2 句**完整的「使用者該說的話的中文版本」**。要 embed 具體 content hooks 強迫使用者翻出中階詞彙：

- ✓ 「告訴室友你昨天接到 HR 通知，下個月正式升職、加薪 10%」—— 強迫翻 "HR notified me / promotion / raise"
- ✗ 「分享一個好消息」—— 太抽象、使用者會用 "I have good news" 矇混過去

中文要具體到「不知道某個詞」會直接暴露。每輪 instruction 加上 speech act 標籤（push back / counter-propose / agree / decline）。

### 4. English script 怎麼寫

AI 的每一句寫完整英文、會在 Gemini Live 中 verbatim 播放。要點：
- AI 的 line 設計成**自然引出使用者要練的詞彙 / 句型**
- 可以**多 hook 一句帶 2-3 個顧慮**，比一句一個顧慮接近真實對話（例：AI 一次拋「太貴 + 剛買菜 + 常吃外面」三點，逼使用者選擇怎麼回應）

### 5. 檔案結構

檔名 `data/roleplays/YYYY-MM-DD-主題.md` —— 主題是短 slug（例 `bistro` / `interview` / `visa`），同日多個 role-play 靠主題區隔、不撞名。檔案內容：

```
# YYYY-MM-DD — Role-play: [scenario 名]（情境分類）

> rationale 一行

## Scenario           ← 1-2 句背景設定
## Setup              ← 提醒 AI verbatim、使用者翻譯
## Dialogue
### Exchange 1
**AI:** "..."
**You (中文):** ...
### Exchange 2
...
## Gemini 開場 prompt ← 指示 Gemini play script verbatim + 列出所有 AI lines
```

檔案總長通常 ~50-70 行（看 exchange 數量）。Gemini prompt 區塊必須**明確指示 verbatim、no improv、no paraphrase**，否則 Gemini 會自由發揮、整個 scripted 設計就破功。

## 範例

```
# 2026-05-11 — Role-play: 說服室友去新餐廳（social）

> rationale: 練 peer-level push back + share view，scripted 強迫翻出
> promotion / hand-made pasta / wine list / atmosphere / treat 等中階詞

## Scenario
你昨天剛被通知下個月升職、加薪 10%。週末晚餐你想帶室友去附近新開的義式
餐廳 —— 你看過評論，他們手工 pasta、酒單、店裡氛圍都很適合慶祝。但室友
有三個顧慮：太貴、剛買完一週的菜、平常已經夠常吃外面。

## Setup
- AI (Gemini): 照 script verbatim 播放、不改詞、不 improv
- 你: 看 Chinese instruction，即時翻成英文（每輪 1-3 句）

## Dialogue

### Exchange 1
**AI:** "Hey! How was your day?"
**You (中文):** 心情很好，告訴室友你昨天接到 HR 通知，下個月正式升職、
              加薪 10%，想犒賞自己一下。

### Exchange 2
**AI:** "Oh wow, congrats! That's a big deal. We should celebrate or something."
**You (中文):** 順著他的話，提議週末去那家附近新開的義式餐廳 —— 你看過
              評論，他們手工 pasta、酒單、氛圍都很有名，正適合慶祝。

（後面 4 個 exchange 略）

## Gemini 開場 prompt
You are playing the user's roommate in a SCRIPTED role-play. Read each of
your 6 lines below verbatim, in order. After each line, wait for the user
to respond, then proceed to the next line. Do NOT improvise, do NOT
paraphrase, do NOT deviate from the script. If the user gets stuck,
gently say "take your time" and wait.

Your lines (in order):
1. "Hey! How was your day?"
2. "Oh wow, congrats! ..."
（後續 4 句略）
```
