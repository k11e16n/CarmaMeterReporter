#!/usr/bin/env python3
"""
呼叫 Gemini API，把 CarmaMeterReporter 當天抓到的結構化數字（今日用量、
本月平均/最高/最低）轉成一句自然語言觀察/建議，附加在每日簡報裡。

刻意獨立於 carma_scraper.py 與 line_push.py 之外：之後股票、新聞模組如果
也想要「一句話摘要」，直接重用這支模組即可，只要準備好對應的 readings
格式或另外設計 prompt。

用法：
    from gemini_summary import generate_daily_observation

    readings = [
        {
            "label": "日常用電", "unit": "kWh",
            "today_date": "2026-07-11", "today_value": 12.3,
            "month_avg": 13.8, "month_max": 40.0, "month_max_date": "2026-06-30",
            "month_min": 3.0, "month_min_date": "2026-06-01",
        },
        # ... 其餘三項
    ]
    observation, mood = generate_daily_observation(api_key, readings)
    # observation 可能是 None（呼叫失敗時），mood 失敗時預設為 "normal"。
    # 呼叫端要能處理 observation 是 None 的情況，讓報告照樣送出，只是少
    # 這一句，不該讓 Gemini 掛掉就擋住整個推播。
"""

from __future__ import annotations

import json
import sys

import requests

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.1-flash-lite:generateContent"
)

_SYSTEM_INSTRUCTION = (
    "你是一個居家水電暖氣用量監控助理，個性幽默俏皮，說話走年輕世代的網路吐槽風，"
    "可以適度加 emoji。使用者會給你今天、本月平均、上月平均的用量數據，"
    "數字本身已經會顯示在報告裡，不需要你重複唸一次。"
    "請只回傳一句簡短、有梗的繁體中文吐槽或稱讚（50字以內）。"
    "如果某項用量比本月平均或上個月平均高出不少，可以毫不留情地酸一下、玩笑吐槽"
    "（例如『這是在家開三溫暖膩』『這個月是家裡開演唱會嗎』這種語氣），"
    "但吐槽要緊扣數字本身的變化幅度，不要編造你看不到的使用情境"
    "（例如猜測是誰在用、在家做了什麼事），除非數據明顯支持這個推論。"
    "如果數字都在正常範圍，用輕鬆調皮的語氣講一句安心的話就好，不用勉強找梗、不用硬酸。"
    "只回傳一個 JSON 物件，格式為 "
    '{"observation": "<一句吐槽或稱讚，不加引號或前綴>", "mood": "high"|"normal"|"low"}。'
    "mood 由你比較四個指標『今日用量』相對『本月平均』的偏離程度，選出偏離最大"
    "（無論偏高或偏低）的那個指標所代表的狀態：明顯偏高選 high，明顯偏低選 low，"
    "其餘或無法判斷選 normal。不要加任何其他文字，也不要用 markdown code fence 包住 JSON。"
)


def _format_readings_for_prompt(readings: list[dict]) -> str:
    """把整理過的每日資料轉成給 Gemini 看的純文字條列。"""
    lines = []
    for r in readings:
        prev_month_part = (
            f"，上月平均 {r['prev_month_avg']}" if r.get("prev_month_avg") is not None else ""
        )
        lines.append(
            f"- {r['label']}（單位：{r['unit']}）："
            f"今日（{r['today_date']}）用量 {r['today_value']}，"
            f"本月平均 {r['month_avg']}，"
            f"本月最高 {r['month_max']}（{r['month_max_date']}），"
            f"本月最低 {r['month_min']}（{r['month_min_date']}）"
            f"{prev_month_part}"
        )
    return "\n".join(lines)


def generate_daily_observation(
    api_key: str, readings: list[dict], timeout: int = 15
) -> tuple[str | None, str]:
    """
    readings 裡每個元素需要有：
        label, unit, today_date, today_value,
        month_avg, month_max, month_max_date, month_min, month_min_date

    回傳 (observation, mood)：observation 是 Gemini 生成的一句話，呼叫失敗
    （網路問題、額度用盡、回應格式跟預期不同）時為 None；mood 是
    "high"/"normal"/"low" 之一，呼叫失敗或內容不合法時預設為 "normal"。
    這個函式不拋例外——這句話跟情緒判斷都是報告的加分項，不是必要欄位，
    失敗不該讓整個每日推播卡住。
    """
    if not readings:
        return None, "normal"

    prompt_body = _format_readings_for_prompt(readings)

    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt_body}]}],
        "generationConfig": {
            "maxOutputTokens": 200,
            "temperature": 0.4,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "object",
                "properties": {
                    "observation": {"type": "string"},
                    "mood": {"type": "string", "enum": ["high", "normal", "low"]},
                },
                "required": ["observation", "mood"],
            },
        },
    }

    try:
        r = requests.post(
            GEMINI_API_URL,
            params={"key": api_key},
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        parsed = json.loads(text)
        observation = parsed.get("observation")
        mood = parsed.get("mood")
        if mood not in ("high", "normal", "low"):
            mood = "normal"
        if not isinstance(observation, str) or not observation.strip():
            observation = None
        return observation, mood
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError) as e:
        print(f"[WARN] Gemini 觀察句生成失敗，報告將略過這一句：{e}", file=sys.stderr)
        return None, "normal"
