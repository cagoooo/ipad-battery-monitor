# iPad 電量監控站（石門國小資訊組）

每 15 分鐘自動從 Jamf Pro 讀取全校 iPad 電量，找出可能「充電線沒插好或壞掉」的平板。

- 正式網址：https://cagoooo.github.io/ipad-battery-monitor/
- 抓資料：`scripts/fetch_jamf.py`（只用 Python 標準函式庫）
- 排程與部署：`.github/workflows/monitor.yml`（GitHub Actions → GitHub Pages）
- 網頁：`site/`（純 HTML／CSS／JS，讀 `data.json`）

## 判斷規則

| 標記 | 條件 |
|---|---|
| 電量危險 | 低於 20% |
| 電量偏低 | 20–49% |
| 半夜沒充飽 | 學生車平板在 0:00–7:30 回報時不到 80%，且之後沒有回升到 80% |
| 連續低電量 | 連續兩次回報（相隔 2 小時以上）都低於 50% 且沒有回升 |
| 很久沒回報 | 超過 72 小時沒有回報電量 |

## 機密設定（GitHub Secrets，不放在程式碼裡）

| 名稱 | 說明 |
|---|---|
| `JAMF_URL` | Jamf Pro 網址 |
| `JAMF_CLIENT_ID` / `JAMF_CLIENT_SECRET` | 建議：Jamf「API 角色與用戶端」建立的唯讀用戶端（有設定就優先使用） |
| `JAMF_USER` / `JAMF_PASSWORD` | 沒有用戶端時使用的帳號密碼 |
| `GCHAT_WEBHOOK` | 選填：Google Chat 聊天室 Webhook，新發現問題時推播 |

設定變數（Repository variables）：`JAMF_SEND_UPDATE_INVENTORY=1`（已開啟）請所有平板立刻回報最新電量，`JAMF_UPDATE_INTERVAL_MIN` 控制多久送一次（預設 60 分鐘，上次送出時間記在 data.json 的 `inventoryCommandAt`）。

---

Made with ❤️ by [阿凱老師](https://cagoooo.github.io/Akai/)
