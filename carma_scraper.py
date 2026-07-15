#!/usr/bin/env python3
"""
CARMA Smart Metering 用電/用水/暖氣資料抓取腳本

架構：
  1. 登入（GET 拿 VIEWSTATE 三兄弟 + POST 送帳密）
  2. 依序切換 5 個分頁（AJAX partial postback），解析每頁的 Highcharts 設定
  3. 寫入 SQLite（carma_readings.db）

已知分頁對應（尚未完全確認，見 TAB_SOURCE_TYPES）：
  - 分頁 0 (24203836): 水 (m^3-wtr)
  - 分頁 2 (86085616): 暖氣/冷氣 (kWh-thml)
  - 分頁 1, 3, 4: 待確認

依賴：
  pip install requests beautifulsoup4 --break-system-packages

安全注意：
  - 本網站是 HTTP（非 HTTPS），帳密會明文傳輸，這是網站本身的限制，
    腳本這邊能做的就是絕對不要把帳密寫死在檔案裡、也不要開 debug log 印出 POST body。
  - 帳密應由呼叫端傳入（例如從 GCP Secret Manager 讀出後透過環境變數或參數傳進來），
    這支腳本本身不做任何憑證儲存。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

BASE = "http://www.carmasmartmetering.com/DirectConsumptionDev"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# 分頁索引（0~4）對應的 meter id，順序來自網站上分頁排列順序
# Phase 1 已確認（2026-07-13）：
#   0: 24203836 -> 熱水 (m^3-wtr)
#   1: 24206195 -> 冷水 (m^3-wtr)
#   2: 86085616 -> 冷暖氣 (kWh-thml)
#   3: T0005287 -> 日常用電 (kWh)
#   4: T0005518 -> 不追蹤
TAB_METER_IDS = ["24203836", "24206195", "86085616", "T0005287", "T0005518"]

TAB_SOURCE_TYPES = {
    "24203836": "hot_water",
    "24206195": "cold_water",
    "86085616": "heat_cooling",
    "T0005287": "electricity",
    "T0005518": None,  # 確認不追蹤，抓資料時直接跳過
}

# 需要重試的暫時性錯誤最大重試次數
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 3

# 換月最多嘗試幾次（正常情況下每天執行只會差 0~1 個月，設 3 純粹是防呆上限）
MAX_MONTH_SWITCHES = 3

DEFAULT_DB_PATH = "carma_readings.db"


# ---------------------------------------------------------------------------
# 資料庫
# ---------------------------------------------------------------------------

def init_db(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS utility_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            reading_date TEXT NOT NULL,
            value REAL,
            unit TEXT,
            is_estimated INTEGER DEFAULT 0,
            fetched_at TEXT NOT NULL,
            UNIQUE(meter_id, reading_date)
        )
        """
    )
    conn.commit()
    return conn


def parse_date_label(date_label: str) -> str | None:
    """
    把網站的 'DD/Mon/YYYY'（例如 '31/Jul/2026'）轉成 ISO 格式 'YYYY-MM-DD'。
    存成 ISO 格式是因為它的字串排序剛好等於日期排序，SQL 的
    ORDER BY reading_date 才會得到正確的時間先後，而不是逐字元比對的亂序結果。
    """
    try:
        return datetime.strptime(date_label.strip(), "%d/%b/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def save_chart_data(conn: sqlite3.Connection, meter_id: str, chart: dict) -> None:
    source_type = TAB_SOURCE_TYPES.get(meter_id, "unknown")
    daily_series = next(
        (s for s in chart["series"] if s["name"] != "Average"), None
    )
    if daily_series is None:
        print(f"[WARN] {meter_id}: 找不到每日用量 series，略過", file=sys.stderr)
        return

    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for date_label, point in zip(chart["categories"], daily_series["values"]):
        if not date_label or point["value"] is None:
            continue
        iso_date = parse_date_label(date_label)
        if iso_date is None:
            print(f"[WARN] {meter_id}: 日期格式無法解析，略過此筆：{date_label!r}", file=sys.stderr)
            continue
        rows.append(
            (
                meter_id,
                source_type,
                iso_date,
                point["value"],
                chart["unit"],
                int(point["flagged"]),
                fetched_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO utility_readings
            (meter_id, source_type, reading_date, value, unit, is_estimated, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(meter_id, reading_date) DO UPDATE SET
            value=excluded.value,
            unit=excluded.unit,
            is_estimated=excluded.is_estimated,
            fetched_at=excluded.fetched_at
        """,
        rows,
    )
    conn.commit()
    print(f"[OK] {meter_id} ({source_type}): 寫入 {len(rows)} 筆")


# ---------------------------------------------------------------------------
# 登入
# ---------------------------------------------------------------------------

def get_hidden_fields(soup: BeautifulSoup) -> dict:
    def field(name: str, default: str = "") -> str:
        tag = soup.find("input", {"name": name})
        return tag.get("value", default) if tag else default

    return {
        "__VIEWSTATE": field("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": field("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": field("__EVENTVALIDATION"),
        "tabMeters_ClientState": field("tabMeters_ClientState"),
    }


def login(service_address: str, account_number: str) -> tuple[requests.Session, dict, str]:
    """回傳 (session, 初次 graphing.aspx 的 hidden_fields, 初次 graphing.aspx 的完整 HTML)。

    第三個回傳值很重要：分頁 0（預設啟用的分頁）的 Highcharts 設定會直接內嵌在這份
    HTML 裡（一般 <script> 標籤），不是透過 AJAX 取得，所以呼叫端不能丟棄它。
    """
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    r = s.get(f"{BASE}/login.aspx")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def field(name: str) -> str:
        tag = soup.find("input", {"name": name})
        if tag is None:
            raise RuntimeError(f"登入頁結構跟預期不同，找不到欄位: {name}")
        return tag.get("value", "")

    payload = {
        "__VIEWSTATE": field("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": field("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": field("__EVENTVALIDATION"),
        "username_txt": service_address,
        "password_txt": account_number,
        "login_btn": "Sign In",
    }

    r2 = s.post(f"{BASE}/login.aspx", data=payload)
    r2.raise_for_status()

    if "Sign Out" not in r2.text:
        raise RuntimeError("登入失敗，請檢查帳密或網站結構是否變動")

    # 保險起見再明確 GET 一次 graphing.aspx，確保拿到最新的 hidden fields
    # （若 requests 已經 follow redirect 到 graphing.aspx，這一步等於是重新整理，無害）
    r3 = s.get(f"{BASE}/graphing.aspx")
    r3.raise_for_status()
    soup3 = BeautifulSoup(r3.text, "html.parser")
    hidden_fields = get_hidden_fields(soup3)

    return s, hidden_fields, r3.text


# ---------------------------------------------------------------------------
# AJAX delta 解析（切分頁用）
# ---------------------------------------------------------------------------

_SEGMENT_HEADER_RE = re.compile(r"(\d+)\|([^|]*)\|([^|]*)\|")


def parse_ms_ajax_delta(raw: bytes) -> dict:
    """
    解析 Microsoft Ajax UpdatePanel 的 delta response。
    格式為重複的 length|type|id|content| 區塊。

    關鍵細節：ASP.NET 宣告的 length 是 content 的 UTF-8 byte 長度，不是字元數。
    如果 content 裡出現非 ASCII 字元，用 Python 字元數切割會跟宣告長度對不齊，
    所以這裡直接在 bytes 層級操作，最後才 decode 成字串。

    另外加了重新同步機制：如果照宣告長度切出來的邊界，下一個字元不是預期的
    '|'，代表對齊跑掉了（例如來源在某個傳輸環節做過換行符正規化），這時改成
    搜尋下一個「數字|型別|id|」的合法 segment header 來重新對齊。
    """
    result: dict = {}
    text = raw.decode("utf-8", errors="replace")
    full_bytes = raw
    pos = 0
    n = len(text)

    while pos < n:
        m = _SEGMENT_HEADER_RE.match(text, pos)
        if not m:
            break
        length = int(m.group(1))
        seg_type, seg_id = m.group(2), m.group(3)
        content_start = m.end()

        content_bytes_start = len(text[:content_start].encode("utf-8"))
        content_bytes = full_bytes[content_bytes_start:content_bytes_start + length]
        content = content_bytes.decode("utf-8", errors="replace")
        expected_next_char_pos = content_start + len(content)

        if expected_next_char_pos < n and text[expected_next_char_pos] == "|":
            result[(seg_type, seg_id)] = content
            pos = expected_next_char_pos + 1
            continue

        resync = _SEGMENT_HEADER_RE.search(text, content_start)
        if resync is None:
            result[(seg_type, seg_id)] = text[content_start:]
            break
        result[(seg_type, seg_id)] = text[content_start:resync.start()]
        pos = resync.start()

    return result


def extract_chart_js_from_full_page(html: str) -> str | None:
    """
    分頁 0（預設啟用分頁）的 Highcharts 設定，直接以一般 <script> 標籤
    內嵌在初次 GET graphing.aspx 的 HTML 裡，不需要（也不會）透過 AJAX 取得。
    這裡直接在整份 HTML 裡找第一個 Highcharts.Chart( 呼叫。
    """
    marker = "Highcharts.Chart("
    idx = html.find(marker)
    if idx == -1:
        return None
    return html[idx:]


def extract_chart_js_from_delta(delta: dict) -> str | None:
    """
    AJAX 切分頁後，Highcharts 設定包在 scriptStartupBlock|ScriptContentWithTags
    區塊裡，格式為 JSON：{"text": "var chart1 = new Highcharts.Chart({...})", ...}
    """
    decoder = json.JSONDecoder()
    for (seg_type, seg_id), content in delta.items():
        if seg_type == "scriptStartupBlock" and seg_id == "ScriptContentWithTags":
            try:
                # 用 raw_decode 而非 json.loads：content 尾端可能因為 resync
                # 抓進了下一段的一小段前綴（例如緊接著的 '|'），raw_decode
                # 只解析到合法 JSON 結束為止，不會因為多餘尾巴整段失敗。
                obj, _ = decoder.raw_decode(content)
            except json.JSONDecodeError:
                continue
            text = obj.get("text", "")
            if "Highcharts.Chart(" in text:
                return text
    return None


def switch_tab(session: requests.Session, hidden_fields: dict, tab_index: int) -> tuple[str | None, dict]:
    """切換到指定分頁，回傳 (chart js 字串或 None, 更新後的 hidden_fields)。"""
    ajax_headers = {
        "X-MicrosoftAjax": "Delta=true",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    payload = {
        "ToolkitScriptManager1": "UpdatePanel|tabMeters",
        "ToolkitScriptManager1_HiddenField": "",
        "HiddenField": "",
        "tabMeters_ClientState": hidden_fields.get("tabMeters_ClientState", ""),
        "__EVENTTARGET": "tabMeters",
        "__EVENTARGUMENT": f"activeTabChanged:{tab_index}",
        "__VIEWSTATE": hidden_fields["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": hidden_fields["__VIEWSTATEGENERATOR"],
        "__EVENTVALIDATION": hidden_fields["__EVENTVALIDATION"],
        "__ASYNCPOST": "true",
    }

    r = session.post(f"{BASE}/graphing.aspx", data=payload, headers=ajax_headers)
    r.raise_for_status()

    delta = parse_ms_ajax_delta(r.content)
    chart_js = extract_chart_js_from_delta(delta)

    new_fields = {
        "__VIEWSTATE": delta.get(("hiddenField", "__VIEWSTATE"), hidden_fields["__VIEWSTATE"]),
        "__VIEWSTATEGENERATOR": delta.get(("hiddenField", "__VIEWSTATEGENERATOR"), hidden_fields["__VIEWSTATEGENERATOR"]),
        "__EVENTVALIDATION": delta.get(("hiddenField", "__EVENTVALIDATION"), hidden_fields["__EVENTVALIDATION"]),
        "tabMeters_ClientState": delta.get(("hiddenField", "tabMeters_ClientState"), hidden_fields.get("tabMeters_ClientState", "")),
    }
    return chart_js, new_fields


_MONTH_NAME_TO_NUM = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def parse_month_year_from_title(title: str) -> tuple[int, int] | None:
    """從 'Daily Consumption During July 2026 for ...' 這種標題抓出 (月, 年)。"""
    m = re.search(r"During (\w+) (\d{4})", title)
    if not m:
        return None
    month_num = _MONTH_NAME_TO_NUM.get(m.group(1))
    if month_num is None:
        return None
    return month_num, int(m.group(2))


def is_next_month_button_disabled(delta: dict) -> bool:
    """
    檢查這次 AJAX 回應裡，nextMonth_btn 是不是被標成 disabled。
    網站會在「已經是最新可查詢的月份」時自動 disable 這顆按鈕，
    這是比自己比對日期更可靠的終止條件，用來避免換月迴圈失控。
    """
    panel_html = delta.get(("updatePanel", "UpdatePanel"), "")
    return bool(re.search(r'name="nextMonth_btn"[^>]*disabled', panel_html))


def switch_month(session: requests.Session, hidden_fields: dict) -> tuple[str | None, dict, bool]:
    """
    按一次「Next Month」。這是傳統 submit 按鈕語意（不是 __doPostBack），
    伺服器靠 payload 裡有沒有 nextMonth_btn 這個欄位判斷是哪顆按鈕被按了。
    回傳 (分頁0 最新月份的 chart js 或 None, 更新後的 hidden_fields, 是否已到最新月份)。
    """
    ajax_headers = {
        "X-MicrosoftAjax": "Delta=true",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    payload = {
        "ToolkitScriptManager1": "UpdatePanel|nextMonth_btn",
        "ToolkitScriptManager1_HiddenField": "",
        "HiddenField": "",
        "tabMeters_ClientState": hidden_fields.get("tabMeters_ClientState", ""),
        "__VIEWSTATE": hidden_fields["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": hidden_fields["__VIEWSTATEGENERATOR"],
        "__EVENTVALIDATION": hidden_fields["__EVENTVALIDATION"],
        "__ASYNCPOST": "true",
        "nextMonth_btn": "Next Month",
    }

    r = session.post(f"{BASE}/graphing.aspx", data=payload, headers=ajax_headers)
    r.raise_for_status()

    delta = parse_ms_ajax_delta(r.content)
    chart_js = extract_chart_js_from_delta(delta)
    disabled = is_next_month_button_disabled(delta)

    new_fields = {
        "__VIEWSTATE": delta.get(("hiddenField", "__VIEWSTATE"), hidden_fields["__VIEWSTATE"]),
        "__VIEWSTATEGENERATOR": delta.get(("hiddenField", "__VIEWSTATEGENERATOR"), hidden_fields["__VIEWSTATEGENERATOR"]),
        "__EVENTVALIDATION": delta.get(("hiddenField", "__EVENTVALIDATION"), hidden_fields["__EVENTVALIDATION"]),
        "tabMeters_ClientState": delta.get(("hiddenField", "tabMeters_ClientState"), hidden_fields.get("tabMeters_ClientState", "")),
    }
    return chart_js, new_fields, disabled


# ---------------------------------------------------------------------------
# Highcharts 設定字串解析
# ---------------------------------------------------------------------------

def find_balanced_braces(text: str, start: int) -> str:
    """從 start（指向 '{'）開始，抓出配對完整的 {...} 區塊。"""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("括號未配對，Highcharts 設定字串可能被截斷")


def parse_series_data(raw: str) -> list[dict]:
    """
    解析 series.data 陣列，處理兩種元素格式：
      - 純數字: 0.1240
      - 帶標記: {y: 0.1070, color: '#99B7DB'}
    用手動掃描而非直接 split(',')，因為物件內部也有逗號。
    """
    items, depth, current = [], 0, ""
    for ch in raw.strip():
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        items.append(current.strip())

    parsed = []
    for it in items:
        if it.startswith("{"):
            y = re.search(r"y:\s*([\-0-9.]+)", it)
            color = re.search(r"color:\s*'([^']*)'", it)
            parsed.append({
                "value": float(y.group(1)) if y else None,
                "flagged": bool(color),
            })
        else:
            try:
                parsed.append({"value": float(it), "flagged": False})
            except ValueError:
                parsed.append({"value": None, "flagged": False})
    return parsed


def parse_chart(config: str) -> dict:
    def field(pattern: str, default=None):
        m = re.search(pattern, config, re.DOTALL)
        return m.group(1) if m else default

    meter_index = field(r"renderTo:\s*'chartContainer_(\d+)'")
    title = field(r"title:\s*\{\s*text:\s*'([^']*)'")
    subtitle = field(r"subtitle:\s*\{\s*text:\s*'([^']*)'")
    unit = field(r"yAxis:\s*\{\s*title:\s*\{\s*text:\s*'([^']*)'")
    categories_raw = field(r"categories:\s*\[([^\]]*)\]", "") or ""
    categories = [c.strip().strip("'") for c in categories_raw.split(",") if c.strip()]

    series = []
    for name, data_raw in re.findall(
        r"name:\s*'([^']*)'\s*,\s*(?:color:\s*'[^']*'\s*,\s*)?data:\s*\[(.*?)\]",
        config, re.DOTALL
    ):
        series.append({"name": name, "values": parse_series_data(data_raw)})

    return {
        "meter_index": int(meter_index) if meter_index is not None else None,
        "title": title,
        "reading_summary": subtitle,
        "unit": unit,
        "categories": categories,
        "series": series,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def switch_tab_with_retry(session: requests.Session, hidden_fields: dict, tab_index: int, meter_id: str) -> tuple[str | None, dict]:
    """包一層重試：AJAX 請求偶發失敗（逾時、暫時性 5xx）時，重試幾次再放棄。"""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 2):  # 第一次嘗試 + MAX_RETRIES 次重試
        try:
            chart_js, new_fields = switch_tab(session, hidden_fields, tab_index)
            if chart_js is not None:
                return chart_js, new_fields
            last_error = RuntimeError("回應中沒有找到 Highcharts 設定（結構可能跟預期不同）")
        except requests.RequestException as e:
            last_error = e

        if attempt <= MAX_RETRIES:
            print(
                f"[WARN] 分頁 {tab_index} ({meter_id}) 第 {attempt} 次嘗試失敗："
                f"{last_error}，{RETRY_BACKOFF_SECONDS} 秒後重試",
                file=sys.stderr,
            )
            time.sleep(RETRY_BACKOFF_SECONDS)

    print(f"[ERROR] 分頁 {tab_index} ({meter_id}) 重試 {MAX_RETRIES} 次後仍失敗：{last_error}", file=sys.stderr)
    return None, hidden_fields


def _parse_chart_from_js(chart_js: str, idx: int, meter_id: str) -> dict | None:
    try:
        chart_start = chart_js.index("Highcharts.Chart(")
        brace_start = chart_js.index("{", chart_start)
        config_str = find_balanced_braces(chart_js, brace_start)
        return parse_chart(config_str)
    except (ValueError, IndexError) as e:
        print(f"[ERROR] 分頁 {idx} ({meter_id}) 解析 Highcharts 設定失敗：{e}", file=sys.stderr)
        return None


def ensure_current_month(session: requests.Session, hidden_fields: dict, tab0_chart_js: str) -> tuple[str, dict]:
    """
    確認分頁 0 目前顯示的月份是不是「今天所在的月份」，不是的話按 Next Month
    直到追上為止。網站預設停在上次瀏覽的月份（不是自動對齊今天），實測發現
    的行為：換月是 session 全域狀態，切一次後其他分頁也會自動跟著顯示新月份，
    所以只需要在這裡處理一次，不用對每個分頁各自換月。

    回傳：(換月後、分頁0 最新月份的 chart js, 更新後的 hidden_fields)
    """
    today = datetime.now()
    chart_js = tab0_chart_js

    for _ in range(MAX_MONTH_SWITCHES):
        chart = _parse_chart_from_js(chart_js, 0, "24203836 (換月檢查)")
        if chart is None or chart.get("title") is None:
            print("[WARN] 換月檢查時無法解析標題，放棄自動換月，使用目前資料", file=sys.stderr)
            break

        parsed = parse_month_year_from_title(chart["title"])
        if parsed is None:
            print(f"[WARN] 標題格式跟預期不同，無法解析月份：{chart['title']!r}", file=sys.stderr)
            break

        month, year = parsed
        if (year, month) >= (today.year, today.month):
            # 已經追到當月（或者網站本來就不可能給未來月份，>= 是保險寫法）
            break

        print(f"[INFO] 目前顯示 {year}/{month:02d}，不是當月（{today.year}/{today.month:02d}），送出 Next Month", file=sys.stderr)
        new_chart_js, hidden_fields, disabled = switch_month(session, hidden_fields)
        if new_chart_js is None:
            print("[WARN] Next Month 請求沒有回傳新的圖表設定，停止換月", file=sys.stderr)
            break
        chart_js = new_chart_js
        if disabled:
            # 網站自己回報「已經是最新月份，不能再往後了」
            break

    return chart_js, hidden_fields


def fetch_all_meters(service_address: str, account_number: str) -> list[dict]:
    session, hidden_fields, initial_html = login(service_address, account_number)

    tab0_chart_js = extract_chart_js_from_full_page(initial_html)
    if tab0_chart_js is None:
        raise RuntimeError("初次頁面裡找不到分頁 0 的 Highcharts 設定，網站結構可能已變動")

    tab0_chart_js, hidden_fields = ensure_current_month(session, hidden_fields, tab0_chart_js)

    charts = []
    for idx, meter_id in enumerate(TAB_METER_IDS):
        if TAB_SOURCE_TYPES.get(meter_id) is None:
            # Phase 1 確認不追蹤的分頁（T0005518），連請求都不用送
            continue

        if idx == 0:
            # 分頁 0 的資料已經在 ensure_current_month() 裡處理到最新月份了，
            # 不需要（也不會成功）再送一次 activeTabChanged:0 的 AJAX 請求。
            chart_js = tab0_chart_js
        else:
            chart_js, hidden_fields = switch_tab_with_retry(session, hidden_fields, idx, meter_id)
            if chart_js is None:
                # 這個分頁重試後仍失敗，記錄下來但不中斷其他分頁的抓取
                continue

        chart = _parse_chart_from_js(chart_js, idx, meter_id)
        if chart is None:
            continue

        chart["meter_id"] = meter_id
        charts.append(chart)

    return charts


def main() -> None:
    parser = argparse.ArgumentParser(description="CARMA 用電/用水/暖氣資料抓取")
    parser.add_argument("--gcp-project", default=os.environ.get("GCP_PROJECT"), help="讀取 Secret Manager 用，正式排程時需要")
    parser.add_argument("--service-address", default=None, help="手動測試用覆蓋值，留空則從 Secret Manager 讀取")
    parser.add_argument("--account-number", default=None, help="手動測試用覆蓋值，留空則從 Secret Manager 讀取")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    service_address = args.service_address
    account_number = args.account_number

    if not service_address or not account_number:
        if not args.gcp_project:
            print(
                "錯誤：--service-address / --account-number 沒有全部給齊，"
                "需要 --gcp-project 才能從 Secret Manager 讀取剩下的憑證",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            from secrets_manager import load_config
            config = load_config(args.gcp_project)
        except Exception as e:
            print(f"[FATAL] 從 Secret Manager 讀取憑證失敗：{e}", file=sys.stderr)
            sys.exit(1)
        service_address = service_address or config["carma_service_address"]
        account_number = account_number or config["carma_account_number"]

    conn = init_db(args.db_path)
    try:
        try:
            charts = fetch_all_meters(service_address, account_number)
        except (RuntimeError, requests.RequestException) as e:
            # 登入失敗、網站結構跟預期不同等整體性錯誤，印出明確訊息方便 cron log 判斷
            print(f"[FATAL] 抓取流程整體失敗：{e}", file=sys.stderr)
            sys.exit(1)

        if not charts:
            print("[WARN] 沒有成功抓到任何分頁的資料", file=sys.stderr)
            sys.exit(1)

        for chart in charts:
            save_chart_data(conn, chart["meter_id"], chart)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
