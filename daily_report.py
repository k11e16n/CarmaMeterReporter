#!/usr/bin/env python3
"""
CarmaMeterReporter 每日報告產生器。

讀取 carma_scraper.py 寫入的 SQLite 資料，組出四個追蹤項目（熱水/冷水/
冷暖氣/日常用電）各自最新一筆有效資料，套用 report_calc.py 的費用估算
公式，加上 gemini_summary.py 生成的一句觀察，最後用 line_push.py 推播
到 LINE 群組。

刻意跟 carma_scraper.py 分開執行：抓資料失敗跟推播失敗要能分開判斷是
哪一段出問題，也方便「重推今天的報告，不重新抓資料」這種情境。

已知的簡化（見 report_calc.py 開頭註解，這裡不重複）：
- 「最新一筆有效資料」用 value > 0 判斷，如果某一天真實用量剛好是 0，
  會被誤判成「還沒回報」而跳過，抓到更早一天當作最新。考量到資料本來
  就有 2-3 天回報延遲，這個誤差的實務影響很小，不特別處理。
- 本月統計（平均/最高/最低）不依賴網站的 Average 線（我們沒有存那個
  值），改成自己對「月初到最新有效日期」這個範圍內的資料算平均/最高/
  最低，這段範圍內的資料不做 value > 0 過濾（合法的 0 用量天要算進去）。

用法：
    python3 daily_report.py --db-path carma_readings.db \\
        --line-token "..." --line-to "..." --gemini-key "..." [--dry-run]

    --dry-run 只印出組好的報告內容，不呼叫 LINE API（Gemini 觀察句仍然
    會呼叫，因為那是報告內容的一部分，dry-run 是為了不消耗 LINE 推播額度）
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from gemini_summary import generate_daily_observation
from line_push import build_flex_bubble, build_flex_carousel_message, push_line_message
from report_calc import (
    daily_electricity_cost,
    heat_cooling_cost,
    monthly_total_estimate,
    water_cost,
)

# meter_id -> (顯示名稱, source_type)
# source_type 要跟 carma_scraper.py 的 TAB_SOURCE_TYPES 一致
METERS = {
    "24203836": ("熱水", "hot_water"),
    "24206195": ("冷水", "cold_water"),
    "86085616": ("冷暖氣", "heat_cooling"),
    "T0005287": ("日常用電", "electricity"),
}


# ---------------------------------------------------------------------------
# 資料庫查詢
# ---------------------------------------------------------------------------

def get_latest_reading(conn: sqlite3.Connection, meter_id: str) -> tuple[str, float, str] | None:
    """回傳 (reading_date, value, unit) 或 None（完全沒有資料時）。"""
    row = conn.execute(
        "SELECT reading_date, value, unit FROM utility_readings "
        "WHERE meter_id = ? AND value > 0 "
        "ORDER BY reading_date DESC LIMIT 1",
        (meter_id,),
    ).fetchone()
    return row


def get_month_stats(conn: sqlite3.Connection, meter_id: str, as_of_date: str) -> dict | None:
    """月初到 as_of_date（含）的平均/最高/最低，包含真實的 0 用量天。"""
    month_start = as_of_date[:8] + "01"
    rows = conn.execute(
        "SELECT reading_date, value FROM utility_readings "
        "WHERE meter_id = ? AND reading_date BETWEEN ? AND ? "
        "ORDER BY reading_date",
        (meter_id, month_start, as_of_date),
    ).fetchall()
    if not rows:
        return None
    values = [v for _, v in rows]
    max_date, max_val = max(rows, key=lambda r: r[1])
    min_date, min_val = min(rows, key=lambda r: r[1])
    return {
        "avg": sum(values) / len(values),
        "max": max_val,
        "max_date": max_date,
        "min": min_val,
        "min_date": min_date,
    }


def get_month_cumulative(conn: sqlite3.Connection, meter_id: str, as_of_date: str) -> float:
    """月初到 as_of_date（含）的用量總和，用來算月累積費用。"""
    month_start = as_of_date[:8] + "01"
    row = conn.execute(
        "SELECT COALESCE(SUM(value), 0) FROM utility_readings "
        "WHERE meter_id = ? AND reading_date BETWEEN ? AND ?",
        (meter_id, month_start, as_of_date),
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# 組報告資料
# ---------------------------------------------------------------------------

def build_report_items(conn: sqlite3.Connection) -> dict:
    """
    回傳 {source_type: {label, unit, latest_date, latest_value, stats,
    cumulative, daily_cost, month_num}}，沒有資料的項目不會出現在結果裡。
    """
    items = {}
    for meter_id, (label, source_type) in METERS.items():
        latest = get_latest_reading(conn, meter_id)
        if latest is None:
            print(f"[WARN] {label} ({meter_id}) 沒有任何有效資料，本次報告略過", file=sys.stderr)
            continue

        latest_date, latest_value, unit = latest
        stats = get_month_stats(conn, meter_id, latest_date)
        cumulative = get_month_cumulative(conn, meter_id, latest_date)
        month_num = int(latest_date[5:7])

        if source_type in ("hot_water", "cold_water"):
            daily_cost = water_cost(source_type, latest_value)
        elif source_type == "heat_cooling":
            daily_cost = heat_cooling_cost(latest_value)
        elif source_type == "electricity":
            cumulative_yesterday = cumulative - latest_value
            daily_cost = daily_electricity_cost(cumulative, cumulative_yesterday, month_num)
        else:
            daily_cost = 0.0

        items[source_type] = {
            "label": label,
            "unit": unit,
            "latest_date": latest_date,
            "latest_value": latest_value,
            "stats": stats,
            "cumulative": cumulative,
            "daily_cost": daily_cost,
            "month_num": month_num,
        }

    return items


def build_monthly_total(items: dict) -> dict | None:
    """需要電費項目存在才能算（月份要用電費那筆的月份判斷分級門檻）。"""
    if "electricity" not in items:
        return None

    return monthly_total_estimate(
        electricity_cumulative_kwh=items["electricity"]["cumulative"],
        cold_water_cumulative_m3=items.get("cold_water", {}).get("cumulative", 0.0),
        hot_water_cumulative_m3=items.get("hot_water", {}).get("cumulative", 0.0),
        heat_cooling_cumulative_kwh=items.get("heat_cooling", {}).get("cumulative", 0.0),
        month=items["electricity"]["month_num"],
    )


# ---------------------------------------------------------------------------
# 組 LINE 訊息
# ---------------------------------------------------------------------------

def build_bubbles(items: dict) -> list[dict]:
    bubbles = []
    for source_type, data in items.items():
        stats = data["stats"]
        lines = [f"最新（{data['latest_date']}）：{data['latest_value']:.3f} {data['unit']}"]
        if stats:
            lines.append(
                f"本月平均 {stats['avg']:.3f} ｜ 最高 {stats['max']:.3f}（{stats['max_date']}）"
                f" ｜ 最低 {stats['min']:.3f}（{stats['min_date']}）"
            )
        lines.append(f"當日費用估算：${data['daily_cost']:.2f}")
        bubbles.append(build_flex_bubble(data["label"], lines))
    return bubbles


def build_summary_bubble(observation: str | None, total: dict | None) -> dict:
    """
    獨立的第五張卡片：Gemini 觀察句 + 本月累積估算。
    獨立成卡片而不是掛在某張追蹤項目卡片的 footer 上，是因為掛哪張純粹
    看 METERS 這個 dict 的排列順序，跟內容本身無關，容易讓人誤以為
    這句觀察是針對「掛著的那張卡片」講的。
    """
    lines = []
    if observation:
        lines.append(observation)

    if total is None:
        lines.append("本月累積估算：資料不足，無法計算")
    else:
        lines.append(f"本月累積估算：${total['total']:.2f}")
        lines.append(
            f"電費 ${total['electricity_after_rebate']:.2f} ｜ "
            f"水費 ${total['cold_water'] + total['hot_water']:.2f} ｜ "
            f"暖氣 ${total['heat_cooling']:.2f}"
        )
        lines.append(f"固定費用（Delivery+其他）：${total['delivery'] + total['other']:.2f}")

    return build_flex_bubble("本月摘要", lines)


def build_gemini_readings(items: dict) -> list[dict]:
    readings = []
    for data in items.values():
        stats = data["stats"]
        if not stats:
            continue
        readings.append(
            {
                "label": data["label"],
                "unit": data["unit"],
                "today_date": data["latest_date"],
                "today_value": data["latest_value"],
                "month_avg": round(stats["avg"], 3),
                "month_max": stats["max"],
                "month_max_date": stats["max_date"],
                "month_min": stats["min"],
                "month_min_date": stats["min_date"],
            }
        )
    return readings


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="CarmaMeterReporter 每日報告")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--gcp-project", default=os.environ.get("GCP_PROJECT"), help="讀取 Secret Manager 用，正式排程時需要")
    parser.add_argument("--line-token", default=None, help="手動測試用覆蓋值，留空則從 Secret Manager 讀取")
    parser.add_argument("--line-to", default=None, help="手動測試用覆蓋值，留空則從 Secret Manager 讀取")
    parser.add_argument("--gemini-key", default=None, help="手動測試用覆蓋值，留空則從 Secret Manager 讀取")
    parser.add_argument("--dry-run", action="store_true", help="只印出報告內容，不呼叫 LINE 推播")
    args = parser.parse_args()

    line_token = args.line_token
    line_to = args.line_to
    gemini_key = args.gemini_key

    if not (line_token and line_to and gemini_key):
        if not args.gcp_project:
            print(
                "錯誤：--line-token / --line-to / --gemini-key 沒有全部給齊，"
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
        line_token = line_token or config["line_channel_access_token"]
        line_to = line_to or config["line_group_id"]
        gemini_key = gemini_key or config["gemini_api_key"]

    conn = sqlite3.connect(args.db_path)
    try:
        items = build_report_items(conn)
    finally:
        conn.close()

    if not items:
        print("[FATAL] 沒有任何項目有資料，無法產生報告", file=sys.stderr)
        sys.exit(1)

    total = build_monthly_total(items)
    gemini_readings = build_gemini_readings(items)
    observation = generate_daily_observation(gemini_key, gemini_readings)

    bubbles = build_bubbles(items)
    summary_bubble = build_summary_bubble(observation, total)
    bubbles.append(summary_bubble)

    message = build_flex_carousel_message("CarmaMeterReporter 每日報告", bubbles)

    if args.dry_run:
        import json
        print(json.dumps(message, ensure_ascii=False, indent=2))
        print("\n[dry-run] 沒有實際推播", file=sys.stderr)
        return

    push_line_message(line_token, line_to, [message])
    print("[OK] 每日報告推播完成")


if __name__ == "__main__":
    main()
