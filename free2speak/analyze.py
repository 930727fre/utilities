#!/usr/bin/env python3
"""Usage: python analyze.py

音檔與 role-play 腳本各自從互動選單選取。
腳本可選「無腳本」（閒聊 / 自由發揮）。
"""
import sys, os, json, re
from datetime import date, datetime
from pathlib import Path
from google import genai

ROOT = Path(__file__).parent
today = date.today().isoformat()


def pick_audio():
    patterns = ["*.m4a", "*.mp3", "*.wav", "*.aac"]
    search_dirs = [Path.cwd(), Path.home() / "Downloads"]
    files = []
    for d in search_dirs:
        if d.exists():
            for pat in patterns:
                files.extend(d.glob(pat))
    files = sorted(set(files), key=lambda p: p.stat().st_mtime, reverse=True)[:10]

    if not files:
        print("✗ 找不到音檔（搜尋 Desktop、Downloads、當前資料夾）")
        sys.exit(1)

    if len(files) == 1:
        print(f"→ 音檔：{files[0].name}")
        return files[0]

    print("找到以下音檔（依時間排序）：")
    for i, f in enumerate(files, 1):
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M")
        print(f"  {i}. {f.name}  [{mtime}  {f.parent}]")
    while True:
        raw = input("選擇 [1]: ").strip() or "1"
        if raw.isdigit() and 1 <= int(raw) <= len(files):
            return files[int(raw) - 1]
        print(f"  請輸入 1–{len(files)}")


def pick_roleplay():
    matches = sorted(
        (ROOT / "data" / "roleplays").glob("[0-9]*-*.md"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:10]

    print("\n選擇 role-play 腳本（近 10 筆，新到舊）：")
    if not matches:
        print("  （尚無腳本）")
    for i, f in enumerate(matches, 1):
        print(f"  {i}. {f.stem}")
    no_script_idx = len(matches) + 1
    print(f"  {no_script_idx}. 無腳本（閒聊 / 自由發揮）")

    while True:
        raw = input("選擇 [1]: ").strip() or "1"
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(matches):
                f = matches[n - 1]
                return f.stem.split("-", 3)[3], f.read_text()
            if n == no_script_idx:
                t = input("主題（用於檔名，例如 chat）：").strip() or "chat"
                return t, "(無腳本 — 閒聊或自由發揮)"
        print(f"  請輸入 1–{no_script_idx}")


audio_path = pick_audio()
topic, roleplay_ctx = pick_roleplay()

base_prompt = (ROOT / "prompts" / "gemini-analysis.md").read_text()

if roleplay_ctx.startswith("(無腳本"):
    script_section = f"練習類型：閒聊 / 自由發揮（無腳本）\n\n{roleplay_ctx}"
else:
    script_section = (
        "今日 role-play 腳本（內含 AI 的英文台詞 + 使用者該翻譯的中文 cue；"
        f"使用者看中文即時翻成英文，方便你判斷他翻出來的英文哪裡有 gap）：\n\n{roleplay_ctx}"
    )

full_prompt = f"{base_prompt}\n\n---\n{script_section}"

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
print(f"→ uploading {audio_path.name} ...")
audio_file = client.files.upload(file=str(audio_path))
print(f"→ analyzing ...")
resp = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=[full_prompt, audio_file],
)

text = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.text.strip(), flags=re.MULTILINE).strip()
try:
    data = json.loads(text)
except json.JSONDecodeError as e:
    print(f"✗ JSON parse 失敗：{e}")
    print(f"  音檔保留在 {audio_path}，修好後可重跑")
    sys.exit(1)

out = ROOT / "data" / "sessions" / f"{today}-{topic}.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(f"✓ wrote {out}")

audio_path.unlink()
print(f"✓ deleted {audio_path.name}")
