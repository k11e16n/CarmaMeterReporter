# CarmaMeterReporter

個人用電/用水監控自動化系統。每天從 CARMA（水電帳戶入口網站）抓取用量資料，
算出費用估算，用 Gemini 生成一句吐槽/稱讚，畫成趨勢圖表 + 情緒插圖，組成
LINE Flex Message 卡片推播到 LINE 群組。跑在 GCP Compute Engine VM 上，
cron 排程每天自動執行。

## 資料流

```
CARMA 網站 --(carma_scraper.py 爬蟲)--> SQLite (utility_readings)
                                              |
                                              v
                                     daily_report.py（主流程）
                                              |
              +-------------------+----------+----------+-------------------+
              v                   v                     v                   v
      report_calc.py       chart_generator.py  illustration_generator.py  gcs_upload.py
      （費用估算，純函式）    （matplotlib 趨勢圖）  （情緒插圖 + 合成觀察句）  （上傳 GCS 拿簽章URL）
              |                   |                     |                   |
              +-------------------+----------+----------+-------------------+
                                              v
                                    line_push.py（組 Flex 卡片 + 推播）
                                              v
                                         LINE 群組
```

## 每日報告結構（3張卡片，可左右滑動）

1. **水表報告**：冷水+熱水近7日趨勢圖
2. **電力報告**：冷暖氣+日常用電近7日趨勢圖
3. **本月摘要**：hero 圖是「Gemini 觀察句 + 情緒插圖」合成圖（插圖依 mood
   從本地 Irasutoya 圖庫隨機挑一張，去背後貼在角落），文字區塊是四個指標
   當日細項 + 本月累積估算明細

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `carma_scraper.py` | 登入 CARMA 網站，解析 Highcharts 資料，寫入 SQLite |
| `backfill_history.py` | 一次性歷史資料回補（往回翻月份），重用 carma_scraper.py 的函式，手動執行 |
| `report_calc.py` | 費用估算公式（分級電價、政府回饋、水費、冷暖氣費），純函式無 I/O |
| `gemini_summary.py` | 呼叫 Gemini，回傳結構化 JSON `{observation, mood}` |
| `chart_generator.py` | matplotlib 畫雙線趨勢圖（2實線+2虛線本月平均+獨立標註框），用專案自帶字型（Noto Sans TC）避免跨平台中文顯示問題 |
| `illustration_generator.py` | 依 mood 從本地圖庫挑插圖、去背、跟觀察句合成一張圖 |
| `illustration_pool_refresher.py` | 獨立維護腳本：爬 irasutoya.com 抽換本地插圖庫，不在每日報告的關鍵路徑上 |
| `composite_sticker.py` | 圖片去背+裁切工具函式（`illustration_generator.py` 用） |
| `gcs_upload.py` | 上傳圖片到 GCS bucket，回傳 v4 簽章 URL |
| `line_push.py` | LINE Flex Message 卡片組裝 + 推播 |
| `secrets_manager.py` | 從 GCP Secret Manager 讀取憑證 |
| `daily_report.py` | 主流程：整合以上所有模組，組出報告並推播 |

## 本機開發

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

憑證：本機測試用 `current_config.json`（明碼，已 gitignore）或直接傳 CLI 參數；
正式環境走 GCP Secret Manager（`--gcp-project <project>`）。

### 常用指令

```bash
# 一次性歷史回補
python3 backfill_history.py --start-year-month 2026-05 --gcp-project mypixelchatroom

# 圖庫抽換（獨立於每日報告，可以隨時手動跑）
python3 illustration_pool_refresher.py

# 每日報告 dry-run（印出 JSON，不推播、不更新 report_state，但圖表/插圖/GCS 上傳都會真的執行）
python3 daily_report.py --db-path carma_readings.db --gcp-project mypixelchatroom \
  --gcs-key-path <本機 service account key路徑> --chart-dir charts --dry-run

# --force：略過「跟上次推播比對有沒有新資料」的檢查，手動測試用，正式 cron 不要加
```

## 正式部署（GCE VM）

- 程式碼跟 requirements.txt 走 git（`git pull`），憑證 JSON key 檔案跟
  `current_config.json` 不進 git，手動 `gcloud compute scp` 傳過去
- GCS 上傳用本機 key 檔案直接呼叫 `storage.Client.from_service_account_json()`，
  不是走 VM 自己的 service account 身份（ADC）——這是評估風險可控後選擇的
  簡化方案，不是原本規劃的預設做法
- `assets/irasutoya_pool/`（爬來的第三方插圖）刻意不進 git（版權考量，
  public repo 上放整批爬來的圖片風險較高），部署後直接在 VM 上跑一次
  `illustration_pool_refresher.py` 就地生成
- crontab（VM 時區顯示是 UTC，`0 13` = 多倫多時間 EDT 早上9點，日光節約時間
  切換時會有一小時漂移，目前接受這個誤差）：

```
0 13 * * * cd ~/CarmaMeterReporter && venv/bin/python3 carma_scraper.py --gcp-project mypixelchatroom >> logs/scraper.log 2>&1 && venv/bin/python3 daily_report.py --db-path carma_readings.db --gcp-project mypixelchatroom --gcs-key-path mypixelchatroom-1e8259844107.json --chart-dir charts >> logs/report.log 2>&1

0 4 1 * * cd ~/CarmaMeterReporter && venv/bin/python3 illustration_pool_refresher.py >> logs/pool_refresh.log 2>&1
```

## 已知限制 / TODO

- [ ] GCS bucket 還沒設定 Object Lifecycle Rule（自動清理舊物件），建議
      `gsutil lifecycle set` 設定「物件超過7天自動刪除」，避免長期累積儲存費用
      （本機/VM 端的暫存檔已經有清理邏輯，這裡指的是 GCS 端本身）
- [ ] `illustration_pool_refresher.py` 是「先刪舊圖、才下載新圖」，如果下載
      整批失敗會導致該 mood 圖庫暫時清空（有 fallback 機制不影響報告本身，
      但插圖會變單調）。之後可以改成「新圖都下載成功才整批替換」
- [ ] Cloudflare Workers AI 的 account/token 已經不再使用（改用本地
      Irasutoya 圖庫），但 Ken 說要保留在 Secret Manager，之後可能拿來做
      別的圖片生成用途
- [ ] Cloudflare API token 先前在另一個對話視窗意外曝光過，原計畫是「功能
      做完確認能動後撤銷重發」——現在功能做完了，但因為已經不再使用這組
      token，撤銷與否變成單純資安衛生問題，不影響任何功能，待 Ken 決定
- [ ] LINE channel token / Gemini API key 曾經明碼放在本機一個測試檔案裡
      （已刪除，且從未進 git/推上 GitHub），Ken 目前決定不重發，如果之後
      改變主意可以再處理
- [ ] cron 時間目前是寫死 UTC，日光節約時間切換時會有一小時漂移，可以考慮
      改用 `CRON_TZ=America/Toronto`（不確定 VM 的 cron 版本支不支援，需要
      實測）
- [ ] `~/.claude/settings.json` 加了限制「Claude 寫入專案目錄以外的檔案要
      先問過」的權限規則，但因為設定檔通常在 session 開始時載入，這個改動
      還沒有實際驗證真的生效過
