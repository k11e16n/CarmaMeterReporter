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
# LATEST_BILL_DATE = 2026-09-29（帳單週期 07/31-08/31/2026）
# 八月驗證：用這組費率重算，跟帳單 $87.78 只差 $0.005，公式結構連續第四個月驗證通過。
# 同時也驗證了「不更新的代價」：八月初沿用七月舊費率會把估算算貴 $5.04（5.7%），
# 證實 Delivery/Heat 費率不是緩慢漂移，是每月會有感變動，這個維護習慣不能省。
DELIVERY_FLAT = 40.86          # Toronto Hydro Delivery，沒有公式，純粹抄最新帳單
HEAT_COOLING_RATE = 0.063310   # 同上，沒有公式（七月 0.080190 → 八月降回這個值，不是單向趨勢）
HOT_WATER_RATE = 8.441708      # 跟七月一樣沒變，五、六月是 8.607438

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

    已知限制（不是bug，是帳單本身的結構）：實際帳單裡，回饋金額是從整張
    帳單的總金額扣，不是只從電費這一項扣——電費小計本身完全不受回饋影響。
    這代表 after_rebate（tier_charge - rebate）在月初累積電量還小時，合理
    地會是負值，這只是「回饋折抵」這個中間量的正常結果，不代表電費本身
    異常，呼叫端加總 total 時要用這個真實值（可能為負），不能先歸零——
    先歸零會讓 total 少扣一部分回饋，算出來的總計會比真實帳單還高。
    `after_rebate_display` 是專門給畫面顯示用的版本（歸零成 $0），不要
    拿去算總計。
    """
    threshold = electricity_threshold_for_month(month)
    tier_charge = electricity_tiered_charge(cumulative_kwh, threshold)
    regulatory = regulatory_charge(cumulative_kwh)
    rebate = (tier_charge + DELIVERY_FLAT + regulatory) * ELECTRICITY_REBATE_RATE
    after_rebate = tier_charge - rebate

    return {
        "tier_charge": tier_charge,
        "regulatory": regulatory,
        "rebate": rebate,
        "after_rebate": after_rebate,  # 真實值，可能為負，total 要用這個
        "after_rebate_display": max(0.0, after_rebate),  # 畫面顯示專用
        "rebate_exceeds_charge": after_rebate < 0,  # 純資料旗標，給呼叫端決定要不要記 log
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
        "electricity_after_rebate_display": elec["after_rebate_display"],
        "electricity_rebate_exceeds_charge": elec["rebate_exceeds_charge"],
        "cold_water": cold_water,
        "hot_water": hot_water,
        "heat_cooling": heat_cooling,
        "delivery": DELIVERY_FLAT,
        "tax_recovery": tax_recovery,
        "hst": HST_FLAT,
        "total": total,
    }
