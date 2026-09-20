"""定期抽換本地 Irasutoya（いらすとや）插圖庫，讓 illustration_generator.py
每次挑到的圖不會一直重複。

不呼叫任何 AI（不會有 hallucination 風險）：直接打 irasutoya.com 的搜尋頁面，
用正規表達式解析頁面裡 `bp_thumbnail_resize("縮圖網址","標題")` 這段內嵌
JS 呼叫取得候選圖片，把縮圖網址的 `/s72-c/` 換成 `/s1600/` 拿到原始大圖再
下載。完全自動、不經人工審核——每次執行會整批替換掉舊圖。

這支腳本刻意獨立於 daily_report.py 的每日推播流程之外：排程上建議跟每日
報告的 cron 分開（例如每月手動或排程跑一次即可），就算某次抽換沒抓到理想
的圖，也不會影響當天報告照常送出，因為 illustration_generator.py 讀的是
既有的本地圖庫，不會因為抽換失敗而清空。

執行方式：python3 illustration_pool_refresher.py
"""

from __future__ import annotations

import os
import random
import re
import time

import requests

POOL_DIR = os.path.join(os.path.dirname(__file__), "assets", "irasutoya_pool")
POOL_SIZE_PER_MOOD = 6

MOOD_KEYWORDS = {
    "high": ["疲れた", "動揺", "焦る"],
    "normal": ["嬉しい", "リラックス"],
    "low": ["眠い", "あくび"],
}

SEARCH_URL = "https://www.irasutoya.com/search"
_THUMBNAIL_PATTERN = re.compile(r'bp_thumbnail_resize\("([^"]+)","([^"]*)"\)')
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _search_candidates(keyword: str, max_results: int = 20) -> list[tuple[str, str]]:
    """打 irasutoya 的搜尋頁面，回傳 (原始大圖網址, 標題) 候選清單。"""
    resp = requests.get(
        SEARCH_URL,
        params={"q": keyword, "max-results": max_results},
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    candidates = []
    for thumb_url, title in _THUMBNAIL_PATTERN.findall(resp.text):
        if "いろいろな表情" in title:
            # 這種標題是「一張圖裡塞多種表情」的合輯圖，不適合當單一 hero 插圖
            continue
        full_url = thumb_url.replace("/s72-c/", "/s1600/")
        candidates.append((full_url, title))
    return candidates


def refresh_pool(mood: str, keywords: list[str] | None = None, pool_size: int = POOL_SIZE_PER_MOOD) -> int:
    """為單一 mood 重新抓一批候選圖，整批取代舊的圖庫內容。回傳實際存了幾張。"""
    keywords = keywords or MOOD_KEYWORDS[mood]

    all_candidates: list[tuple[str, str]] = []
    for kw in keywords:
        all_candidates.extend(_search_candidates(kw))
        time.sleep(1)  # 對別人的伺服器客氣一點，不要連續密集打

    random.shuffle(all_candidates)

    mood_dir = os.path.join(POOL_DIR, mood)
    os.makedirs(mood_dir, exist_ok=True)
    for old_file in os.listdir(mood_dir):
        os.remove(os.path.join(mood_dir, old_file))

    saved = 0
    for url, title in all_candidates:
        if saved >= pool_size:
            break
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[WARN] 下載失敗，略過：{url}（{exc}）")
            continue

        out_path = os.path.join(mood_dir, f"{saved:02d}.png")
        with open(out_path, "wb") as f:
            f.write(resp.content)
        print(f"[OK] {mood}: {title} -> {out_path}")
        saved += 1

    return saved


def refresh_all_pools() -> dict[str, int]:
    counts = {}
    for mood in MOOD_KEYWORDS:
        counts[mood] = refresh_pool(mood)
        print(f"[DONE] {mood}: 共存了 {counts[mood]} 張")
    return counts


if __name__ == "__main__":
    refresh_all_pools()
