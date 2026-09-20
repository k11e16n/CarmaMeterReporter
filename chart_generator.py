"""每日趨勢折線圖產生器。

只接收已經撈好的資料結構（不碰 DB connection），輸出成 PNG 檔案，供
daily_report.py 上傳到 GCS 後當 LINE Flex Message 的 hero 圖片使用。
"""

from __future__ import annotations

import os
from datetime import datetime

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt

# 用專案自帶的開源字型檔（Noto Sans TC，OFL 授權），不依賴系統字型——
# 本機 macOS 的 PingFang TC/Heiti TC 在 Linux VM 上不存在，會讓中文變
# 成方塊亂碼，統一用自帶字型檔才能保證本機/VM 顯示結果一致。
_FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "fonts", "NotoSansTC-Regular.ttf")
fm.fontManager.addfont(_FONT_PATH)
plt.rcParams["font.sans-serif"] = [fm.FontProperties(fname=_FONT_PATH).get_name()]
plt.rcParams["axes.unicode_minus"] = False

METER_COLORS = {
    "hot_water": "#E4572E",  # 熱水：紅
    "cold_water": "#2E86DE",  # 冷水：藍
    "heat_cooling": "#F5A623",  # 冷暖氣：橘
    "electricity": "#16A085",  # 日常用電：青綠
}


def render_dual_line_chart(series_list: list[dict], output_path: str, title: str | None = None) -> str:
    """畫一張圖，包含兩個相關指標各自的近日趨勢實線 + 本月平均虛線。

    Args:
        series_list: 剛好 2 個 dict，各自：
            {
                "source_type": str,   # METER_COLORS 的 key，例如 "cold_water"
                "label": str,         # 顯示用標籤，例如 "冷水"
                "unit": str,          # 顯示用單位，例如 "m^3-wtr"
                "daily": list[tuple[str, float]],  # (date_str, value)，升冪排序
                "month_avg": float | None,          # 本月平均，沒有就不畫虛線
            }
        output_path: 輸出 PNG 的路徑，母目錄需已存在。
        title: 圖表標題，選填。

    兩個指標各自「最新有效日期」可能差一兩天（CARMA 各分頁回報進度本來就
    不一致），所以 x 軸一定要用真正的日期型別（不能用日期字串），不然
    matplotlib 會把日期字串當成分類座標，兩條線的日期類別合併順序一亂，
    線跟面積填色的位置就會跑掉。

    Returns:
        str: output_path，方便串接後續上傳流程。
    """
    fig, ax = plt.subplots(figsize=(8, 4.5))

    for series in series_list:
        color = METER_COLORS[series["source_type"]]
        dates = [datetime.strptime(d, "%Y-%m-%d").date() for d, _ in series["daily"]]
        values = [v for _, v in series["daily"]]

        ax.plot(dates, values, color=color, linewidth=2, label=series["label"], zorder=3)
        ax.fill_between(dates, values, color=color, alpha=0.15, zorder=1)

        if series.get("month_avg") is not None:
            ax.axhline(
                series["month_avg"],
                color=color,
                linestyle="--",
                linewidth=1,
                label=f"{series['label']} 本月平均 {series['month_avg']:.3g}",
                zorder=2,
            )

        if dates:
            ax.plot(dates[-1], values[-1], marker="o", color="red", markersize=6, zorder=5)
            ax.annotate(
                f"{series['label']} {values[-1]}{series['unit']}",
                xy=(dates[-1], values[-1]),
                xytext=(10, 10),
                textcoords="offset points",
                fontsize=9,
                bbox=dict(boxstyle="round", fc="white", ec=color, lw=1.5),
                zorder=6,
            )

    if title:
        ax.set_title(title)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)

    return output_path
