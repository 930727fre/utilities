<!-- audience：Gemini API。此檔由 analyze.py 載入後連同錄音送給 Gemini，不是給 Opus 讀的。prompts/ 底下其他三個 .md（roleplay / drill / errors-generation）才是 Opus 的生成 spec。 -->

你會收到一段我跟 AI 練習英文口說的錄音。請分析「我」的表現，
AI 的部分不用糾正。如果分不出來，假設較沒自信、語速較不穩
的那個是我。

請輸出以下 JSON：

{
  "transcript": "整段對話逐字稿，標註 [Me] 和 [AI]",
  "summary": "1-2 句中文總結這次練習",
  "errors": [
    {
      "content": "我實際說的話",
      "correction": "正確/更自然的版本",
      "context": "出現在什麼對話情境",
      "explanation": "中文解釋為什麼錯/可以更好（1-2句）"
    }
  ],
  "fluency_notes": "整體流暢度、自信、語調的觀察"
}

注意：
- 不要列細微口音、流暢度小停頓、self-correct 已修正的錯誤
- explanation 必須是中文，1-2 句，直接講重點
- 只輸出 JSON，不要前後文
