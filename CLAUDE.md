# CLAUDE.md

台股觀察名單追蹤器：手動維護候選標的清單，排程自動抓TWSE官方資料、比對alert規則，異常記錄在網頁上。定位是個人研究追蹤工具，不做投資建議。

## 資料流
1. `data/watchlist.json`——使用者手動編輯，候選股票清單＋為什麼關注的筆記＋alert門檻（`alert_thresholds`未填的欄位繼承`global_defaults`）
2. `scripts/update_stocks.py`（由`.github/workflows/daily-stock-update.yml`平日UTC 07:30觸發，即台北15:30）：
   - 呼叫TWSE官方OpenAPI（`STOCK_DAY_ALL`收盤價、`BWIBBU_ALL`本益比/殖利率/淨值比），這兩支API只回傳「最新一日」全市場快照，不支援單股查詢也沒有歷史區間
   - 篩出watchlist的ticker，upsert進`data/stock_history.json`（同日重跑覆蓋不新增，只留最近260筆）
   - 純Python deterministic規則判斷alert（漲跌幅/52週高低/本益比/殖利率門檻），**不用LLM算數字**
   - 新alert寫入`data/alerts_log.json`（同一異常不重複記錄；10-15起不寄信，`last_sent_date`欄位名沿用舊名、網頁依賴勿改）
3. `stock_watchlist.html`——單檔前端，fetch同目錄兩份JSON渲染卡片，零外部依賴

## 已知限制（刻意不做，非遺漏）
- 沒有官方可依股票代號訂閱的重大訊息公告RSS/API，因此**做不到**「公司發布重大訊息」這類質化提醒，只有股價/本益比/殖利率/淨值比等量化數字alert
- TWSE API只有最新一日資料，歷史趨勢完全靠上線後逐日累積；52週高低規則要求`stock_history.json`該ticker累積滿60筆才啟用，避免上線初期資料不足就誤報新高/新低

## Secrets
10-15 起不需要任何 secret（寄信與 Groq 已移除）。

## TWSE API欄位陷阱
`Date`是民國年字串（`1150909`＝2026-09-09，需`year=1911+int(s[:3])`換算）；`Change`是漲跌金額不是百分比，要自算`change_pct`；`PEratio`虧損公司常是空字串，需轉`null`。
