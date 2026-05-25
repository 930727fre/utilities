#!/usr/bin/env python3
"""Minimal Gemini latency test — 10 short JA→zh-Hant cues, warm connection.

Run from the host shell with the API key in the current shell only:
    export GEMINI_API_KEY="<paste>"
    python3 test-gemini.py

Reuses a single HTTPS connection across the 10 calls (via requests.Session) so
the steady-state numbers match what xyt would actually see if it switched.
"""
import os
import time

import requests

API_KEY = os.environ["GEMINI_API_KEY"]
URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
       f"gemini-2.5-flash-lite:generateContent?key={API_KEY}")

CUES = [
    "当たり前でしょ?",
    "ありがとうございます",
    "今日はいい天気ですね",
    "明日また会いましょう",
    "わかりました",
    "すみません、ちょっと待ってください",
    "ビワコイに入水しました",
    "美味しいですね",
    "頑張ってください",
    "おはようございます",
]


def prompt(t: str) -> str:
    return ("Translate the following Japanese subtitle line to Traditional Chinese.\n"
            "Output ONLY the translation. No commentary, no romanization, no Japanese.\n\n"
            f"Japanese: {t}\nTraditional Chinese:")


def main():
    s = requests.Session()
    total = 0.0
    for i, cue in enumerate(CUES, 1):
        t0 = time.perf_counter()
        r = s.post(URL, json={"contents": [{"parts": [{"text": prompt(cue)}]}]}, timeout=30)
        dt = time.perf_counter() - t0
        total += dt
        try:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            text = f"ERROR {r.status_code}: {r.text[:120]}"
        print(f"{i:2d}. {dt*1000:5.0f}ms  {cue}  →  {text}")

    print(f"\n{len(CUES)} calls · total {total:.2f}s · avg {total/len(CUES)*1000:.0f}ms")


if __name__ == "__main__":
    main()
