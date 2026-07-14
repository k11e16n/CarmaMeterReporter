#!/usr/bin/env python3
"""
CarmaMeterReporter 費用估算邏輯。

刻意跟資料庫查詢、推播、Gemini 全部解耦——這裡只有純函式（輸入用量數字，
輸出金額），方便獨立測試，也方便之後費率變動時只改這一支檔案。

費率來源：2026-05 帳單反推 + 交叉驗證（討論紀錄見 CarmaMeterReporter 專案）。

已知的限制（不是 bug，是刻意的簡化，見專案討論）：
- Tier 2 電價（超過分級門檻的部分）只有交叉比對驗證，沒有帳單實際驗證過，
  但用戶月用量遠低於門檻（100~200 kWh vs. 600 kWh 門檻），實務上幾乎不會
  用到這個分支。
- Rebate 的計算基礎少算了 Regulatory Charges（因為它被打包進「其他」常數
  裡了），會讓 rebate 少算約 $0.14/月，金額小到可以忽略，不為此拆開常數
  增加複雜度。
- Delivery、其他（Regulatory + Tax Recovery + HST）都是固定常數，取自單一
  張帳單，不會隨用量變動。等累積更多帳單後可以校正。
"""

from __future__ import annotations

WATER_RATES = {
    "cold_water": 7.030200,
    "hot_water": 8.607438,
}
HEAT_COOLING_RATE = 0.048420

ELECTRICITY_TIER1_RATE = 0.12    # 已用 2026-05 帳單驗證
ELECTRICITY_TIER2_RATE = 0.142   # 信心較低，未經帳單驗證，僅交叉比對

SUMMER_MONTHS = {5, 6, 7, 8, 9, 10}
SUMMER_THRESHOLD_KWH = 600.0
WINTER_THRESHOLD_KWH = 1000.0

ELECTRICITY_REBATE_RATE = 0.235  # 已用 2026-05 帳單驗證

DELIVERY_FLAT = 27.82   # 固定值，沿用 2026-05 帳單
OTHER_FLAT = 12.76      # Regulatory + Tax Recovery + HST 加總，同上


def electricity_threshold_for_month(month: int) -> float:
    """安大略 Tiered 電價的分級門檻：夏季（5-10月）較低，冬季較高。"""
    return SUMMER_THRESHOLD_KWH if month in SUMMER_MONTHS else WINTER_THRESHOLD_KWH


def electricity_tiered_charge(cumulative_kwh: float, threshold: float) -> float:
    """
    分級電費：門檻內的用量套 Tier 1 費率，超過門檻的部分套 Tier 2 費率。
    cumulative_kwh 是「從月初累積到某一天」的總用量，不是單日用量——
    分級是看累積量，不是看單日量。
    """
    tier1_kwh = min(cumulative_kwh, threshold)
    tier2_kwh = max(0.0, cumulative_kwh - threshold)
    return tier1_kwh * ELECTRICITY_TIER1_RATE + tier2_kwh * ELECTRICITY_TIER2_RATE


def water_cost(source_type: str, usage_m3: float) -> float:
    return usage_m3 * WATER_RATES[source_type]


def heat_cooling_cost(usage_kwh: float) -> float:
    return usage_kwh * HEAT_COOLING_RATE


def daily_electricity_cost(
    cumulative_through_today: float,
    cumulative_through_yesterday: float,
    month: int,
) -> float:
    """
    當日電費的分級貢獻（不含 rebate——rebate 只在月累積估算時套用，
    daily 這裡只反映當天實際增加的用量對應到的分級費用）。
    """
    threshold = electricity_threshold_for_month(month)
    return (
        electricity_tiered_charge(cumulative_through_today, threshold)
        - electricity_tiered_charge(cumulative_through_yesterday, threshold)
    )


def monthly_electricity_cost_with_rebate(cumulative_kwh: float, month: int) -> dict:
    """回傳 rebate 前後的電費，讓呼叫端可以決定要不要分開顯示。"""
    threshold = electricity_threshold_for_month(month)
    before_rebate = electricity_tiered_charge(cumulative_kwh, threshold)
    rebate = (before_rebate + DELIVERY_FLAT) * ELECTRICITY_REBATE_RATE
    return {
        "before_rebate": before_rebate,
        "rebate": rebate,
        "after_rebate": before_rebate - rebate,
    }


def monthly_total_estimate(
    electricity_cumulative_kwh: float,
    cold_water_cumulative_m3: float,
    hot_water_cumulative_m3: float,
    heat_cooling_cumulative_kwh: float,
    month: int,
) -> dict:
    """本月累積總金額估算，四大項用量各自的月累積 + 兩個固定月費常數。"""
    elec = monthly_electricity_cost_with_rebate(electricity_cumulative_kwh, month)
    cold_water = water_cost("cold_water", cold_water_cumulative_m3)
    hot_water = water_cost("hot_water", hot_water_cumulative_m3)
    heat_cooling = heat_cooling_cost(heat_cooling_cumulative_kwh)

    total = (
        elec["after_rebate"]
        + cold_water
        + hot_water
        + heat_cooling
        + DELIVERY_FLAT
        + OTHER_FLAT
    )

    return {
        "electricity_before_rebate": elec["before_rebate"],
        "electricity_rebate": elec["rebate"],
        "electricity_after_rebate": elec["after_rebate"],
        "cold_water": cold_water,
        "hot_water": hot_water,
        "heat_cooling": heat_cooling,
        "delivery": DELIVERY_FLAT,
        "other": OTHER_FLAT,
        "total": total,
    }
