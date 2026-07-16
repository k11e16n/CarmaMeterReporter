#!/usr/bin/env python3
"""
一次性回補歷史資料，把 carma_readings.db 補到指定的起始月份為止。

這不是每日排程的一部分，手動執行一次就好——跑完之後，之後 carma_scraper.py
的正常每日執行會自然接續往後補新資料，不需要再跑這支腳本，除非你想拉更早
的歷史。

技術背景（跟 carma_scraper.py 的邏輯共用，但這裡多了「往回翻月份」這件事，
是全新、沒有實測驗證過的操作組合，第一次執行請盯著看有沒有 WARN/ERROR）：

- 分頁 0 只有在「目前不是 active tab」時，切換才會真的觸發渲染
  （這是 carma_scraper.py 已經驗證過的行為：登入後預設 active tab 是 0，
  這時候切分頁 0 是 no-op；但如果目前 active tab 是別的分頁，切到分頁 0
  屬於真正的索引變化，應該會正常運作——這個推論本身沒有實測過，第一次跑
  要注意)
- 按 Prev Month，會重新渲染「目前 active 的分頁」，不是固定重新渲染分頁 0
  （這點我們藉此設計成：每個月抓完 4 個分頁後，目前 active 分頁會停在
  分頁 3，這時候按 Prev Month，剛好順便把新月份分頁 3 的資料也拿到了，
  不用再多送一次請求）

用法：
    python3 backfill_history.py --start-year-month 2026-05 \\
        --gcp-project mypixelchatroom

    或手動傳憑證（測試用）：
    python3 backfill_history.py --start-year-month 2026-05 \\
        --service-address "..." --account-number "..."
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import requests

from carma_scraper import (
    BASE,
    TAB_METER_IDS,
    TAB_SOURCE_TYPES,
    ensure_current_month,
    extract_chart_js_from_delta,
    extract_chart_js_from_full_page,
    find_balanced_braces,
    init_db,
    login,
    parse_chart,
    parse_month_year_from_title,
    parse_ms_ajax_delta,
    save_chart_data,
    switch_tab_with_retry,
)

MAX_MONTHS_BACK = 36  # 防呆上限，避免網站行為跟預期不同時無限迴圈


def is_prev_month_button_disabled(delta: dict) -> bool:
    """已經到最舊的可查詢月份時，網站會把 prevMonth_btn 標成 disabled。"""
    panel_html = delta.get(("updatePanel", "UpdatePanel"), "")
    return bool(re.search(r'name="prevMonth_btn"[^>]*disabled', panel_html))


def switch_prev_month(session: requests.Session, hidden_fields: dict) -> tuple[str | None, dict, bool]:
    """
    按一次 Prev Month。這是跟 carma_scraper.py 的 switch_month()（Next Month）
    對稱的操作，一樣是傳統 submit 按鈕語意，不是 __doPostBack。

    回傳 (目前 active 分頁在新月份的 chart js 或 None, 更新後的 hidden_fields,
    是否已經到最舊月份)。
    """
    ajax_headers = {
        "X-MicrosoftAjax": "Delta=true",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    payload = {
        "ToolkitScriptManager1": "UpdatePanel|prevMonth_btn",
        "ToolkitScriptManager1_HiddenField": "",
        "HiddenField": "",
        "tabMeters_ClientState": hidden_fields.get("tabMeters_ClientState", ""),
        "__VIEWSTATE": hidden_fields["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": hidden_fields["__VIEWSTATEGENERATOR"],
        "__EVENTVALIDATION": hidden_fields["__EVENTVALIDATION"],
        "__ASYNCPOST": "true",
        "prevMonth_btn": "Prev Month",
    }

    r = session.post(f"{BASE}/graphing.aspx", data=payload, headers=ajax_headers)
    r.raise_for_status()

    delta = parse_ms_ajax_delta(r.content)
    chart_js = extract_chart_js_from_delta(delta)
    disabled = is_prev_month_button_disabled(delta)

    new_fields = {
        "__VIEWSTATE": delta.get(("hiddenField", "__VIEWSTATE"), hidden_fields["__VIEWSTATE"]),
        "__VIEWSTATEGENERATOR": delta.get(("hiddenField", "__VIEWSTATEGENERATOR"), hidden_fields["__VIEWSTATEGENERATOR"]),
        "__EVENTVALIDATION": delta.get(("hiddenField", "__EVENTVALIDATION"), hidden_fields["__EVENTVALIDATION"]),
        "tabMeters_ClientState": delta.get(("hiddenField", "tabMeters_ClientState"), hidden_fields.get("tabMeters_ClientState", "")),
    }
    return chart_js, new_fields, disabled


def _save_if_valid(conn, idx: int, chart_js: str | None) -> None:
    meter_id = TAB_METER_IDS[idx]
    if TAB_SOURCE_TYPES.get(meter_id) is None:
        return
    if chart_js is None:
        print(f"[WARN] 分頁 {idx} ({meter_id}) 沒有資料可存", file=sys.stderr)
        return
    try:
        chart_start = chart_js.index("Highcharts.Chart(")
        brace_start = chart_js.index("{", chart_start)
        config_str = find_balanced_braces(chart_js, brace_start)
        chart = parse_chart(config_str)
    except (ValueError, IndexError) as e:
        print(f"[ERROR] 分頁 {idx} ({meter_id}) 解析失敗：{e}", file=sys.stderr)
        return
    save_chart_data(conn, meter_id, chart)


def backfill(service_address: str, account_number: str, db_path: str, start_year: int, start_month: int) -> None:
    session, hidden_fields, initial_html = login(service_address, account_number)
    conn = init_db(db_path)

    tab0_chart_js = extract_chart_js_from_full_page(initial_html)
    if tab0_chart_js is None:
        raise RuntimeError("初次頁面裡找不到分頁 0 的 Highcharts 設定，網站結構可能已變動")

    # 全新 session 預設停留在網站自己記得的月份（實測是六月），不是今天實際
    # 所在的月份——回補要從「真正的當月」開始往回走，不然最新的月份會被漏掉
    tab0_chart_js, hidden_fields = ensure_current_month(session, hidden_fields, tab0_chart_js)

    # 上一輪按 Prev Month 時，順便拿到的「新月份、分頁3」資料，第一輪還沒有
    carried_tab3_chart_js: str | None = None

    for month_iteration in range(MAX_MONTHS_BACK):
        if carried_tab3_chart_js is not None:
            # 目前 active tab = 3（上一輪按 Prev Month 帶過來的），
            # 先切回分頁 0 —— 這是「真正的索引變化」（3→0），不是
            # 「切到已經 active 的分頁」那種 no-op 情況
            tab0_chart_js, hidden_fields = switch_tab_with_retry(session, hidden_fields, 0, TAB_METER_IDS[0])
            tab3_chart_js = carried_tab3_chart_js
        else:
            tab3_chart_js = None  # 第一輪還沒抓過分頁3，等下面依序切過去拿

        chart0 = parse_chart(
            find_balanced_braces(
                tab0_chart_js, tab0_chart_js.index("{", tab0_chart_js.index("Highcharts.Chart("))
            )
        )
        parsed = parse_month_year_from_title(chart0["title"])
        if parsed is None:
            print(f"[WARN] 標題格式跟預期不同，無法解析月份，停止回補：{chart0['title']!r}", file=sys.stderr)
            break
        month, year = parsed
        print(f"[INFO] 正在處理 {year}/{month:02d} ...")

        # 依序切到分頁 1、2，分頁 3 如果已經有（carried），就不用重抓
        tab1_chart_js, hidden_fields = switch_tab_with_retry(session, hidden_fields, 1, TAB_METER_IDS[1])
        tab2_chart_js, hidden_fields = switch_tab_with_retry(session, hidden_fields, 2, TAB_METER_IDS[2])
        if tab3_chart_js is None:
            tab3_chart_js, hidden_fields = switch_tab_with_retry(session, hidden_fields, 3, TAB_METER_IDS[3])

        _save_if_valid(conn, 0, tab0_chart_js)
        _save_if_valid(conn, 1, tab1_chart_js)
        _save_if_valid(conn, 2, tab2_chart_js)
        _save_if_valid(conn, 3, tab3_chart_js)

        if (year, month) <= (start_year, start_month):
            print(f"[INFO] 已經到達起始月份 {start_year}/{start_month:02d}，回補完成")
            break

        # 目前 active tab = 3，按 Prev Month 會順便重新渲染分頁3在新月份的資料
        new_tab3_chart_js, hidden_fields, disabled = switch_prev_month(session, hidden_fields)
        if new_tab3_chart_js is None:
            print("[WARN] Prev Month 沒有回傳資料，停止回補", file=sys.stderr)
            break
        carried_tab3_chart_js = new_tab3_chart_js

        if disabled:
            print("[INFO] 網站回報已經是最舊的可查詢月份，回補到此為止（可能還沒到達你指定的起始月份）")
            # 這是最後一輪了，把這個月的資料也存起來
            tab0_chart_js, hidden_fields = switch_tab_with_retry(session, hidden_fields, 0, TAB_METER_IDS[0])
            tab1_chart_js, hidden_fields = switch_tab_with_retry(session, hidden_fields, 1, TAB_METER_IDS[1])
            tab2_chart_js, hidden_fields = switch_tab_with_retry(session, hidden_fields, 2, TAB_METER_IDS[2])
            _save_if_valid(conn, 0, tab0_chart_js)
            _save_if_valid(conn, 1, tab1_chart_js)
            _save_if_valid(conn, 2, tab2_chart_js)
            _save_if_valid(conn, 3, carried_tab3_chart_js)
            break
    else:
        print(f"[WARN] 已經跑了 {MAX_MONTHS_BACK} 個月還沒到達起始月份，防呆上限先停止", file=sys.stderr)

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="CarmaMeterReporter 歷史資料回補（一次性）")
    parser.add_argument("--start-year-month", required=True, help="回補到這個月為止，格式 YYYY-MM，例如 2026-05")
    parser.add_argument("--gcp-project", default=os.environ.get("GCP_PROJECT"))
    parser.add_argument("--service-address", default=None)
    parser.add_argument("--account-number", default=None)
    parser.add_argument("--db-path", default="carma_readings.db")
    args = parser.parse_args()

    start_year, start_month = (int(x) for x in args.start_year_month.split("-"))

    service_address = args.service_address
    account_number = args.account_number
    if not service_address or not account_number:
        if not args.gcp_project:
            print("錯誤：需要 --gcp-project 才能從 Secret Manager 讀取憑證", file=sys.stderr)
            sys.exit(1)
        from secrets_manager import load_config
        config = load_config(args.gcp_project)
        service_address = service_address or config["carma_service_address"]
        account_number = account_number or config["carma_account_number"]

    backfill(service_address, account_number, args.db_path, start_year, start_month)


if __name__ == "__main__":
    main()
