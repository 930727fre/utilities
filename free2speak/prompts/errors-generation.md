# Errors.md 整理指令

整理 errors.md 時遵守以下原則。給 Claude Code 互動式更新用。

---

## 觸發

使用者說「看 sessions/ 最新一筆，更新 errors.md」→ 讀那筆 session 的 JSON。
先把該 session 的 `summary` 跟 `fluency_notes` 顯示給使用者看（一兩句練習總結
+ 流暢度觀察），再做兩個對稱動作：**加** 跟 **刪**。

兩個動作邏輯相同 —— Opus 從這場的 transcript 撈候選、條列給使用者，**使用者
自己勾選**。Opus 不替使用者決定收不收、畢不畢業。

### 加 —— 新錯收進 errors.md

把 session `errors[]` 的**全部**條目逐條列給使用者，**不要預篩**，由使用者
勾選要收的。Opus 不替使用者判斷哪些「值得收」—— 看到候選使用者自己會判斷。

- 勾選的條目寫進 errors.md。若某條跟現有條目是同一個 pattern → 範例 append
  進現有條目，不開新條（見「同類合一」）。
- 沒勾的候選**直接棄用**，不轉去任何地方。

### 刪 —— 學會的錯畢業掉

掃 transcript，比對 errors.md 每一條，找出「這場使用者**有產出該 pattern 且
正確**」的條目，列成清單給使用者：

> 這幾條這次你講對了 —— 「第三人稱 -s」你說了 "My brother gets up"、
> 「allergy + to」你說了 "allergic to cats"…… 要畢業（刪掉）哪些？

使用者自由心證：有把握就畢業、覺得只是短期記憶之後還會犯就留著。勾選的條目
**直接刪除整條**（不留 archive）。

「講對一次」只是呈給使用者的證據，**不是規則** —— 沒有計數器、沒有門檻、沒有
「累積 N 次才能畢業」。畢業與否的判斷在使用者腦中，不在檔案裡。

**長尾**：已改掉、但之後對話都沒再用到的錯，不會被「刪」步驟撈到（它沒出現在
transcript）。這類統一在月度 audit 時處理 —— Opus 掃 errors.md 對比近期 sessions/，
找出長期沒出現的條目逐條列給使用者決定是否畢業。

## 錯題池規則

- **同類合一** —— 同一 pattern 算一條，靠判斷不用 regex。例：「She don't like」
  「She own」「it just shut down」都算「第三人稱單數 -s」一條，新範例 append
  進去，不開新條目。
- **畢業就刪，不留 archive** —— 畢業 = 刪除整條。未來若回頭犯，當新條目處理，
  沒有 mature / archive 狀態。判斷依據單純 = errors.md 裡有沒有對應條目：有 →
  走「重新犯」；沒有 → 走「加新錯」。Opus 不需要知道某 pattern 是否曾畢業過。

## 每條條目要有

- **標題**：簡短描述 pattern（例：「第三人稱單數動詞 -s」、「allergy + to，
  不是 with」）
- **範例**：使用者實際說的錯 → 正確版。可多筆累積。
- **分組**：## 文法 / ## 用法 / 中式英文 / ## 詞彙
- **上次出現**：YYYY-MM-DD（純資訊，供 drill 挑「最近犯」參考，不附著任何規則）

沒有計數器、沒有狀態欄。

## 重新犯時

該 pattern 已在 errors.md 裡 → 範例 append 一筆、「上次出現」更新成新日期。
（該 pattern 不在 errors.md 裡 → 走「加新錯」，當新條目。）

## 範例

```markdown
# 錯題本

> Active only。畢業就刪，不留 archive。

## 文法

### 第三人稱單數動詞 -s
- 範例：She don't really like → She doesn't really like
- 範例：She own a garden → She owns a garden
- 範例：it just shut down at 40% → it just shuts down at 40%
- 上次出現：2026-05-14

## 用法 / 中式英文

### "allergy / allergic" 後面接 to，不是 with
- 範例：I have a mild allergy with seafood → I have a mild allergy to seafood
- 同理：allergic to / sensitive to / immune to
- 上次出現：2026-05-15

## 詞彙

### grape vs grapefruit 別搞混
- 範例：a cup of grape juice → a cup of grapefruit juice
- 備註：grape = 葡萄、grapefruit = 葡萄柚，完全不同水果
- 上次出現：2026-05-15
```
