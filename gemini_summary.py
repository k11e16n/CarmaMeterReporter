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
    observation = generate_daily_observation(api_key, readings)
    # observation 可能是 None（呼叫失敗時），呼叫端要能處理這種情況，
    # 讓報告照樣送出，只是少這一句，不該讓 Gemini 掛掉就擋住整個推播。
"""

from __future__ import annotations

import sys

import requests

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.1-flash-lite:generateContent"
)

_SYSTEM_INSTRUCTION = (
    "你是一個居家水電暖氣用量監控助理。使用者會給你今天跟本月的用量數據，"
    "數字本身（今日用量、本月平均/最高/最低）已經會顯示在報告裡，不需要你重複。"
    "請只回傳一句簡短、口語化的繁體中文觀察或建議（30字以內），"
    "聚焦在數字背後可能的意義，例如「今天用量比平均高不少，可能是天氣轉冷」"
    "或「本月用量都很穩定，沒什麼需要注意的」這類語氣。"
    "不要編造你看不到的使用情境（例如猜測是誰在用、做了什麼事），"
    "除非數據明顯支持這個推論。"
    "如果數字都在正常範圍，給一句安心、簡短的話就好，不用勉強找異常。"
    "只回傳這一句話本身，不要加引號、前綴（例如「觀察：」）、或任何說明文字。"
)


def _format_readings_for_prompt(readings: list[dict]) -> str:
    """把整理過的每日資料轉成給 Gemini 看的純文字條列。"""
    lines = []
    for r in readings:
        lines.append(
            f"- {r['label']}（單位：{r['unit']}）："
            f"今日（{r['today_date']}）用量 {r['today_value']}，"
            f"本月平均 {r['month_avg']}，"
            f"本月最高 {r['month_max']}（{r['month_max_date']}），"
            f"本月最低 {r['month_min']}（{r['month_min_date']}）"
        )
    return "\n".join(lines)


def generate_daily_observation(
    api_key: str, readings: list[dict], timeout: int = 15
) -> str | None:
    """
    readings 裡每個元素需要有：
        label, unit, today_date, today_value,
        month_avg, month_max, month_max_date, month_min, month_min_date

    回傳 Gemini 生成的一句話；呼叫失敗（網路問題、額度用盡、回應格式跟預期
    不同）一律回傳 None，不拋例外——這句話是報告的加分項，不是必要欄位，
    失敗不該讓整個每日推播卡住。
    """
    if not readings:
        return None

    prompt_body = _format_readings_for_prompt(readings)

    payload = {
        "systemInstruction": {"parts": [{"text": _SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt_body}]}],
        "generationConfig": {
            "maxOutputTokens": 100,
            "temperature": 0.4,
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
        return text or None
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        print(f"[WARN] Gemini 觀察句生成失敗，報告將略過這一句：{e}", file=sys.stderr)
        return None
