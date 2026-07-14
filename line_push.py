#!/usr/bin/env python3
"""
LINE Messaging API 推播模組。

刻意獨立於 carma_scraper.py 之外：CarmaMeterReporter 只是第一個使用者，
之後股票、新聞等模組要推播時，直接 import 這個檔案即可，不用重複實作
push 邏輯或錯誤處理。

用法：
    from line_push import push_line_message, build_flex_bubble, build_flex_carousel_message

    message = build_flex_carousel_message(
        alt_text="今日用量摘要",
        bubbles=[build_flex_bubble("日常用電", ["今日：12.3 kWh", "本月平均：13.8 kWh"])],
    )
    push_line_message(access_token, group_id, [message])
"""

from __future__ import annotations

import requests

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

# LINE 平台的硬性限制，這裡不是我們自己訂的
MAX_MESSAGES_PER_PUSH = 5
MAX_BUBBLES_PER_CAROUSEL = 12


class LinePushQuotaExceeded(Exception):
    """本月免費推播額度用盡，或短時間內送太多次觸發速率限制。"""


class LinePushAuthError(Exception):
    """Channel Access Token 無效或過期。"""


class LinePushError(Exception):
    """其他推播失敗情況（收件人不存在、payload 格式錯誤等）。"""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"LINE push 失敗 (status={status_code}): {body}")


def push_line_message(access_token: str, to: str, messages: list[dict]) -> None:
    """
    呼叫 LINE Push Message API。成功回傳 None；失敗一律用例外表達，
    呼叫端可以用 except 分別處理「額度用盡」「認證失敗」「其他錯誤」三種情況，
    決定要不要重試、要不要改用備援通知管道。

    messages: 最多 5 個 message object（LINE 平台限制，不是我們自訂的）。
    """
    if not messages:
        raise ValueError("messages 不能是空的")
    if len(messages) > MAX_MESSAGES_PER_PUSH:
        raise ValueError(f"單次最多 {MAX_MESSAGES_PER_PUSH} 個 message object，收到 {len(messages)} 個")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    payload = {"to": to, "messages": messages}

    r = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=10)

    if r.status_code == 200:
        return

    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text}

    if r.status_code == 401:
        raise LinePushAuthError(f"認證失敗，Channel Access Token 可能已失效：{body}")

    if r.status_code == 429:
        message_text = str(body.get("message", ""))
        raise LinePushQuotaExceeded(
            f"推播被拒絕（額度用盡或觸發速率限制）：{message_text or body}"
        )

    raise LinePushError(r.status_code, body)


def build_flex_bubble(title: str, lines: list[str], footer: str | None = None) -> dict:
    """
    建一個簡單的 Flex Message bubble：標題 + 多行文字內容。
    Phase 4 的每日簡報可以對每個追蹤項目（電/水/暖氣）各建一個 bubble，
    再用 build_flex_carousel_message() 組成一則可以左右滑動的訊息。
    """
    body_contents: list[dict] = [
        {"type": "text", "text": title, "weight": "bold", "size": "lg", "wrap": True},
        {"type": "separator", "margin": "md"},
    ]
    for line in lines:
        body_contents.append(
            {"type": "text", "text": line, "size": "sm", "wrap": True, "margin": "sm"}
        )

    bubble: dict = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body_contents,
        },
    }

    if footer:
        bubble["footer"] = {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": footer, "size": "xs", "color": "#999999", "wrap": True}
            ],
        }

    return bubble


def build_flex_carousel_message(alt_text: str, bubbles: list[dict]) -> dict:
    """
    把多個 bubble 組成一個 message object。
    - 只有一個 bubble：直接送單張卡片
    - 多個 bubble：包成 carousel（左右滑動）

    alt_text 是必填欄位：裝置無法顯示 Flex Message 時（例如通知橫幅、聊天列表預覽），
    LINE 會改顯示這段文字，不能省略。
    """
    if not bubbles:
        raise ValueError("bubbles 不能是空的")
    if len(bubbles) > MAX_BUBBLES_PER_CAROUSEL:
        raise ValueError(f"carousel 最多 {MAX_BUBBLES_PER_CAROUSEL} 個 bubble，收到 {len(bubbles)} 個")

    contents = bubbles[0] if len(bubbles) == 1 else {"type": "carousel", "contents": bubbles}

    return {
        "type": "flex",
        "altText": alt_text,
        "contents": contents,
    }
