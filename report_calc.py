#!/usr/bin/env python3
"""
CarmaMeterReporter 費用估算邏輯。

刻意跟資料庫查詢、推播、Gemini 全部解耦——這裡只有純函式（輸入用量數字，
輸出金額），方便獨立測試，也方便之後費率變動時只改這一支檔案。

費率來源：用 2026-05、2026-06、2026-07 三張帳單交叉驗證（討論紀錄見
CarmaMeterReporter 專案）。三個資料點才能真正驗證一條公式是否成立——
兩點永遠連得出一條線，那不是驗證，是代數恆等式，這點在專案討論中曾經
踩過一次坑。

## 已驗證、可信的線性公式（三張帳單三次獨立驗證，比率幾乎一致）
- Regulatory Charges ≈ 0.0055 / kWh
- Utility Sales Tax Recovery ≈ 0.0187 / kWh

## 已驗證、可信的固定值
- HST：$5.04（五月帳單的 $10.18 是被 Occupancy/Paper/Deposit 這些一次性
  費用一起課稅拉高的，六、七月排除這些干擾後穩定在 $5.04）
- 水費、暖氣費率的「每度計算」邏輯：驗證通過，沒有變過

## 沒有公式、必須手動維護的兩個值
- Delivery：驗證了「固定 + 每度線性」這個假設，用五、六月推出的公式去
  預測七月，差了 $2.12；用六、七月的公式去預測五月，差了 $12.93——
  代表 Delivery 不是固定+線性關係，是 Toronto Hydro/CARMA 不定期調整的
  數字，沒有規律可循。
- Heat and Cooling Energy 費率：連續三個月都在漲（0.048420 → 0.063810
  → 0.080190），同樣沒有規律。

**這兩個值只能抓「最近一期帳單的實際數字」，不是算出來的**——收到新帳單
時，麻煩手動更新下面的 DELIVERY_FLAT 跟 HEAT_COOLING_RATE，並更新
LATEST_BILL_DATE 這行註解，方便之後回頭查是從哪張帳單抄的。

已知的限制（不是 bug，是刻意的簡化）：
- Tier 2 電價（超過分級門檻的部分）只有交叉比對驗證，沒有帳單實際驗證過，
  但用戶月用量遠低於門檻（100~400 kWh vs. 600 kWh 門檻），實務上幾乎不會
  用到這個分支。
"""

from __future__ import annotations

# --- 抄自最新一期帳單，收到新帳單時記得更新這三個值跟下面的日期 ---
# LATEST_BILL_DATE = 2026-08-27（帳單週期 06/30-07/31/2026）
DELIVERY_FLAT = 46.00         # Toronto Hydro Delivery，沒有公式，純粹抄最新帳單
HEAT_COOLING_RATE = 0.080190  # 同上，沒有公式
HOT_WATER_RATE = 8.441708     # 同上——五、六月是 8.607438，七月變成這個值，
                               # 不是穩定公式，跟 Delivery/Heat 歸同一類

# --- 已驗證的公式，不用隨帳單更新 ---
COLD_WATER_RATE = 7.030200     # 三張帳單都一致，可以放心當常數

ELECTRICITY_TIER1_RATE = 0.12
ELECTRICITY_TIER2_RATE = 0.142   # 信心較低，未經帳單驗證，僅交叉比對

SUMMER_MONTHS = {5, 6, 7, 8, 9, 10}
SUMMER_THRESHOLD_KWH = 600.0
WINTER_THRESHOLD_KWH = 1000.0

ELECTRICITY_REBATE_RATE = 0.235

REGULATORY_RATE_PER_KWH = 0.0055    # 三張帳單驗證：0.00542 / 0.00550 / 0.00547
TAX_RECOVERY_RATE_PER_KWH = 0.0187  # 三張帳單驗證：0.01869 / 0.01872 / 0.01872
HST_FLAT = 5.04                     # 六、七月穩定一致（五月被一次性費用污染，排除）


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


def regulatory_charge(cumulative_kwh: float) -> float:
    return cumulative_kwh * REGULATORY_RATE_PER_KWH


def tax_recovery_charge(cumulative_kwh: float) -> float:
    return cumulative_kwh * TAX_RECOVERY_RATE_PER_KWH


def water_cost(source_type: str, usage_m3: float) -> float:
    rate = COLD_WATER_RATE if source_type == "cold_water" else HOT_WATER_RATE
    return usage_m3 * rate


def heat_cooling_cost(usage_kwh: float) -> float:
    return usage_kwh * HEAT_COOLING_RATE


def daily_electricity_cost(
    cumulative_through_today: float,
    cumulative_through_yesterday: float,
    month: int,
) -> float:
    """
    當日電費的分級貢獻（不含 rebate、Delivery、Regulatory、Tax Recovery——
    這些只在月累積估算時套用，daily 這裡只反映當天實際增加的用量對應到的
    分級電力費用本身）。

    如果算出負值：正常用量規模下（遠低於分級門檻）這個公式退化成
    「電價費率 × 當天用量」的純線性關係，唯一會讓結果變負的可能就是
    輸入的原始用量本身是負的——這代表資料源頭有異常值，不是這個公式
    的邏輯錯誤。呼叫端（daily_report.py）已經對這種情況做了防護。
    """
    threshold = electricity_threshold_for_month(month)
    return (
        electricity_tiered_charge(cumulative_through_today, threshold)
        - electricity_tiered_charge(cumulative_through_yesterday, threshold)
    )


def monthly_electricity_cost_with_rebate(cumulative_kwh: float, month: int) -> dict:
    """
    回傳 rebate 前後的電費，以及拆開來的 Regulatory Charges，
    讓呼叫端可以決定要不要分開顯示。
    Rebate 的計算基礎是 Tier1電費 + Delivery + Regulatory（用六月帳單
    精確驗證過：0.235 × (42.12+45.26+1.93) = 20.99，跟帳單一致）。

    已知限制：這個公式是用「完整一個月」的帳單驗證出來的，Delivery 是
    整月固定金額。月初資料量還很小時（累積用電量低），把全額 Delivery
    套進 rebate 基礎會讓 rebate 金額大過當時還很小的 tier 電費，算出
    負值——這不是資料異常，是公式套用時機（月初 vs 月底）造成的數學
    結果，跟月底帳單完整時算出來的正確答案不矛盾。這裡做防護歸零，
    避免「本月累積估算」在月初顯示不合理的負電費。
    """
    threshold = electricity_threshold_for_month(month)
    tier_charge = electricity_tiered_charge(cumulative_kwh, threshold)
    regulatory = regulatory_charge(cumulative_kwh)
    rebate = (tier_charge + DELIVERY_FLAT + regulatory) * ELECTRICITY_REBATE_RATE
    after_rebate = tier_charge - rebate
    clamped = after_rebate < 0
    if clamped:
        after_rebate = 0.0

    return {
        "tier_charge": tier_charge,
        "regulatory": regulatory,
        "rebate": rebate,
        "after_rebate": after_rebate,
        "clamped": clamped,  # 純資料旗標，讓呼叫端決定要不要記 log——這支
                              # 檔案本身不做任何 I/O，見檔案開頭說明
    }


def monthly_total_estimate(
    electricity_cumulative_kwh: float,
    cold_water_cumulative_m3: float,
    hot_water_cumulative_m3: float,
    heat_cooling_cumulative_kwh: float,
    month: int,
) -> dict:
    """本月累積總金額估算。"""
    elec = monthly_electricity_cost_with_rebate(electricity_cumulative_kwh, month)
    cold_water = water_cost("cold_water", cold_water_cumulative_m3)
    hot_water = water_cost("hot_water", hot_water_cumulative_m3)
    heat_cooling = heat_cooling_cost(heat_cooling_cumulative_kwh)
    tax_recovery = tax_recovery_charge(electricity_cumulative_kwh)

    total = (
        elec["after_rebate"]
        + elec["regulatory"]
        + cold_water
        + hot_water
        + heat_cooling
        + DELIVERY_FLAT
        + tax_recovery
        + HST_FLAT
    )

    return {
        "electricity_before_rebate": elec["tier_charge"],
        "electricity_regulatory": elec["regulatory"],
        "electricity_rebate": elec["rebate"],
        "electricity_after_rebate": elec["after_rebate"],
        "electricity_rebate_clamped": elec["clamped"],
        "cold_water": cold_water,
        "hot_water": hot_water,
        "heat_cooling": heat_cooling,
        "delivery": DELIVERY_FLAT,
        "tax_recovery": tax_recovery,
        "hst": HST_FLAT,
        "total": total,
    }
