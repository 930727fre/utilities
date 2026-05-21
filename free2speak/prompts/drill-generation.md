# Drill 生成指令

生成新 drill 檔案時遵守以下原則。給 Claude Code 互動式生成 drill 用。

---

## 觸發

使用者說「生今天的 drill」→ 看 `errors.md` + 最近 10 筆 sessions/ + `drills/index.md`，寫成 `drills/YYYY-MM-DD.md` 並 append 一行索引。

索引行格式 `YYYY-MM-DD — 一句話 rationale`，永遠 append、不刪舊行。只給月度 audit 看軌跡用，不需要記下選了哪些 error。

## Session 結構

每天 **10 張卡**。檔案分兩段：
- **第一遍**：卡 1→10 順序
- **第二遍**：同 10 張隨機排序當複習

共 20 次 plays、10 張獨立卡片。答案用 `<details>` 折疊。

沒有自評、沒有排程、沒有 AI 評分 —— 使用者打開 md 自己照著唸。

## 題目組合

10 張卡的組合大致：
- **~7 條來自 errors.md** —— 已知錯題複習。挑「高頻 + 最近犯 + recurring」優先
- **~3 條來自最近 sessions/** —— 不是錯題本身，是延伸學習：更道地的表達、慣用搭配、最近主題相關片語。獨立從 session 內容（transcript、AI 用過的道地說法）取材，跟 errors.md 收了哪些錯無關

比例看當下情況自己調。

## 兩種子題型（單次 session 混搭）

| 題型 | 機制 |
|---|---|
| 中翻英 | 顯示中文 → 你唸英文 → 顯示參考答案 |
| 填空 recall | 顯示英文（部分挖空）+ 中文 → 你補出來 → 顯示完整版 |

**選哪種：**
- 短文法 / 慣用搭配 / 短詞彙 → 中翻英
- 長句型 / 慣用語 / 固定片語 → 填空 recall

## 動態生成原則

Drill 卡的句子要**新生成**，**不能**把 errors.md 原來的範例句直接重貼。同一個 pattern 每次用不同例句，避免死背特定句子。

## Cold start

errors.md + sessions/ 加總題材不足以生 10 張卡時 drill 不啟用 —— 先用 role-play 累積資料（前一兩週純跑 role-play、主題隨機選）。

## 檔案結構

```
# YYYY-MM-DD — Drill

> rationale 一行

## 第一遍（順序 1→10）

### 1. [中翻英]
中文：[Chinese sentence]

<details>
<summary>展開答案</summary>

**英文：** [English answer]
**針對：** [errors.md 條目名 / sessions/ 延伸出處]

</details>

### 8. [填空 recall]
中文：[Chinese hint]
英文：`[English with ___ blanks]`

<details>
<summary>展開答案</summary>

**完整：** [完整英文，填空處用 **bold** 標出]
**針對：** [pattern / 片語]

</details>

## 第二遍（隨機順序）

[同 10 張卡，打亂順序後完整重貼內容，格式同第一遍]
```
